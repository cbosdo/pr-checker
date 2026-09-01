# SPDX-FileCopyrightText: 2026 SUSE LLC
# SPDX-FileContributor: Cédric Bosdonnat
#
# SPDX-License-Identifier: Apache-2.0

import click
import json
import logging
import os.path
import re
import subprocess
import tempfile
from typing import List, Dict, Any, Optional
from github import Auth, Github, Repository, PullRequest, Commit


def setup_logging(level_name: str):
    """Configures global logging level and format."""
    numeric_level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Helper Logic for Evaluation
# ---------------------------------------------------------------------------


class PRContext:
    def __init__(
        self,
        pr: PullRequest.PullRequest,
        files: List[str],
        comments_text: str,
        body_text: str,
        head_commit: Commit.Commit,
        statuses: Dict[str, Any],
        commit_date: Any,
    ):
        self.pr = pr
        self.files = files
        self.comments_text = comments_text
        self.body_text = body_text
        self.head_commit = head_commit
        self.statuses = statuses
        self.commit_date = commit_date

    @classmethod
    def fetch(
        cls, repo: Repository.Repository, pr: PullRequest.PullRequest
    ) -> "PRContext":
        """Fetches all necessary PR details in a single aggregated pass."""
        logging.debug(f"Fetching full context for PR #{pr.number}...")

        # Fetch modified filenames
        files = [f.filename for f in pr.get_files()]

        # Fetch issue comments into a single joined body for fast regex checking
        comments = [c.body for c in pr.get_issue_comments() if c.body]
        comments_text = "\n".join(comments)

        # Get head commit and extract statuses map (latest status per check context)
        head_commit = repo.get_commit(pr.head.sha)
        commit_date = head_commit.commit.committer.date

        statuses = {}
        for s in head_commit.get_statuses():
            if s.context not in statuses:
                statuses[s.context] = (
                    s  # PyGithub presents statuses reverse-chronologically
                )

        return cls(
            pr=pr,
            files=files,
            comments_text=comments_text,
            body_text=pr.body or "",
            head_commit=head_commit,
            statuses=statuses,
            commit_date=commit_date,
        )


def matches_patterns(files: List[str], patterns: List[str]) -> bool:
    """Check if any file matches any pattern using fnmatch-style regex."""
    import fnmatch

    for f in files:
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
    if body_text:
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
    if latest_status and latest_status.state == "pending":
        logging.info(
            f"PR #{ctx.pr.number}: Check '{check_name}' is currently PENDING. Skipping."
        )
        return False

    # Magic comment check
    if has_magic_comment(ctx.comments_text, check_name):
        logging.info(
            f"PR #{ctx.pr.number}: Found magic comment to rerun '{check_name}'."
        )
        return True

    # Checkbox check in body
    if has_checked_box(ctx.body_text, check_name):
        logging.info(f"PR #{ctx.pr.number}: Found checked box to rerun '{check_name}'.")
        return True

    # File patterns match AND (never ran OR ran before last commit)
    if matches_patterns(ctx.files, patterns):
        if not latest_status:
            logging.info(
                f"PR #{ctx.pr.number}: Check '{check_name}' has never been run."
            )
            return True

        if latest_status.created_at < ctx.commit_date:
            logging.info(
                f"PR #{ctx.pr.number}: Check '{check_name}' ran before last commit date ({ctx.commit_date})."
            )
            return True
        else:
            logging.debug(
                f"PR #{ctx.pr.number}: Check '{check_name}' is already up-to-date."
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

    # Initialize PyGithub using netrc token
    gh = Github(auth=Auth.NetrcAuth())

    # Initialize GitHub connection and pass context down to subcommands
    ctx.obj = {"repo": gh.get_repo(repo)}


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
@click.pass_context
def list_prs(ctx, config: str, output: str):
    """Scan open PRs and output a JSON file of required check runs."""
    repo: Repository.Repository = ctx.obj["repo"]

    with open(config, "r") as f:
        check_mapping: Dict[str, List[str]] = json.load(f)

    results = {}
    open_prs = repo.get_pulls(state="open")

    logging.info("Scanning opened Pull Requests...")
    for pr in open_prs:
        logging.debug(
            f"#{pr.number} #{pr.title} (head: {pr.head.sha[:7]}) #{pr.html_url}"
        )
        pr_context = PRContext.fetch(repo, pr)
        required_checks = []

        for check_name, patterns in check_mapping.items():
            if evaluate_check_run(pr_context, check_name, patterns):
                required_checks.append(check_name)

        if required_checks:
            results[pr.number] = {
                "head_sha": pr.head.sha,
                "checks_to_run": required_checks,
            }

    with open(output, "w") as f:
        json.dump(results, f, indent=2)

    logging.info(f"List complete. Matched {len(results)} PRs requiring checks.")
    logging.info(f"Results written to '{output}'.")


@cli.command("run")
@click.option("--pr", type=int, required=True, help="Target Pull Request number.")
@click.option("--check-name", required=True, help="Name of the check context to run.")
@click.option("--command", required=True, help="Test script/command to execute.")
@click.option(
    "--build-url", default=None, help="URL to the build log to be set in the PR check."
)
@click.pass_context
def run_check(ctx, pr: int, check_name: str, command: str, build_url: Optional[str]):
    """Checkout a PR to a temporary directory, run a check, and post status."""
    repo: Repository.Repository = ctx.obj["repo"]

    pull_request = repo.get_pull(pr)
    head_sha = pull_request.head.sha
    head_commit = repo.get_commit(head_sha)

    # Base status parameters dictionary
    status_kwargs = {
        "context": check_name,
        "description": "Check is currently running...",
    }
    if build_url:
        status_kwargs["target_url"] = build_url

    success = False
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logging.debug(f"Created temporary workspace: {tmp_dir}")

            logging.info(f"Fetching commit {head_sha[:7]} via shallow clone...")
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
                    repo.clone_url,
                    "clone",
                ],
                cwd=tmp_dir,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Mark check status as pending
            logging.info(f"Updating GitHub status context '{check_name}' to PENDING...")
            head_commit.create_status(state="pending", **status_kwargs)

            logging.info(f"Executing command: '{command}'")
            result = subprocess.run(
                command,
                shell=True,
                cwd=os.path.join(tmp_dir, "clone"),
            )

            success = result.returncode == 0

    except Exception as e:
        logging.error(f"Execution failed with error: {e}")
        success = False

    # Mark final status
    final_state = "success" if success else "failure"
    status_kwargs["description"] = (
        "Check passed successfully!" if success else "Check failed."
    )

    head_commit.create_status(state=final_state, **status_kwargs)

    logging.info(
        f"Check '{check_name}' completed. Updated GitHub status state to: {final_state.upper()}"
    )


def main():
    cli()


if __name__ == "__main__":
    main()
