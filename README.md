# GitHub PR Check Runner (`pr-checker`)

`pr-checker` is a lightweight Python CLI tool designed to inspect open GitHub Pull Requests against configurable file pattern rules, identify required status checks, and execute those checks in isolated temporary environments.

It uses local `~/.netrc` credentials for secure authentication—preventing sensitive tokens from leaking into command-line arguments or process inspection logs (`ps`).


## Key Features

- **Netrc Authentication:** Seamlessly authenticates with GitHub API and `git clone` operations via your local `~/.netrc` file.
- **Batched API Inspections:** Fetches PR files, comments, and statuses in a single context pass per PR to minimize GitHub API quota consumption.
- **Smart Re-run Triggers:** Automatically schedules or re-schedules check runs based on:
  - File pattern matches against modified files in the PR.
  - Commits pushed *after* the previous check run date.
  - Magic comments in the PR discussion (e.g., `rerun <check-name> !!!`).
  - Markdown checkboxes in the PR description (e.g., `- [x] Re-run test "<check-name>"`).
- **Pending Protection:** Prevents duplicate runs by ignoring checks currently marked as `pending`.
- **Isolated Execution:** Shallow-clones only the target commit revision using `--depth 1` into a temporary directory before running test commands.
- **Status Updates:** Updates GitHub commit status contexts to `pending`, `success`, or `failure`, with support for custom build/log target URLs (`--build-url`).


## Prerequisites & GitHub Token Scopes

### Required GitHub Token Scopes

When creating your Personal Access Token (PAT) on GitHub, select the following permissions based on your token type:

#### 1. Fine-grained Personal Access Tokens (Recommended)
Grant access to the target repository with these permissions:
- **Pull requests:** `Read-only` (to inspect open PRs, modified files, body, and comments)
- **Contents:** `Read-only` (to clone code via `git clone`)
- **Commit statuses:** `Read and write` (to post `pending`, `success`, and `failure` status checks)

#### 2. Personal Access Tokens (Classic)
Check the following top-level scope:
- `repo` (Full control of private repositories - required to read PRs, clone content, and write commit statuses)
  - *For public-only repositories:* `public_repo` and `repo:status` are sufficient.


## Configuration

### `~/.netrc` File Setup

Create or update your `~/.netrc` file in your home directory (`~/.netrc` on Linux/macOS or `%HOME%\_netrc` on Windows). 

Because `PyGithub` targets `api.github.com` and `git clone` targets `github.com`, **both machine entries must be defined** in your `.netrc` file using your token:

```text
machine api.github.com
login <yourlogin>
password ghp_YourGitHubPersonalAccessTokenHere

machine github.com
login <yourlogin>
password ghp_YourGitHubPersonalAccessTokenHere
```

> **Note:** The `login` field value can be any non-empty string (e.g., `x-access-token` or your GitHub username) when using tokens, but both `login` and `password` fields must be present.

Set strict file permissions so only your user account can read it:

```bash
chmod 600 ~/.netrc
```

### Check Rules Configuration File (`checks.json`)

Create a JSON file mapping status check names to array lists of fnmatch-style file glob patterns:

```json
{
  "unit-tests": [
    "src/**/*.py",
    "tests/**/*.py",
    "pyproject.toml"
  ],
  "frontend-lint": [
    "frontend/**/*.js",
    "frontend/**/*.ts",
    "frontend/**/*.vue"
  ],
  "docs-check": [
    "docs/**/*",
    "*.md"
  ]
}
```

## Installation

Clone the repository and install using `pip`:

```bash
pip install .
```

For development (editable mode):

```bash
pip install -e .
```

## Usage & Examples

`pr-checker` provides two operational modes: `list` and `run`.

### `list` mode

Scans open PRs in a target repository, evaluates triggers against `checks.json`, and outputs a JSON file detailing which PRs require check runs.

#### Command Example:

```bash
pr-checker --repo "my-org/my-repo" list \
  --config checks.json \
  --output pending_checks.json
```

#### Example Output File (`pending_checks.json`):

```json
{
  "42": {
    "head_sha": "a1b2c3d4e5f67890abcdef1234567890abcdef12",
    "checks_to_run": [
      "unit-tests",
      "docs-check"
    ]
  },
  "105": {
    "head_sha": "f9e8d7c6b5a43210fedcba9876543210fedcba98",
    "checks_to_run": [
      "frontend-lint"
    ]
  }
}
```

### `run` mode

Executes a specific check for a target PR:

1. Marks the GitHub commit status context as **`pending`**.
2. Performs an isolated shallow clone (`--depth 1`) of the target commit in a temporary directory.
3. Runs the specified test command.
4. Marks the GitHub commit status context as **`success`** or **`failure`**.

#### Command Examples:

**Basic Run:**

```bash
pr-checker --repo "my-org/my-repo" run \
  --pr 42 \
  --check-name "unit-tests" \
  --command "pytest tests/"
```

**Run with Build URL attached:**

```bash
pr-checker --repo "my-org/my-repo" run \
  --pr 42 \
  --check-name "unit-tests" \
  --command "pytest tests/" \
  --build-url "[https://ci.example.com/builds/12345](https://ci.example.com/builds/12345)"
```

## How Re-run Triggers Work

A check is flagged for execution during a `list` run if **any** of the following conditions evaluate to `true`:

* **Magic Comment:** A comment matching `rerun <check-name> !!!` (case-insensitive) is present in the PR comments.
* **PR Checkbox:** A checkbox matching `- [x] Re-run test "<check-name>"` is checked in the PR body description.
* **Modified Files:** Files modified in the PR match the pattern rules in `checks.json` **AND**:
* The check has never run on the PR, **OR**
* The check's previous run timestamp is older than the PR's latest commit date.


> **Note:** If a check status is currently marked as `pending`, `pr-checker` skips rescheduling to prevent duplicate concurrent runs.

## License

Distributed under the Apache 2.0 License.
