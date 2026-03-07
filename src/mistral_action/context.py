"""Parse GitHub Actions event context into structured data."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class EventType(Enum):
    ISSUE_COMMENT = "issue_comment"
    ISSUES = "issues"
    PULL_REQUEST = "pull_request"
    PULL_REQUEST_REVIEW_COMMENT = "pull_request_review_comment"
    PULL_REQUEST_REVIEW = "pull_request_review"
    PUSH = "push"
    WORKFLOW_DISPATCH = "workflow_dispatch"
    SCHEDULE = "schedule"
    UNKNOWN = "unknown"


class EntityType(Enum):
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    NONE = "none"


@dataclass
class Actor:
    login: str
    id: int = 0
    type: str = "User"  # "User", "Bot", "Organization"

    @property
    def is_bot(self) -> bool:
        return self.type == "Bot" or self.login.endswith("[bot]")


@dataclass
class Entity:
    """An issue or pull request."""

    entity_type: EntityType
    number: int
    title: str
    body: str
    state: str = "open"
    user: Actor | None = None
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    head_ref: str = ""
    base_ref: str = ""
    html_url: str = ""
    diff_url: str = ""

    @property
    def is_pull_request(self) -> bool:
        return self.entity_type == EntityType.PULL_REQUEST


@dataclass
class Comment:
    id: int
    body: str
    user: Actor
    html_url: str = ""
    created_at: str = ""
    diff_hunk: str = ""
    path: str = ""
    line: int | None = None


@dataclass
class Repository:
    owner: str
    name: str
    full_name: str
    default_branch: str = "main"
    html_url: str = ""

    @property
    def nwo(self) -> str:
        """Name with owner, e.g. 'owner/repo'."""
        return self.full_name


@dataclass
class GitHubContext:
    event_name: EventType
    action: str
    actor: Actor
    repository: Repository
    entity: Entity | None
    comment: Comment | None
    run_id: str
    run_url: str
    server_url: str
    api_url: str
    raw_event: dict[str, Any]

    @property
    def is_entity_event(self) -> bool:
        return self.entity is not None

    @property
    def ref(self) -> str:
        return os.environ.get("GITHUB_REF", "")

    @property
    def sha(self) -> str:
        return os.environ.get("GITHUB_SHA", "")

    @property
    def workspace(self) -> str:
        return os.environ.get("GITHUB_WORKSPACE", os.getcwd())


def _parse_actor(data: dict[str, Any] | None) -> Actor:
    if not data:
        return Actor(login="unknown", id=0, type="User")
    return Actor(
        login=data.get("login", "unknown"),
        id=data.get("id", 0),
        type=data.get("type", "User"),
    )


def _fetch_full_pr(owner: str, repo: str, pr_number: int) -> dict[str, Any] | None:
    """Fetch full PR data from the API when the event payload only has a stub.

    When issue_comment fires on a PR, the payload has the PR under
    issue.pull_request as just { "url": "...", "html_url": "..." } —
    no head/base branch info. We need to call the API to get the full object.
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"/repos/{owner}/{repo}/pulls/{pr_number}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        logger.warning("Failed to fetch full PR #%d data", pr_number, exc_info=True)
    return None


def _parse_entity(event: dict[str, Any], event_name: EventType) -> Entity | None:
    """Extract the issue or PR from the event payload."""
    pr_data = event.get("pull_request")
    issue_data = event.get("issue")

    # For issue_comment events, check if the issue is actually a PR
    if event_name == EventType.ISSUE_COMMENT and issue_data:
        if issue_data.get("pull_request"):
            # The issue payload only has a PR stub — build what we can from issue_data,
            # then enrich with full PR data below in _enrich_pr_entity()
            return _build_entity(issue_data, EntityType.PULL_REQUEST)
        return _build_entity(issue_data, EntityType.ISSUE)

    if event_name in (
        EventType.PULL_REQUEST,
        EventType.PULL_REQUEST_REVIEW_COMMENT,
        EventType.PULL_REQUEST_REVIEW,
    ):
        if pr_data:
            return _build_entity(pr_data, EntityType.PULL_REQUEST)
        return None

    if event_name == EventType.ISSUES:
        if issue_data:
            return _build_entity(issue_data, EntityType.ISSUE)
        return None

    return None


