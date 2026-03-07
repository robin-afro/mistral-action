"""Detect which mode the action should run in based on the GitHub event."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mistral_action.context import EntityType, EventType, GitHubContext


class Mode(Enum):
    TAG = "tag"  # Triggered by @mistralai mention in issue/PR comment
    REVIEW = "review"  # Triggered by PR open/sync for automatic review
    AGENT = "agent"  # Triggered by custom prompt (workflow_dispatch, schedule, etc.)
    SKIP = "skip"  # No action needed


@dataclass
class ModeResult:
    mode: Mode
    reason: str


def _check_trigger_in_text(text: str, trigger_phrase: str) -> bool:
    """Check if the trigger phrase appears in the text."""
    return trigger_phrase.lower() in text.lower()


def _check_assignee_trigger(context: GitHubContext, assignee_trigger: str) -> bool:
    """Check if the entity was assigned to the trigger user."""
    if not assignee_trigger or not context.entity:
        return False
    return assignee_trigger.lstrip("@").lower() in [
        a.lower() for a in context.entity.assignees
    ]


def _check_label_trigger(context: GitHubContext, label_trigger: str) -> bool:
    """Check if the entity has the trigger label."""
    if not label_trigger or not context.entity:
        return False
    return label_trigger.lower() in [lbl.lower() for lbl in context.entity.labels]


def detect_mode(
    context: GitHubContext,
    trigger_phrase: str = "@mistralai",
    assignee_trigger: str = "",
    label_trigger: str = "mistral",
    custom_prompt: str = "",
) -> ModeResult:
    """Detect the appropriate mode based on the GitHub event context.

    Priority:
    1. If a custom prompt is provided and this is a non-entity event, use agent mode.
    2. If this is an issue_comment or PR review comment with the trigger phrase, use tag mode.
    3. If this is an issue/PR opened/edited/labeled/assigned with the trigger, use tag mode.
    4. If this is a pull_request opened/synchronize/reopened, use review mode.
    5. If a custom prompt is provided (fallback), use agent mode.
    6. Otherwise, skip.
    """

    event = context.event_name
    action = context.action

    # --- Issue comment with trigger phrase ---
    if event == EventType.ISSUE_COMMENT and action == "created":
        if context.comment and _check_trigger_in_text(
            context.comment.body, trigger_phrase
        ):
            entity_kind = (
                "PR"
                if context.entity
                and context.entity.entity_type == EntityType.PULL_REQUEST
                else "issue"
            )
            return ModeResult(
                mode=Mode.TAG,
                reason=f"Trigger phrase '{trigger_phrase}' found in {entity_kind} comment",
            )
        return ModeResult(
            mode=Mode.SKIP,
            reason=f"Comment does not contain trigger phrase '{trigger_phrase}'",
        )

    # --- PR review comment with trigger phrase ---
    if event == EventType.PULL_REQUEST_REVIEW_COMMENT and action == "created":
        if context.comment and _check_trigger_in_text(
            context.comment.body, trigger_phrase
        ):
            return ModeResult(
                mode=Mode.TAG,
                reason=f"Trigger phrase '{trigger_phrase}' found in PR review comment",
            )
        return ModeResult(
            mode=Mode.SKIP,
            reason=f"Review comment does not contain trigger phrase '{trigger_phrase}'",
        )

    # --- Issue opened/edited/labeled/assigned with trigger ---
    if event == EventType.ISSUES and action in (
        "opened",
        "edited",
        "labeled",
        "assigned",
    ):
        if context.entity:
            # Check trigger in body
            if _check_trigger_in_text(context.entity.body, trigger_phrase):
                return ModeResult(
                    mode=Mode.TAG,
                    reason=f"Trigger phrase '{trigger_phrase}' found in issue body",
                )
            # Check assignee trigger
            if _check_assignee_trigger(context, assignee_trigger):
                return ModeResult(
                    mode=Mode.TAG,
                    reason=f"Issue assigned to '{assignee_trigger}'",
                )
            # Check label trigger
            if action == "labeled" and _check_label_trigger(context, label_trigger):
                return ModeResult(
                    mode=Mode.TAG,
                    reason=f"Label '{label_trigger}' added to issue",
                )
        return ModeResult(
            mode=Mode.SKIP,
            reason="Issue event does not match any trigger",
        )

    # --- PR opened/synchronize/reopened → automatic review ---
    if event == EventType.PULL_REQUEST and action in (
        "opened",
        "synchronize",
        "ready_for_review",
        "reopened",
    ):
        # Check if a custom prompt was provided (agent mode on PR)
        if custom_prompt:
            return ModeResult(
                mode=Mode.AGENT,
                reason="Custom prompt provided for pull_request event",
            )
        # Check label/assignee triggers
        if context.entity:
            if _check_label_trigger(context, label_trigger):
                return ModeResult(
                    mode=Mode.TAG,
                    reason=f"Label '{label_trigger}' found on PR",
                )
            if _check_assignee_trigger(context, assignee_trigger):
                return ModeResult(
                    mode=Mode.TAG,
                    reason=f"PR assigned to '{assignee_trigger}'",
                )
        return ModeResult(
            mode=Mode.REVIEW,
            reason=f"PR {action} — running automatic review",
        )

    # --- PR review submitted ---
    if event == EventType.PULL_REQUEST_REVIEW and action == "submitted":
        if context.comment and _check_trigger_in_text(
            context.comment.body, trigger_phrase
        ):
            return ModeResult(
                mode=Mode.TAG,
                reason=f"Trigger phrase '{trigger_phrase}' found in PR review body",
            )
        return ModeResult(
            mode=Mode.SKIP,
            reason="PR review does not contain trigger phrase",
        )

    # --- Non-entity events with a custom prompt → agent mode ---
    if event in (
        EventType.WORKFLOW_DISPATCH,
        EventType.SCHEDULE,
        EventType.PUSH,
    ):
        if custom_prompt:
            return ModeResult(
                mode=Mode.AGENT,
                reason=f"Custom prompt provided for {event.value} event",
            )
        return ModeResult(
            mode=Mode.SKIP,
            reason=f"No custom prompt provided for {event.value} event",
        )

    # --- Fallback: custom prompt provided for unknown event type ---
    if custom_prompt:
        return ModeResult(
            mode=Mode.AGENT,
            reason=f"Custom prompt provided (fallback) for {event.value} event",
        )

    return ModeResult(
        mode=Mode.SKIP,
        reason=f"Unhandled event type: {event.value}/{action}",
    )
