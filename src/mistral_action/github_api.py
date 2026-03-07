"""GitHub API helpers using the `gh` CLI.

All GitHub operations go through `gh` which is pre-authenticated on Actions runners.
This avoids needing PyGithub or requests — `gh` handles auth, pagination, and rate limiting.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class GitHubAPIError(Exception):
    """Raised when a `gh` CLI call fails."""

    def __init__(self, message: str, returncode: int = 1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def _run_gh(
    args: list[str],
    *,
    check: bool = True,
    input_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a `gh` CLI command and return the result."""
    cmd = ["gh", *args]
    logger.debug("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=input_data,
    )

    if check and result.returncode != 0:
        raise GitHubAPIError(
            f"`gh {' '.join(args)}` failed (exit {result.returncode}): {result.stderr.strip()}",
            returncode=result.returncode,
            stderr=result.stderr,
        )

    return result


def _gh_api(
    endpoint: str,
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
    json_body: dict | None = None,
    raw_body: str | None = None,
) -> dict | list | str:
    """Call the GitHub REST API via `gh api`.

    Returns parsed JSON, or raw text if the response isn't JSON.
    """
    args = ["api", endpoint, "--method", method]

    if fields:
        for key, value in fields.items():
            args.extend(["-f", f"{key}={value}"])

    input_data = None
    if json_body is not None:
        args.extend(["--input", "-"])
        input_data = json.dumps(json_body)
    elif raw_body is not None:
        args.extend(["--input", "-"])
        input_data = raw_body

    result = _run_gh(args, input_data=input_data)
    stdout = result.stdout.strip()

    if not stdout:
        return {}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return stdout


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def get_actor_permission(owner: str, repo: str, username: str) -> str:
    """Get the permission level for a user on a repository.

    Returns one of: "admin", "write", "read", "none".
    """
    try:
        data = _gh_api(f"/repos/{owner}/{repo}/collaborators/{username}/permission")
        if isinstance(data, dict):
            return data.get("permission", "none")
        return "none"
    except GitHubAPIError:
        return "none"


def check_write_permission(owner: str, repo: str, username: str) -> bool:
    """Check if a user has write (or higher) access to a repository."""
    permission = get_actor_permission(owner, repo, username)
    return permission in ("admin", "write", "maintain")


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@dataclass
class CommentResult:
    id: int
    html_url: str


def create_issue_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> CommentResult:
    """Create a comment on an issue or pull request."""
    data = _gh_api(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        method="POST",
        json_body={"body": body},
    )
    if isinstance(data, dict):
        return CommentResult(
            id=data.get("id", 0),
            html_url=data.get("html_url", ""),
        )
    raise GitHubAPIError(f"Unexpected response creating comment: {data}")


def update_issue_comment(
    owner: str,
    repo: str,
    comment_id: int,
    body: str,
) -> None:
    """Update an existing comment."""
    _gh_api(
        f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
        method="PATCH",
        json_body={"body": body},
    )


def add_reaction(
    owner: str,
    repo: str,
    comment_id: int,
    reaction: str = "eyes",
) -> None:
    """Add a reaction to a comment. Reaction can be: +1, -1, laugh, confused, heart, hooray, rocket, eyes."""
    try:
        _gh_api(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions",
            method="POST",
            json_body={"content": reaction},
        )
    except GitHubAPIError as exc:
        # Reactions can fail if already exists — not critical
        logger.warning("Failed to add reaction: %s", exc)


# ---------------------------------------------------------------------------
# Issues & Pull Requests — reading
# ---------------------------------------------------------------------------


def get_issue_comments(
    owner: str,
    repo: str,
    issue_number: int,
    per_page: int = 100,
) -> list[dict]:
    """Fetch all comments on an issue or PR."""
    data = _gh_api(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments?per_page={per_page}",
    )
    if isinstance(data, list):
        return data
    return []


def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    """Fetch the diff for a pull request."""
    result = _run_gh(
        ["pr", "diff", str(pr_number), "--repo", f"{owner}/{repo}"],
    )
    return result.stdout


def get_pr_files(owner: str, repo: str, pr_number: int) -> list[dict]:
    """Fetch the list of files changed in a PR."""
    data = _gh_api(f"/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100")
    if isinstance(data, list):
        return data
    return []