def _build_entity(data: dict[str, Any], entity_type: EntityType) -> Entity:
    labels_raw = data.get("labels", [])
    labels = [lbl.get("name", "") for lbl in labels_raw if isinstance(lbl, dict)]

    assignees_raw = data.get("assignees", [])
    assignees = [a.get("login", "") for a in assignees_raw if isinstance(a, dict)]

    head_ref = ""
    base_ref = ""
    diff_url = ""
    if entity_type == EntityType.PULL_REQUEST:
        head = data.get("head", {})
        base = data.get("base", {})
        head_ref = head.get("ref", "")
        base_ref = base.get("ref", "")
        diff_url = data.get("diff_url", "")

    return Entity(
        entity_type=entity_type,
        number=data.get("number", 0),
        title=data.get("title", ""),
        body=data.get("body", "") or "",
        state=data.get("state", "open"),
        user=_parse_actor(data.get("user")),
        labels=labels,
        assignees=assignees,
        head_ref=head_ref,
        base_ref=base_ref,
        html_url=data.get("html_url", ""),
        diff_url=diff_url,
    )


def _parse_comment(event: dict[str, Any], event_name: EventType) -> Comment | None:
    comment_data = event.get("comment")
    if not comment_data:
        return None

    # For review comments, include diff context
    diff_hunk = ""
    path = ""
    line = None
    if event_name == EventType.PULL_REQUEST_REVIEW_COMMENT:
        diff_hunk = comment_data.get("diff_hunk", "")
        path = comment_data.get("path", "")
        line = comment_data.get("line")

    return Comment(
        id=comment_data.get("id", 0),
        body=comment_data.get("body", "") or "",
        user=_parse_actor(comment_data.get("user")),
        html_url=comment_data.get("html_url", ""),
        created_at=comment_data.get("created_at", ""),
        diff_hunk=diff_hunk,
        path=path,
        line=line,
    )


def _parse_repo(event: dict[str, Any]) -> Repository:
    repo = event.get("repository", {})
    owner_data = repo.get("owner", {})
    return Repository(
        owner=owner_data.get("login", ""),
        name=repo.get("name", ""),
        full_name=repo.get("full_name", ""),
        default_branch=repo.get("default_branch", "main"),
        html_url=repo.get("html_url", ""),
    )


def parse_github_context() -> GitHubContext:
    """Parse the full GitHub Actions context from environment and event JSON."""
    event_name_str = os.environ.get("GITHUB_EVENT_NAME", "unknown")
    try:
        event_name = EventType(event_name_str)
    except ValueError:
        event_name = EventType.UNKNOWN

    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    raw_event: dict[str, Any] = {}
    if event_path and Path(event_path).exists():
        raw_event = json.loads(Path(event_path).read_text())

    action = raw_event.get("action", "")
    actor_login = os.environ.get("GITHUB_ACTOR", "unknown")
    sender = raw_event.get("sender", {})
    actor = Actor(
        login=sender.get("login", actor_login),
        id=sender.get("id", 0),
        type=sender.get("type", "User"),
    )

    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_url = f"{server_url}/{repository}/actions/runs/{run_id}" if run_id else ""

    repo_obj = _parse_repo(raw_event)
    entity = _parse_entity(raw_event, event_name)

    # Enrich PR entity with full data when the event payload only has a stub
    # (e.g. issue_comment on a PR — head_ref/base_ref will be empty)
    if (
        entity is not None
        and entity.entity_type == EntityType.PULL_REQUEST
        and not entity.head_ref
        and repo_obj.owner
        and repo_obj.name
        and entity.number
    ):
        logger.info(
            "PR #%d has no head_ref — fetching full PR data from API",
            entity.number,
        )
        full_pr = _fetch_full_pr(repo_obj.owner, repo_obj.name, entity.number)
        if full_pr:
            head = full_pr.get("head", {})
            base = full_pr.get("base", {})
            entity.head_ref = head.get("ref", "")
            entity.base_ref = base.get("ref", "")
            entity.diff_url = full_pr.get("diff_url", entity.diff_url)
            logger.info(
                "Enriched PR #%d: head=%s base=%s",
                entity.number,
                entity.head_ref,
                entity.base_ref,
            )
        else:
            logger.warning("Could not fetch full PR data for #%d", entity.number)

    return GitHubContext(
        event_name=event_name,
        action=action,
        actor=actor,
        repository=repo_obj,
        entity=entity,
        comment=_parse_comment(raw_event, event_name),
        run_id=run_id,
        run_url=run_url,
        server_url=server_url,
        api_url=api_url,
        raw_event=raw_event,
    )
