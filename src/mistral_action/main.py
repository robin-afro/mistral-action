"""Main orchestrator for the Mistral GitHub Action.

This is the entrypoint that ties together:
1. GitHub context parsing (what event triggered us?)
2. Mode detection (tag, review, or agent mode?)
3. Permission checks (is the actor allowed?)
4. Progress tracking (post "working on it" comment)
5. Prompt assembly (build a rich, context-aware prompt)
6. Branch management (create branches for issue work)
7. Vibe execution (run the agent in headless mode)
8. Result reporting (update comments, create PRs)

Mirrors the architecture of anthropics/claude-code-action but for Mistral Vibe.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from mistral_action.context import (
    EntityType,
    EventType,
    GitHubContext,
    parse_github_context,
)
from mistral_action.github_api import (
    CommentResult,
    GitHubAPIError,
    add_reaction,
    check_write_permission,
    create_issue_comment,
    create_pull_request,
    git_checkout_branch,
    git_checkout_new_branch,
    git_commit,
    git_current_branch,
    git_current_sha,
    git_fetch_branch,
    git_has_changes,
    git_has_new_commits,
    git_log_since,
    git_push,
    git_setup_identity,
    update_issue_comment,
)
from mistral_action.modes import Mode, detect_mode
from mistral_action.prompt_builder import build_prompt
from mistral_action.run_vibe import Conclusion, VibeConfig, VibeResult, run_vibe

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment / action inputs
# ---------------------------------------------------------------------------


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    val = _env(key).lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


def _env_int(key: str, default: int | None = None) -> int | None:
    val = _env(key)
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return default


def _env_float(key: str, default: float | None = None) -> float | None:
    val = _env(key)
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return default


def _set_output(name: str, value: str) -> None:
    """Set a GitHub Actions output variable."""
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        with open(output_file, "a") as f:
            # Use multiline syntax to handle values with special chars
            delimiter = f"ghadelimiter_{name}"
            f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
    # Also log it
    logger.info("Output %s=%s", name, value[:200] if len(value) > 200 else value)


def _load_system_prompt() -> str:
    """Load the system prompt from the prompts directory."""
    # Check for user-provided system prompt path
    custom_path = _env("SYSTEM_PROMPT_PATH")
    if custom_path and Path(custom_path).is_file():
        return Path(custom_path).read_text(errors="replace")

    # Use the bundled system prompt
    prompts_dir = Path(__file__).parent.parent.parent / "prompts"
    system_prompt_file = prompts_dir / "system.md"
    if system_prompt_file.is_file():
        return system_prompt_file.read_text(errors="replace")

    logger.warning("System prompt not found at %s", system_prompt_file)
    return ""


# ---------------------------------------------------------------------------
# Branch naming
# ---------------------------------------------------------------------------


def _generate_branch_name(
    entity_type: str,
    entity_number: int,
    prefix: str = "mistral/",
) -> str:
    """Generate a branch name like 'mistral/issue-42-1720000000'."""
    timestamp = int(time.time())
    return f"{prefix}{entity_type}-{entity_number}-{timestamp}"


# ---------------------------------------------------------------------------
# Progress comment helpers
# ---------------------------------------------------------------------------

_PROGRESS_TEMPLATE = """\
> {icon} **Mistral** is {status}...
>
> Triggered by @{actor} · [View run]({run_url})
"""

_COMPLETE_SUCCESS_TEMPLATE = """\
> {icon} **Mistral** has finished.
>
> Triggered by @{actor} · [View run]({run_url})

{pr_section}
{branch_section}
{summary_section}
"""

_COMPLETE_SUCCESS_NO_CHANGES_TEMPLATE = """\
> ℹ️ **Mistral** finished without making changes.
>
> Triggered by @{actor} · [View run]({run_url})

{summary_section}
"""

_COMPLETE_FAILURE_TEMPLATE = """\
> {icon} **Mistral** encountered an error.
>
> Triggered by @{actor} · [View run]({run_url})

