"""Assemble rich, context-aware prompts from GitHub event data.

The prompt builder gathers all relevant context (issue body, PR diff, comments,
review threads, project instructions) and combines them into a single mega-prompt
that Vibe can act on in headless mode.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mistral_action.context import (
    Comment,
    Entity,
    EntityType,
    EventType,
    GitHubContext,
)
from mistral_action.github_api import (
    get_issue_comments,
    get_pr_diff,
    get_pr_files,
    get_pr_review_comments,
)
from mistral_action.modes import Mode

logger = logging.getLogger(__name__)

# Max characters for diff to avoid blowing up context
MAX_DIFF_CHARS = 120_000
MAX_COMMENT_CHARS = 2_000
MAX_COMMENTS = 50


def _truncate(text: str, max_chars: int, label: str = "content") -> str:
    if len(text) <= max_chars:
        return text
    logger.info("Truncating %s from %d to %d chars", label, len(text), max_chars)
    return text[:max_chars] + f"\n\n... ({label} truncated at {max_chars} characters)"


def _read_project_instructions(workspace: str) -> str:
    """Read project-level agent instructions from well-known files."""
    candidates = [
        "AGENTS.md",
        ".vibe/AGENTS.md",
        ".github/AGENTS.md",
        "CLAUDE.md",  # also support Claude's convention
    ]
    parts: list[str] = []
    for candidate in candidates:
        path = Path(workspace) / candidate
        if path.is_file():
            content = path.read_text(errors="replace").strip()
            if content:
                parts.append(f"## Project instructions from `{candidate}`\n\n{content}")
                logger.info("Loaded project instructions from %s", candidate)
    return "\n\n---\n\n".join(parts)


def _format_entity_context(entity: Entity) -> str:
    """Format the issue or PR as context."""
    kind = "Pull Request" if entity.is_pull_request else "Issue"
    lines = [
        f"## {kind} #{entity.number}: {entity.title}",
        "",
    ]
    if entity.labels:
        lines.append(f"**Labels:** {', '.join(entity.labels)}")
    if entity.user:
        lines.append(f"**Author:** @{entity.user.login}")
    if entity.state:
        lines.append(f"**State:** {entity.state}")
    if entity.is_pull_request:
        if entity.base_ref and entity.head_ref:
            lines.append(f"**Branches:** `{entity.head_ref}` → `{entity.base_ref}`")
    lines.append("")
    if entity.body:
        lines.append("### Description")
        lines.append("")
        lines.append(entity.body)
    return "\n".join(lines)


def _format_comment_context(comment: Comment) -> str:
    """Format a single comment."""
    parts = [f"**@{comment.user.login}**"]
    if comment.created_at:
        parts[0] += f" ({comment.created_at})"
    parts[0] += ":"
    parts.append("")

    if comment.path:
        parts.append(f"_File: `{comment.path}`_")
        if comment.line is not None:
            parts[-1] = f"_File: `{comment.path}` line {comment.line}_"
    if comment.diff_hunk:
        parts.append("```diff")
        parts.append(comment.diff_hunk)
        parts.append("```")

    body = _truncate(comment.body, MAX_COMMENT_CHARS, "comment")
    parts.append(body)
    return "\n".join(parts)


def _format_conversation(
    owner: str,
    repo: str,
    issue_number: int,
    triggering_comment: Comment | None,
) -> str:
    """Fetch and format the full comment thread on an issue or PR."""
    try:
        raw_comments = get_issue_comments(owner, repo, issue_number)
    except Exception:
        logger.warning("Failed to fetch comments for #%d", issue_number, exc_info=True)
        raw_comments = []

    if not raw_comments:
        return ""

    # Limit to most recent comments
    if len(raw_comments) > MAX_COMMENTS:
        raw_comments = raw_comments[-MAX_COMMENTS:]

    lines = ["## Conversation"]
    for rc in raw_comments:
        user = rc.get("user", {})
        c = Comment(
            id=rc.get("id", 0),
            body=rc.get("body", ""),
            user=type(
                "Actor", (), {"login": user.get("login", "unknown")}
            )(),  # quick shim
            created_at=rc.get("created_at", ""),
        )
        # Skip the triggering comment itself — it's already in the prompt
        if triggering_comment and c.id == triggering_comment.id:
            continue
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"**@{c.user.login}** ({c.created_at}):")
        lines.append("")
        lines.append(_truncate(c.body, MAX_COMMENT_CHARS, "comment"))

    return "\n".join(lines)


def _format_pr_diff_context(owner: str, repo: str, pr_number: int) -> str:
    """Fetch and format the PR diff and file list."""
    parts: list[str] = []

    # File list
    try:
        files = get_pr_files(owner, repo, pr_number)
        if files:
            file_lines = [f"## Files changed ({len(files)} files)"]
            for f in files:
                status = f.get("status", "modified")
                filename = f.get("filename", "")
                additions = f.get("additions", 0)
                deletions = f.get("deletions", 0)
                file_lines.append(f"- `{filename}` ({status}, +{additions}/-{deletions})")
            parts.append("\n".join(file_lines))
    except Exception:
        logger.warning("Failed to fetch PR files", exc_info=True)

    # Diff
    try:
        diff = get_pr_diff(owner, repo, pr_number)
        if diff:
            diff = _truncate(diff, MAX_DIFF_CHARS, "diff")
            parts.append(f"## Diff\n\n```diff\n{diff}\n```")
    except Exception:
        logger.warning("Failed to fetch PR diff", exc_info=True)

    # Review comments (inline)
    try:
        review_comments = get_pr_review_comments(owner, repo, pr_number)
        if review_comments:
            rc_lines = ["## Existing review comments"]
            for rc in review_comments[:30]:  # limit
                user = rc.get("user", {}).get("login", "unknown")
                body = rc.get("body", "")
                path = rc.get("path", "")
                line = rc.get("line")
                hunk = rc.get("diff_hunk", "")
                rc_lines.append("")
                rc_lines.append("---")
                rc_lines.append(f"**@{user}** on `{path}`" + (f" line {line}" if line else "") + ":")
                if hunk:
                    rc_lines.append(f"```diff\n{hunk}\n```")
                rc_lines.append(_truncate(body, MAX_COMMENT_CHARS, "review comment"))
            parts.append("\n".join(rc_lines))
    except Exception:
        logger.warning("Failed to fetch review comments", exc_info=True)

    return "\n\n".join(parts)


def _extract_user_request(
    context: GitHubContext,
    trigger_phrase: str,
) -> str:
    """Extract the actual user request from the triggering comment or entity body.

    Strips the trigger phrase so Vibe gets a clean instruction.
    """
    text = ""
    if context.comment:
        text = context.comment.body
    elif context.entity:
        text = context.entity.body

    # Remove the trigger phrase
    cleaned = text
    for variant in [trigger_phrase, trigger_phrase.lower(), trigger_phrase.upper()]:
        cleaned = cleaned.replace(variant, "").strip()

    return cleaned if cleaned else text


def build_prompt(
    context: GitHubContext,
    mode: Mode,
    trigger_phrase: str = "@mistral",
    custom_prompt: str = "",
    system_prompt: str = "",
    workspace: str = "",
) -> str:
    """Build the full prompt to send to Vibe.

    Assembles:
    1. System prompt (how to behave as a GitHub agent)
    2. Project instructions (AGENTS.md)
    3. Entity context (issue/PR title, body, labels)
    4. Conversation context (comments thread)
    5. PR-specific context (diff, files, review comments)
    6. The user's actual request
    """
    sections: list[str] = []

    # --- System prompt ---
    if system_prompt:
        sections.append(system_prompt)

    # --- Project instructions ---
    if workspace:
        instructions = _read_project_instructions(workspace)
        if instructions:
            sections.append(instructions)

    owner = context.repository.owner
    repo = context.repository.name

    # --- Agent mode: custom prompt, minimal context ---
    if mode == Mode.AGENT:
        if context.entity:
            sections.append(_format_entity_context(context.entity))
            if context.entity.is_pull_request:
                sections.append(
                    _format_pr_diff_context(owner, repo, context.entity.number)
                )
        sections.append(f"## Task\n\n{custom_prompt}")
        return "\n\n---\n\n".join(sections)

    # --- Review mode: PR opened/synced, run review ---
    if mode == Mode.REVIEW:
        if context.entity:
            sections.append(_format_entity_context(context.entity))
            sections.append(
                _format_pr_diff_context(owner, repo, context.entity.number)
            )

        sections.append(
            "## Task\n\n"
            "Review this pull request. Analyze the diff carefully and provide feedback.\n\n"
            "Focus on:\n"
            "- Bugs, logic errors, and edge cases\n"
            "- Security issues\n"
            "- Performance concerns\n"
            "- Code quality and readability\n"
            "- Missing tests or documentation\n\n"
            "If the code looks good, say so. Be constructive and specific.\n"
            "Do NOT nitpick style issues unless they affect readability."
        )

        if custom_prompt:
            sections.append(f"## Additional instructions\n\n{custom_prompt}")

        return "\n\n---\n\n".join(sections)

    # --- Tag mode: triggered by @mistral mention ---
    if mode == Mode.TAG:
        if context.entity:
            sections.append(_format_entity_context(context.entity))

            # Add conversation history
            conversation = _format_conversation(
                owner,
                repo,
                context.entity.number,
                context.comment,
            )
            if conversation:
                sections.append(conversation)

            # Add PR context if this is a PR
            if context.entity.is_pull_request:
                sections.append(
                    _format_pr_diff_context(owner, repo, context.entity.number)
                )

        # The user's request
        user_request = _extract_user_request(context, trigger_phrase)
        if user_request:
            sections.append(f"## Request from @{context.actor.login}\n\n{user_request}")

        # Add context about the triggering comment if it's a review comment
        if context.comment and context.comment.diff_hunk:
            sections.append(
                f"## Code context (from review comment)\n\n"
                f"File: `{context.comment.path}`"
                + (f" line {context.comment.line}" if context.comment.line else "")
                + f"\n\n```diff\n{context.comment.diff_hunk}\n```"
            )

        # Guidance for the agent based on entity type
        if context.entity and not context.entity.is_pull_request:
            sections.append(
                "## Instructions\n\n"
                "You are working on an issue. After understanding the request:\n"
                "1. Explore the codebase to understand the relevant code.\n"
                "2. Make the necessary code changes.\n"
                "3. Run tests to verify your changes work.\n"
                "4. Commit and push your changes to the current branch.\n"
            )
        elif context.entity and context.entity.is_pull_request:
            sections.append(
                "## Instructions\n\n"
                "You are working on a pull request. After understanding the request:\n"
                "1. If asked to review: analyze the diff and provide feedback.\n"
                "2. If asked to make changes: implement them, run tests, commit and push.\n"
                "3. If the request is about a specific review comment: focus on that code location.\n"
            )

        return "\n\n---\n\n".join(sections)

    # Fallback
    return custom_prompt or "No task provided."
