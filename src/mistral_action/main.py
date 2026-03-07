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
    git_current_sha,
    git_fetch_branch,
    git_has_changes,
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
> Triggered by @{actor} | [View run]({run_url})

{details}"""

_COMPLETE_TEMPLATE = """\
> {icon} **Mistral** has {status}.
>
> Triggered by @{actor} | [View run]({run_url})

{details}"""


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


def _update_progress_comment(
    context: GitHubContext,
    comment: CommentResult,
    *,
    success: bool,
    output_summary: str = "",
    branch_name: str = "",
    pr_url: str = "",
    error: str = "",
) -> None:
    """Update the progress comment with the final result."""
    if success:
        icon = "✅"
        status = "finished"
        details_parts = []
        if pr_url:
            details_parts.append(f"📝 Created PR: {pr_url}")
        if branch_name:
            details_parts.append(f"🌿 Branch: `{branch_name}`")
        if output_summary:
            details_parts.append(f"\n<details><summary>Output summary</summary>\n\n{output_summary}\n\n</details>")
        details = "\n".join(details_parts)
    else:
        icon = "❌"
        status = "encountered an error"
        details = ""
        if error:
            details = f"```\n{error[:3000]}\n```"

    body = _COMPLETE_TEMPLATE.format(
        icon=icon,
        status=status,
        actor=context.actor.login,
        run_url=context.run_url,
        details=details,
    )

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


def _commit_and_push(branch_name: str, issue_number: int | None = None) -> str | None:
    """If Vibe made changes, commit and push them.

    Returns the commit SHA if changes were pushed, None otherwise.
    """
    if not git_has_changes():
        logger.info("No changes to commit")
        return None

    # Build commit message
    msg = "feat: implement changes from Mistral agent"
    if issue_number:
        msg = f"feat: implement changes for #{issue_number}\n\nResolves #{issue_number}"

    sha = git_commit(msg)
    if sha:
        git_push(branch_name)
        logger.info("Pushed commit %s to %s", sha, branch_name)
    return sha


def _maybe_create_pr(
    context: GitHubContext,
    branch_name: str,
    base_branch: str,
) -> str:
    """Create a PR from the branch if one doesn't exist already.

    Returns the PR URL, or empty string if no PR was created.
    """
    entity = context.entity
    if not entity:
        return ""

    title = f"[Mistral] {entity.title}"
    body_lines = [
        f"Resolves #{entity.number}",
        "",
        "---",
        "",
        f"*Automated by [Mistral Action]({context.run_url}) triggered by @{context.actor.login}*",
    ]
    body = "\n".join(body_lines)

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

    try:
        if mode_result.mode == Mode.TAG:
            if context.entity and not context.entity.is_pull_request:
                # Issue: create new branch
                branch_name = _prepare_tag_mode_issue(context, branch_prefix, base_branch)
            elif context.entity and context.entity.is_pull_request:
                # PR: checkout the PR's branch
                branch_name = _prepare_tag_mode_pr(context)
        elif mode_result.mode == Mode.REVIEW:
            # Review: checkout PR branch (read-only, but may push fixes)
            if context.entity and context.entity.head_ref:
                branch_name = _prepare_tag_mode_pr(context)
        elif mode_result.mode == Mode.AGENT:
            # Agent mode: work on current branch or create one
            git_setup_identity()
            if context.entity and not context.entity.is_pull_request:
                branch_name = _prepare_tag_mode_issue(context, branch_prefix, base_branch)
            else:
                branch_name = os.environ.get("GITHUB_REF_NAME", "main")
    except Exception:
        logger.warning("Branch preparation failed, working on current HEAD", exc_info=True)

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

    # Phase 8: Handle results
    logger.info("Phase 8: Handling results")

    if vibe_result.conclusion == Conclusion.SUCCESS:
        # Check if Vibe made changes and commit/push them
        sha = _commit_and_push(
            branch_name,
            issue_number=context.entity.number if context.entity else None,
        )

        # Create PR if this was an issue-triggered run with changes
        if (
            sha
            and context.entity
            and not context.entity.is_pull_request
            and mode_result.mode in (Mode.TAG, Mode.AGENT)
        ):
            pr_url = _maybe_create_pr(context, branch_name, base_branch)
            if pr_url:
                _set_output("pr_url", pr_url)

    # Phase 9: Update progress comment
    logger.info("Phase 9: Updating progress comment")
    if progress_comment:
        # Build a short summary from Vibe's output
        output_summary = ""
        if vibe_result.output:
            # Take last ~2000 chars as summary
            out = vibe_result.output.strip()
            if len(out) > 2000:
                output_summary = "..." + out[-2000:]
            else:
                output_summary = out

        _update_progress_comment(
            context,
            progress_comment,
            success=vibe_result.conclusion == Conclusion.SUCCESS,
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