def get_pr_review_comments(
    owner: str,
    repo: str,
    pr_number: int,
) -> list[dict]:
    """Fetch review comments on a PR."""
    data = _gh_api(
        f"/repos/{owner}/{repo}/pulls/{pr_number}/comments?per_page=100",
    )
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------


def create_branch(
    owner: str,
    repo: str,
    branch_name: str,
    from_sha: str,
) -> None:
    """Create a new branch from a given SHA."""
    _gh_api(
        f"/repos/{owner}/{repo}/git/refs",
        method="POST",
        json_body={
            "ref": f"refs/heads/{branch_name}",
            "sha": from_sha,
        },
    )


def get_default_branch_sha(owner: str, repo: str, branch: str = "") -> str:
    """Get the HEAD SHA of a branch (defaults to the repo's default branch)."""
    if not branch:
        repo_data = _gh_api(f"/repos/{owner}/{repo}")
        if isinstance(repo_data, dict):
            branch = repo_data.get("default_branch", "main")
        else:
            branch = "main"

    data = _gh_api(f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
    if isinstance(data, dict):
        obj = data.get("object", {})
        return obj.get("sha", "")
    return ""


# ---------------------------------------------------------------------------
# Pull Requests — creation
# ---------------------------------------------------------------------------


def create_pull_request(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    draft: bool = False,
) -> dict:
    """Create a pull request."""
    data = _gh_api(
        f"/repos/{owner}/{repo}/pulls",
        method="POST",
        json_body={
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        },
    )
    if isinstance(data, dict):
        return data
    raise GitHubAPIError(f"Unexpected response creating PR: {data}")


# ---------------------------------------------------------------------------
# PR Reviews
# ---------------------------------------------------------------------------


def create_pr_review(
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    event: str = "COMMENT",
    comments: list[dict] | None = None,
) -> dict:
    """Submit a pull request review.

    event: "APPROVE", "REQUEST_CHANGES", or "COMMENT".
    comments: optional list of inline review comments, each with
              {"path": str, "line": int, "body": str}.
    """
    payload: dict = {
        "body": body,
        "event": event,
    }
    if comments:
        payload["comments"] = comments

    data = _gh_api(
        f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        method="POST",
        json_body=payload,
    )
    if isinstance(data, dict):
        return data
    raise GitHubAPIError(f"Unexpected response creating review: {data}")


# ---------------------------------------------------------------------------
# Git operations (local)
# ---------------------------------------------------------------------------


def git_setup_identity(name: str = "mistral[bot]", email: str = "mistral[bot]@users.noreply.github.com") -> None:
    """Configure git identity for commits.

    Uses --global so the command works regardless of the current working
    directory (the action's composite steps may run from the action path,
    not the workspace).
    """
    subprocess.run(["git", "config", "--global", "user.name", name], check=True, capture_output=True)
    subprocess.run(["git", "config", "--global", "user.email", email], check=True, capture_output=True)


def git_checkout_new_branch(branch_name: str) -> None:
    """Create and checkout a new branch from the current HEAD."""
    subprocess.run(["git", "checkout", "-b", branch_name], check=True, capture_output=True)


def git_checkout_branch(branch_name: str) -> None:
    """Checkout an existing branch."""
    subprocess.run(["git", "checkout", branch_name], check=True, capture_output=True)


def git_fetch_branch(branch_name: str) -> None:
    """Fetch a remote branch."""
    subprocess.run(
        ["git", "fetch", "origin", branch_name],
        check=True,
        capture_output=True,
    )


def git_add_all() -> None:
    """Stage all changes."""
    subprocess.run(["git", "add", "-A"], check=True, capture_output=True)


def git_has_changes() -> bool:
    """Check if there are any staged or unstaged changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def git_commit(message: str) -> str | None:
    """Commit staged changes. Returns the commit SHA, or None if nothing to commit."""
    if not git_has_changes():
        return None

    git_add_all()
    subprocess.run(
        ["git", "commit", "-m", message],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def git_push(branch_name: str, force: bool = False) -> None:
    """Push the current branch to origin."""
    cmd = ["git", "push", "origin", branch_name]
    if force:
        cmd.append("--force-with-lease")
    subprocess.run(cmd, check=True, capture_output=True)


def git_current_sha() -> str:
    """Get the current HEAD SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