{error_section}
"""


def _post_progress_comment(
    context: GitHubContext,
    message: str = "working on it",
) -> CommentResult | None:
    """Post a progress/tracking comment on the issue or PR."""
    if not context.entity:
        return None

    body = _PROGRESS_TEMPLATE.format(
        icon="🔄",
        status=message,
        actor=context.actor.login,
        run_url=context.run_url,
        details="",
    )

    try:
        return create_issue_comment(
            context.repository.owner,
            context.repository.name,
            context.entity.number,
            body,
        )
    except GitHubAPIError:
        logger.warning("Failed to post progress comment", exc_info=True)
        return None


def _extract_vibe_summary(output: str) -> str:
    """Try to extract a meaningful summary from Vibe's output.

    Looks for the last substantial block of assistant text.
    Falls back to the last ~1500 chars if no clear summary is found.
    """
    if not output:
        return ""

    text = output.strip()

    # Take the last ~1500 chars as a rough summary
    if len(text) > 1500:
        text = "…" + text[-1500:]

    return text


def _update_progress_comment(
    context: GitHubContext,
    comment: CommentResult,
    *,
    success: bool,
    made_changes: bool = False,
    output_summary: str = "",
    branch_name: str = "",
    pr_url: str = "",
    error: str = "",
) -> None:
    """Update the progress comment with the final result."""

    if not success:
        error_section = ""
        if error:
            # Truncate very long errors
            err = error[:3000]
            error_section = f"<details><summary>Error details</summary>\n\n```\n{err}\n```\n\n</details>"

        body = _COMPLETE_FAILURE_TEMPLATE.format(
            icon="❌",
            actor=context.actor.login,
            run_url=context.run_url,
            error_section=error_section,
        )
    elif not made_changes:
        summary_section = ""
        if output_summary:
            summary_section = (
                "<details><summary>What Mistral did</summary>\n\n"
                f"```\n{output_summary}\n```\n\n"
                "</details>"
            )

        body = _COMPLETE_SUCCESS_NO_CHANGES_TEMPLATE.format(
            actor=context.actor.login,
            run_url=context.run_url,
            summary_section=summary_section,
        )
    else:
        pr_section = ""
        if pr_url:
            pr_section = f"### 📝 Pull Request\n\n**{pr_url}**\n"

        branch_section = ""
        if branch_name:
            branch_section = f"🌿 Branch: `{branch_name}`"

        summary_section = ""
        if output_summary:
            summary_section = (
                "<details><summary>What Mistral did</summary>\n\n"
                f"```\n{output_summary}\n```\n\n"
                "</details>"
            )

        body = _COMPLETE_SUCCESS_TEMPLATE.format(
            icon="✅",
            actor=context.actor.login,
            run_url=context.run_url,
            pr_section=pr_section,
            branch_section=branch_section,
            summary_section=summary_section,
        )

    # Clean up excessive blank lines
    import re
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    try:
        update_issue_comment(
            context.repository.owner,
            context.repository.name,
            comment.id,
            body,
        )
    except GitHubAPIError:
        logger.warning("Failed to update progress comment", exc_info=True)


# ---------------------------------------------------------------------------
# Mode-specific prepare logic
# ---------------------------------------------------------------------------


def _prepare_tag_mode_issue(
    context: GitHubContext,
    branch_prefix: str,
    base_branch: str,
) -> str:
    """Prepare for tag mode on an issue: create a new branch.

    Returns the branch name.
    """
    entity = context.entity
    if not entity:
        raise RuntimeError("Tag mode on issue requires an entity")

    branch_name = _generate_branch_name("issue", entity.number, prefix=branch_prefix)

    # Set up git and create the branch
    git_setup_identity()

    # Make sure we're on the base branch
    try:
        git_fetch_branch(base_branch)
        git_checkout_branch(base_branch)
    except Exception:
        logger.warning("Could not checkout %s, using current HEAD", base_branch)

    git_checkout_new_branch(branch_name)
    logger.info("Created branch: %s", branch_name)

    return branch_name


def _prepare_tag_mode_pr(context: GitHubContext) -> str:
    """Prepare for tag mode on a PR: checkout the PR's head branch.

    Returns the branch name.
    """
    entity = context.entity
    if not entity or not entity.head_ref:
        raise RuntimeError("Tag mode on PR requires a PR entity with head_ref")

    branch_name = entity.head_ref

    git_setup_identity()

    try:
        git_fetch_branch(branch_name)
        git_checkout_branch(branch_name)
    except Exception:
        logger.warning(
            "Could not checkout PR branch %s, working on detached HEAD", branch_name
        )

    logger.info("Checked out PR branch: %s", branch_name)
    return branch_name


# ---------------------------------------------------------------------------
# Post-execution: commit, push, create PR
# ---------------------------------------------------------------------------


def _commit_and_push(
    branch_name: str,
    starting_sha: str,
    issue_number: int | None = None,
) -> str | None:
    """Push any changes the agent made (commits and/or uncommitted files).

    The agent is instructed to commit with descriptive messages itself.
    This function handles three scenarios:

    1. Agent committed AND left no uncommitted changes → just push.
    2. Agent committed AND left uncommitted changes → fallback-commit the
       leftovers, then push everything.
    3. Agent did NOT commit but left uncommitted changes → fallback-commit,
       then push.
    4. Nothing changed at all → return None.

    Returns the final HEAD SHA if anything was pushed, None otherwise.
    """
    agent_committed = git_has_new_commits(starting_sha)
    has_uncommitted = git_has_changes()

    if not agent_committed and not has_uncommitted:
        logger.info("No changes to commit or push")
        return None

    if agent_committed:
        commit_log = git_log_since(starting_sha)
        logger.info("Agent made commits:\n%s", commit_log)

    # Fallback-commit any leftover uncommitted changes
    if has_uncommitted:
        fallback_msg = "chore: commit remaining changes from Mistral agent"
        if issue_number:
            fallback_msg += f"\n\nRelated to #{issue_number}"
        logger.info("Committing leftover uncommitted changes with fallback message")
        git_commit(fallback_msg)

    # Push everything
    final_sha = git_current_sha()
    git_push(branch_name)
    logger.info("Pushed to %s (HEAD: %s)", branch_name, final_sha)
    return final_sha


def _maybe_create_pr(
    context: GitHubContext,
    branch_name: str,
    base_branch: str,
    summary: str = "",
) -> str:
    """Create a PR from the branch if one doesn't exist already.

    Returns the PR URL, or empty string if no PR was created.
    """
    entity = context.entity

    # Build a rich PR title
    if entity:
        title = f"[Mistral] {entity.title}"
    else:
        title = f"[Mistral] Automated changes on `{branch_name}`"

    # Build a rich PR body
    body_parts: list[str] = []

    if entity:
        body_parts.append(f"Resolves #{entity.number}")
        body_parts.append("")
        body_parts.append(f"> **Original issue:** {entity.title}")
        if entity.body:
            # Include a truncated version of the issue body for context
            issue_excerpt = entity.body[:800].strip()
            if len(entity.body) > 800:
                issue_excerpt += "…"
            body_parts.append(f"> \n> {issue_excerpt}")
        body_parts.append("")

    if summary:
        body_parts.append("## What was done")
        body_parts.append("")
        body_parts.append(summary)
        body_parts.append("")

    body_parts.append("---")
    body_parts.append("")
    body_parts.append(
        f"🤖 *Automated by [Mistral Action]({context.run_url}) · "
        f"triggered by @{context.actor.login}*"
    )

    body = "\n".join(body_parts)

    try:
        pr_data = create_pull_request(
            context.repository.owner,
            context.repository.name,
            title=title,
            body=body,
            head=branch_name,
            base=base_branch,
        )
        pr_url = pr_data.get("html_url", "")
        logger.info("Created PR: %s", pr_url)
        return pr_url
    except GitHubAPIError as exc:
        logger.warning("Failed to create PR: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Main entrypoint for the Mistral GitHub Action."""
    # Set up logging
    log_level = _env("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("=" * 60)
    logger.info("Mistral Action starting")
    logger.info("=" * 60)

    # Read configuration from environment (set by action.yml)
    trigger_phrase = _env("TRIGGER_PHRASE", "@mistral")
    assignee_trigger = _env("ASSIGNEE_TRIGGER", "")
    label_trigger = _env("LABEL_TRIGGER", "mistral")
    custom_prompt = _env("PROMPT", "")
    mistral_api_key = _env("MISTRAL_API_KEY")
    model = _env("MODEL", "")
    max_turns = _env_int("MAX_TURNS")
    max_price = _env_float("MAX_PRICE")
    vibe_args = _env("VIBE_ARGS", "")
    timeout = _env_int("TIMEOUT_SECONDS", 1800) or 1800
    branch_prefix = _env("BRANCH_PREFIX", "mistral/")
    base_branch_override = _env("BASE_BRANCH", "")
    output_format = _env("OUTPUT_FORMAT", "text")

    if not mistral_api_key:
        logger.error("MISTRAL_API_KEY is required but not set")
        _set_output("conclusion", "failure")
        sys.exit(1)

    # Phase 1: Parse GitHub context
    logger.info("Phase 1: Parsing GitHub context")
    context = parse_github_context()

    # chdir into the workspace immediately so every git / file operation
    # runs inside the repo checkout.  uv run --directory sets cwd to the
    # *action's* directory for dependency resolution, which means all
    # subprocess calls (git, gh, etc.) would otherwise run from the wrong place.
    workspace = context.workspace
    if workspace and os.path.isdir(workspace):
        os.chdir(workspace)
        logger.info("Changed working directory to workspace: %s", workspace)

    logger.info(
        "Event: %s/%s | Actor: %s | Repo: %s",
        context.event_name.value,
        context.action,
        context.actor.login,
        context.repository.full_name,
    )
    if context.entity:
        logger.info(
            "Entity: %s #%d (%s)",
            context.entity.entity_type.value,
            context.entity.number,
            context.entity.title[:80],
        )

    # Phase 2: Detect mode
    logger.info("Phase 2: Detecting mode")
    mode_result = detect_mode(
        context,
        trigger_phrase=trigger_phrase,
        assignee_trigger=assignee_trigger,
        label_trigger=label_trigger,
        custom_prompt=custom_prompt,
    )
    logger.info("Mode: %s — %s", mode_result.mode.value, mode_result.reason)

    if mode_result.mode == Mode.SKIP:
        logger.info("Skipping — no action needed")
        _set_output("conclusion", "skipped")
        return

    # Phase 3: Check permissions
    logger.info("Phase 3: Checking permissions")
    allowed_users_str = _env("ALLOWED_USERS", "")
    allowed_users = [u.strip() for u in allowed_users_str.split(",") if u.strip()] if allowed_users_str else []

    if allowed_users and "*" not in allowed_users:
        if context.actor.login not in allowed_users:
            logger.warning(
                "Actor %s not in allowed_users list, checking repo permissions",
                context.actor.login,
            )
            has_write = check_write_permission(
                context.repository.owner,
                context.repository.name,
                context.actor.login,
            )
            if not has_write:
                logger.error("Actor %s does not have write permission", context.actor.login)
                _set_output("conclusion", "failure")
                sys.exit(1)
    else:
        # Default: check write permission
        if context.actor.login and not context.actor.is_bot:
            has_write = check_write_permission(
                context.repository.owner,
                context.repository.name,
                context.actor.login,
            )
            if not has_write:
                logger.error("Actor %s does not have write permission", context.actor.login)
                _set_output("conclusion", "failure")
                sys.exit(1)

    logger.info("Permission check passed for %s", context.actor.login)

    # Phase 4: Post progress comment + add reaction
    logger.info("Phase 4: Posting progress indicator")
    progress_comment: CommentResult | None = None

    # Add "eyes" reaction to the triggering comment
    if context.comment and context.entity:
        add_reaction(
            context.repository.owner,
            context.repository.name,
            context.comment.id,
        )

    progress_comment = _post_progress_comment(context)

    # Phase 5: Prepare branch
    logger.info("Phase 5: Preparing branch")
    branch_name = ""
    base_branch = base_branch_override or context.repository.default_branch
    pr_url = ""
    # Track whether we need to create a PR at the end (issues / agent without entity)
    needs_pr = False

    is_issue = (
        context.entity is not None
        and not context.entity.is_pull_request
    )
    is_pr = (
        context.entity is not None
        and context.entity.is_pull_request
    )

    if is_issue:
        # Issues ALWAYS get a new branch — fail hard if we can't create one,
        # because we must never push to the default branch.
        branch_name = _prepare_tag_mode_issue(context, branch_prefix, base_branch)
        needs_pr = True
        logger.info("Created branch %s for issue #%d", branch_name, context.entity.number)
    elif is_pr:
        # PRs: checkout the PR's existing head branch
        branch_name = _prepare_tag_mode_pr(context)
        needs_pr = False
    elif mode_result.mode == Mode.AGENT and not context.entity:
        # Agent mode without entity (workflow_dispatch, schedule, etc.):
        # create a branch so we never touch the default branch.
        git_setup_identity()
        branch_name = _generate_branch_name("agent", 0, prefix=branch_prefix)
        git_checkout_new_branch(branch_name)
        needs_pr = True
        logger.info("Created branch %s for agent mode", branch_name)
    else:
        # Review mode or other — work on whatever branch we're on
        git_setup_identity()
        branch_name = os.environ.get("GITHUB_REF_NAME", base_branch)

    _set_output("branch_name", branch_name)
    logger.info("Working branch: %s", branch_name or "(current)")

    # Phase 6: Build prompt
    logger.info("Phase 6: Building prompt")
    system_prompt = _load_system_prompt()
    workspace = context.workspace

    prompt = build_prompt(
        context=context,
        mode=mode_result.mode,
        trigger_phrase=trigger_phrase,
        custom_prompt=custom_prompt,
        system_prompt=system_prompt,
        workspace=workspace,
    )

    logger.info("Prompt assembled (%d characters)", len(prompt))

    # Record the starting SHA so we can detect agent commits later
    starting_sha = git_current_sha()
    logger.info("Starting SHA: %s", starting_sha)

    # Phase 7: Run Vibe
    logger.info("Phase 7: Running Mistral Vibe")
    logger.info("=" * 60)

    extra_args = vibe_args.split() if vibe_args else []

    vibe_config = VibeConfig(
        prompt=prompt,
        api_key=mistral_api_key,
        model=model,
        max_turns=max_turns,
        max_price=max_price,
        output_format=output_format,
        auto_approve=True,
        extra_args=extra_args,
        timeout_seconds=timeout,
        workdir=workspace,
    )

    vibe_result: VibeResult = run_vibe(vibe_config)

    logger.info("=" * 60)
    logger.info("Vibe finished: %s", vibe_result.conclusion.value)

    # Re-read the branch name — the agent may have renamed it (issues only).
    # For PRs we keep the original name no matter what.
    if is_issue or (not is_pr and needs_pr):
        actual_branch = git_current_branch()
        if actual_branch and actual_branch != branch_name:
            logger.info(
                "Agent renamed branch: %s → %s", branch_name, actual_branch
            )
            branch_name = actual_branch
            _set_output("branch_name", branch_name)

    # Phase 8: Handle results
    logger.info("Phase 8: Handling results")

    # Build a human-readable summary from Vibe's output
    output_summary = ""
    made_changes = False
    if vibe_result.output:
        output_summary = _extract_vibe_summary(vibe_result.output)

    if vibe_result.conclusion == Conclusion.SUCCESS:
        # Push any commits the agent made (and fallback-commit leftovers)
        sha = _commit_and_push(
            branch_name,
            starting_sha=starting_sha,
            issue_number=context.entity.number if context.entity else None,
        )
        made_changes = sha is not None

        # Create PR if we're on a fresh branch that needs one
        if sha and needs_pr and branch_name:
            # Include the agent's own commit log in the PR body
            commit_log = git_log_since(starting_sha, format="%h %s")
            pr_summary = output_summary
            if commit_log:
                pr_summary = f"### Commits\n\n```\n{commit_log}\n```\n\n{output_summary}"
            pr_url = _maybe_create_pr(
                context, branch_name, base_branch, summary=pr_summary,
            )
            if pr_url:
                _set_output("pr_url", pr_url)
        elif not sha and needs_pr:
            logger.info("No changes were made — skipping PR creation")

    # Phase 9: Update progress comment
    logger.info("Phase 9: Updating progress comment")
    if progress_comment:
        _update_progress_comment(
            context,
            progress_comment,
            success=vibe_result.conclusion == Conclusion.SUCCESS,
            made_changes=made_changes,
            output_summary=output_summary,
            branch_name=branch_name,
            pr_url=pr_url,
            error=vibe_result.error,
        )

    # Set final outputs
    conclusion = (
        "success" if vibe_result.conclusion == Conclusion.SUCCESS
        else "timeout" if vibe_result.conclusion == Conclusion.TIMEOUT
        else "failure"
    )
    _set_output("conclusion", conclusion)

    # Write step summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        try:
            with open(summary_file, "a") as f:
                f.write(f"## Mistral Action Report\n\n")
                f.write(f"- **Mode:** {mode_result.mode.value}\n")
                f.write(f"- **Conclusion:** {conclusion}\n")
                f.write(f"- **Actor:** @{context.actor.login}\n")
                if branch_name:
                    f.write(f"- **Branch:** `{branch_name}`\n")
                if pr_url:
                    f.write(f"- **PR:** {pr_url}\n")
                f.write("\n")
                if vibe_result.error:
                    f.write(f"### Error\n\n```\n{vibe_result.error[:5000]}\n```\n")
        except OSError:
            logger.warning("Failed to write step summary", exc_info=True)

    if vibe_result.conclusion != Conclusion.SUCCESS:
        logger.error("Action failed: %s", vibe_result.error or "unknown error")
        sys.exit(1)

    logger.info("Action completed successfully")


if __name__ == "__main__":
    main()
