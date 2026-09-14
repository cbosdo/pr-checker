# SPDX-FileCopyrightText: 2026 SUSE LLC
# SPDX-FileContributor: Cédric Bosdonnat
#
# SPDX-License-Identifier: Apache-2.0
"""
pr-checker main module
"""

import fnmatch
import json
import logging
import netrc
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import click


def setup_logging(level_name: str):
    """Configures global logging level and format."""
    numeric_level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def get_netrc_auth():
    """Fetch api.github.com token from ~/.netrc"""
    try:
        logging.debug("Reading netrc credentials for api.github.com")
        netrc_info = netrc.netrc()
        auth = netrc_info.authenticators("api.github.com")

        if not auth:
            raise ValueError("No entry found for 'api.github.com' in ~/.netrc file.")

        _, _, token = auth
        return token
    except FileNotFoundError:
        logging.error("~/.netrc file not found.")
        sys.exit(1)
    except netrc.NetrcParseError as e:
        logging.error("Error reading ~/.netrc: %s", e)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Helper Logic for Evaluation
# ---------------------------------------------------------------------------


GRAPHQL_URL = "https://api.github.com/graphql"

PR_BATCH_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(states: OPEN, first: 50, after: $cursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        body
        url
        headRefOid
        files(first: 100) {
          pageInfo {
            hasNextPage
          }
          nodes {
            path
          }
        }
        commits(last: 1) {
          nodes {
            commit {
              committedDate
              status {
                contexts {
                  context
                  state
                  createdAt
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

PR_SINGLE_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    url
    pullRequest(number: $number) {
      headRefOid
    }
  }
}
"""


def execute_with_retry(
    req: urllib.request.Request, retries: int = 3, backoff: float = 2.0
) -> bytes:
    """Executes a urllib request with exponential backoff on transient errors."""
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            # Retry on rate limits (429) or transient server errors (500, 502, 503, 504)
            if e.code in (403, 429, 500, 502, 503, 504) and attempt < retries:
                sleep_time = backoff**attempt
                retry_after_hdr = e.headers.get("Retry-After")
                if retry_after_hdr and retry_after_hdr.isdigit():
                    sleep_time = float(retry_after_hdr)

                logging.warning(
                    "HTTP %s encountered. Retrying in %.1f seconds (Attempt %d/%d)...",
                    e.code,
                    sleep_time,
                    attempt,
                    retries,
                )
                time.sleep(sleep_time)
                continue

            # Print detailed error and fail immediately for client/auth errors (401, 403, 404, etc.)
            logging.error(
                "HTTP Request failed with status %s: %s", e.code, e.read().decode()
            )
            sys.exit(1)
        except urllib.error.URLError as e:
            # Handle network-level timeouts or connection failures
            if attempt < retries:
                sleep_time = backoff**attempt
                logging.warning(
                    "Network error (%s). Retrying in %.1f seconds (Attempt %d/%d)...",
                    e.reason,
                    sleep_time,
                    attempt,
                    retries,
                )
                time.sleep(sleep_time)
                continue

            logging.error(
                "Network request failed after %d retries: %s", retries, e.reason
            )
            sys.exit(1)

    sys.exit(1)


def execute_graphql(
    query: str, variables: Dict[str, Any], token: str
) -> Dict[str, Any]:
    """Sends a GraphQL POST request using standard urllib."""
    headers = {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "pr-checker",
    }
    logging.debug("Executing GraphQL query (%s): %s", variables, query)
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(GRAPHQL_URL, data=payload, headers=headers)

    raw_data = execute_with_retry(req)
    result = json.loads(raw_data.decode("utf-8"))

    if "errors" in result:
        logging.error("GraphQL errors: %s", result["errors"])
        sys.exit(1)

    return result["data"]


def create_commit_status(
    owner: str,
    repo: str,
    sha: str,
    state: str,
    context: str,
    description: str,
    token: str,
    target_url: Optional[str] = None,
):
    """Posts a commit status via GitHub REST API v3."""
    url = f"https://api.github.com/repos/{owner}/{repo}/statuses/{sha}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "pr-checker",
    }

    payload = {
        "state": state.lower(),
        "context": context,
        "description": description,
    }
    if target_url:
        payload["target_url"] = target_url

    logging.debug("Creating status: %s", payload)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )

    raw_data = execute_with_retry(req)
    return json.loads(raw_data.decode("utf-8"))


class PRContext:
    """
    Data class representing a pull request.
    This object caches the data needed to perform the checks without querying the server every time.
    """

    def __init__(
        self,
        number: int,
        title: str,
        head_sha: str,
        html_url: str,
        files: List[str],
        paged_files: bool,
        body_text: str,
        statuses: Dict[str, Any],
        commit_date: str,
    ):
        self.number = number
        self.title = title
        self.head_sha = head_sha
        self.html_url = html_url
        self.files = files
        self.paged_files = paged_files
        self.body_text = body_text
        self.statuses = statuses
        self.commit_date = commit_date


def fetch_all_pr_contexts_graphql(
    owner: str, repo_name: str, token: str, filter_prs: Optional[Tuple[int, ...]] = None
) -> List[PRContext]:
    """Fetches full evaluation contexts for open PRs in batched GraphQL requests."""
    contexts = []
    has_next_page = True
    cursor = None

    while has_next_page:
        data = execute_graphql(
            PR_BATCH_QUERY,
            {"owner": owner, "repo": repo_name, "cursor": cursor},
            token,
        )
        pr_connection = data["repository"]["pullRequests"]

        for pr_node in pr_connection["nodes"]:
            pr_num = pr_node["number"]

            # Filter in memory if user supplied specific --pr flags
            if filter_prs and pr_num not in filter_prs:
                continue

            files = [f["path"] for f in pr_node["files"]["nodes"]]

            commit_node = pr_node["commits"]["nodes"][0]["commit"]
            commit_date = commit_node["committedDate"]

            # Parse Status contexts
            statuses = {}
            status_obj = commit_node.get("status")
            if status_obj and status_obj.get("contexts"):
                statuses = {ctx["context"]: ctx for ctx in status_obj["contexts"]}

            contexts.append(
                PRContext(
                    number=pr_num,
                    title=pr_node["title"],
                    head_sha=pr_node["headRefOid"],
                    html_url=pr_node["url"],
                    files=files,
                    paged_files=pr_node["files"]["pageInfo"]["hasNextPage"],
                    body_text=pr_node["body"] or "",
                    statuses=statuses,
                    commit_date=commit_date,
                )
            )

        has_next_page = pr_connection["pageInfo"]["hasNextPage"]
        cursor = pr_connection["pageInfo"]["endCursor"]

    return contexts


def matches_patterns(ctx: PRContext, patterns: List[str]) -> bool:
    """Check if any file matches any pattern using fnmatch-style regex."""
    if ctx.paged_files:
        logging.warning(
            "PR #%s: Blindly match: more than 100 files changed",
            ctx.number,
        )
        return True

    logging.debug(
        "matches_pattern() files: %s, patters: %s",
        ", ".join(ctx.files),
        ", ".join(patterns),
    )

    for f in ctx.files:
        for p in patterns:
            if fnmatch.fnmatch(f, p):
                return True
    return False


def has_magic_comment(comments_text: str, check_name: str) -> bool:
    """Check if a magic comment 'rerun <check_name> !!!' exists."""
    pattern = re.compile(rf"rerun\s+{re.escape(check_name)}\s+!!!", re.IGNORECASE)
    return bool(pattern.search(comments_text))


def has_checked_box(body_text: str, check_name: str) -> bool:
    """Check if 'Re-run test "<check_name>"' is checked in the PR body."""
    if not body_text:
        return False
    pattern = re.compile(
        rf"\[[xX]\]\s*Re-run\s+test\s+\"{re.escape(check_name)}\"", re.IGNORECASE
    )
    return bool(pattern.search(body_text))


def evaluate_check_run(
    ctx: PRContext,
    check_name: str,
    patterns: List[str],
) -> bool:
    """Determines whether a check needs to be scheduled using pre-fetched PR context."""
    latest_status = ctx.statuses.get(check_name)

    # If check is marked as pending, it shouldn't be rescheduled
    if latest_status and latest_status.get("state", "").lower() == "pending":
        logging.info(
            "PR #%s: Check '%s' is currently PENDING. Skipping.",
            ctx.number,
            check_name,
        )
        return False

    # Checkbox check in body
    if has_checked_box(ctx.body_text, check_name):
        logging.info(
            "PR #%s: Found checked box to rerun '%s'.",
            ctx.number,
            check_name,
        )
        return True

    # File patterns match AND (never ran OR ran before last commit)
    if matches_patterns(ctx, patterns):
        if not latest_status:
            logging.info(
                "PR #%s: Check '%s' has never been run.",
                ctx.number,
                check_name,
            )
            return True

        # Dates are in ISO-8601 UTC, and thus lexicographical and chronological orders are the same
        if latest_status.get("createdAt", "") < ctx.commit_date:
            logging.info(
                "PR #%s: Check '%s' ran before last commit date (%s).",
                ctx.number,
                check_name,
                ctx.commit_date,
            )
            return True
        logging.debug(
            "PR #%s: Check '%s' is already up-to-date.",
            ctx.number,
            check_name,
        )

    return False


# ---------------------------------------------------------------------------
# Click Command Interface
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--repo", required=True, help="Target repository name (e.g., 'owner/repo')."
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default="INFO",
    show_default=True,
    help="Set the stdout logging verbosity.",
)
@click.pass_context
def cli(ctx, repo: str, log_level: str):
    """CLI Tool to list and execute PR checks using netrc authentication."""
    setup_logging(log_level)

    # Get the token using netrc
    token = get_netrc_auth()
    ctx.obj = {
        "repo_str": repo,
        "token": token,
    }


@cli.command("list")
@click.option(
    "--config",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON config mapping check names to file pattern arrays.",
)
@click.option(
    "--output",
    default="checks.json",
    show_default=True,
    help="Destination path for the output JSON file.",
)
@click.option(
    "--pr",
    type=int,
    multiple=True,
    help="Only check the Pull Requests matching these numbers.",
)
@click.pass_context
def list_prs(ctx, config: str, output: str, pr: Tuple[int]):
    """Scan open PRs and output a JSON file of required check runs."""
    repo_str = ctx.obj["repo_str"]
    token = ctx.obj["token"]

    owner, repo_name = repo_str.split("/", 1)

    with open(config, "r", encoding="utf-8") as f:
        check_mapping: Dict[str, List[str]] = json.load(f)

    results = {}
    logging.info("Scanning opened Pull Requests via GitHub GraphQL API...")
    pr_contexts = fetch_all_pr_contexts_graphql(
        owner=owner, repo_name=repo_name, token=token, filter_prs=pr if pr else None
    )

    for pr_ctx in pr_contexts:
        logging.debug(
            "#%s %s (head: %s) %s",
            pr_ctx.number,
            pr_ctx.title,
            pr_ctx.head_sha[:7],
            pr_ctx.html_url,
        )
        required_checks = []

        for check_name, patterns in check_mapping.items():
            if evaluate_check_run(pr_ctx, check_name, patterns):
                required_checks.append(check_name)

        if required_checks:
            results[pr_ctx.number] = {
                "head_sha": pr_ctx.head_sha,
                "checks_to_run": required_checks,
            }

    with open(output, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    logging.info("List complete. Matched %d PRs requiring checks.", len(results))
    logging.info("Results written to '%s'.", output)


@cli.command("run")
@click.option("--pr", type=int, required=True, help="Target Pull Request number.")
@click.option("--check-name", required=True, help="Name of the check context to run.")
@click.option("--command", required=True, help="Test script/command to execute.")
@click.option(
    "--build-url", default=None, help="URL to the build log to be set in the PR check."
)
@click.option("--description", help="Test description to set on the Github check.")
@click.option(
    "--git-dir",
    default=None,
    type=click.Path(file_okay=False, writable=True),
    help="Git clone directory to use instead of a temporary directory.",
)
@click.pass_context
def run_check(
    ctx,
    pr: int,
    check_name: str,
    command: str,
    build_url: Optional[str],
    description: Optional[str],
    git_dir: Optional[str],
):
    """Checkout a PR to a temporary directory, run a check, and post status."""
    repo_str = ctx.obj["repo_str"]
    token = ctx.obj["token"]
    owner, repo_name = repo_str.split("/", 1)

    pr_data = execute_graphql(
        PR_SINGLE_QUERY,
        {"owner": owner, "repo": repo_name, "number": pr},
        token,
    )

    pr_node = pr_data["repository"]["pullRequest"]
    head_sha = pr_node["headRefOid"]

    def _execute_check(target_dir: str):
        if git_dir:
            os.makedirs(git_dir)

        logging.info("Fetching commit %s via shallow clone...", head_sha[:7])
        subprocess.run(
            [
                "git",
                "-c",
                "credential.helper=store",
                "-c",
                "credential.netrcFile=~/.netrc",
                "clone",
                "--depth=1",
                f"--revision={head_sha}",
                f"{pr_data['repository']['url']}.git",
                target_dir,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Mark check status as pending
        logging.info("Updating GitHub status context '%s' to PENDING...", check_name)
        create_commit_status(
            owner=owner,
            repo=repo_name,
            sha=head_sha,
            state="pending",
            context=check_name,
            description="Check is currently running...",
            token=token,
            target_url=build_url,
        )

        logging.info("Executing command: '%s'", command)
        result = subprocess.run(
            command,
            shell=True,
            check=False,
            cwd=target_dir,
        )

        return result.returncode == 0

    success = False
    try:
        if git_dir:
            success = _execute_check(git_dir)
        else:
            with tempfile.TemporaryDirectory() as tmp_dir:
                logging.debug("Created temporary workspace: %s", tmp_dir)
                success = _execute_check(tmp_dir)

    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Execution failed with error: %s", e)
        success = False

    # Mark final status
    final_state = "success" if success else "failure"
    final_desc = description or (
        "Check passed successfully!" if success else "Check failed."
    )

    create_commit_status(
        owner=owner,
        repo=repo_name,
        sha=head_sha,
        state=final_state,
        context=check_name,
        description=final_desc,
        token=token,
        target_url=build_url,
    )

    logging.info(
        "Check '%s' completed. Updated GitHub status state to: %s",
        check_name,
        final_state.upper(),
    )

    # Reflect the test status in the exit code for automation to use it
    sys.exit(0 if success else 1)


def main():
    """
    Main entry point for pr-checker.
    """
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    main()
