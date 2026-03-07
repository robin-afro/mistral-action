"""Tests for mode detection — validates all GitHub event types route to the correct mode."""

from __future__ import annotations

import pytest

from mistral_action.context import (
    Actor,
    Comment,
    Entity,
    EntityType,
    EventType,
    GitHubContext,
    Repository,
)
from mistral_action.modes import Mode, detect_mode


def _make_context(
    event_name: EventType = EventType.ISSUE_COMMENT,
    action: str = "created",
    entity_type: EntityType = EntityType.ISSUE,
    entity_body: str = "",
    entity_labels: list[str] | None = None,
    entity_assignees: list[str] | None = None,
    entity_head_ref: str = "",
    comment_body: str = "",
    has_entity: bool = True,
    has_comment: bool = True,
) -> GitHubContext:
    actor = Actor(login="testuser", id=1, type="User")
    repo = Repository(
        owner="owner",
        name="repo",
        full_name="owner/repo",
        default_branch="main",
    )
    entity = None
    if has_entity:
        entity = Entity(
            entity_type=entity_type,
            number=42,
            title="Test entity",
            body=entity_body,
            labels=entity_labels or [],
            assignees=entity_assignees or [],
            head_ref=entity_head_ref,
            base_ref="main",
        )
    comment = None
    if has_comment:
        comment = Comment(
            id=100,
            body=comment_body,
            user=actor,
        )
    return GitHubContext(
        event_name=event_name,
        action=action,
        actor=actor,
        repository=repo,
        entity=entity,
        comment=comment,
        run_id="12345",
        run_url="https://github.com/owner/repo/actions/runs/12345",
        server_url="https://github.com",
        api_url="https://api.github.com",
        raw_event={},
    )


# ---------------------------------------------------------------------------
# Tag mode — issue comment
# ---------------------------------------------------------------------------


class TestIssueCommentTagMode:
    def test_trigger_phrase_in_comment(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            comment_body="@mistralai please fix this bug",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG

    def test_trigger_phrase_case_insensitive(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            comment_body="@Mistralai review this code",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG

    def test_custom_trigger_phrase(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            comment_body="/ai do this thing",
        )
        result = detect_mode(ctx, trigger_phrase="/ai")
        assert result.mode == Mode.TAG

    def test_no_trigger_skips(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            comment_body="This is a normal comment without any trigger",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.SKIP

    def test_trigger_on_pr_comment(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            entity_type=EntityType.PULL_REQUEST,
            comment_body="@mistralai fix the tests",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG
        assert "PR" in result.reason

    def test_trigger_on_issue_comment(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            entity_type=EntityType.ISSUE,
            comment_body="@mistralai implement this",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG
        assert "issue" in result.reason


# ---------------------------------------------------------------------------
# Tag mode — PR review comment
# ---------------------------------------------------------------------------


class TestPRReviewCommentTagMode:
    def test_trigger_in_review_comment(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST_REVIEW_COMMENT,
            action="created",
            entity_type=EntityType.PULL_REQUEST,
            comment_body="@mistralai this variable name is wrong",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG

    def test_no_trigger_in_review_comment_skips(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST_REVIEW_COMMENT,
            action="created",
            entity_type=EntityType.PULL_REQUEST,
            comment_body="Good catch, let me fix that",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.SKIP


# ---------------------------------------------------------------------------
# Tag mode — issue events
# ---------------------------------------------------------------------------


class TestIssueEventTagMode:
    def test_trigger_in_issue_body_on_open(self):
        ctx = _make_context(
            event_name=EventType.ISSUES,
            action="opened",
            entity_body="@mistralai please implement this feature",
            has_comment=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG

    def test_trigger_in_issue_body_on_edit(self):
        ctx = _make_context(
            event_name=EventType.ISSUES,
            action="edited",
            entity_body="@mistralai updated requirements",
            has_comment=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG

    def test_assignee_trigger(self):
        ctx = _make_context(
            event_name=EventType.ISSUES,
            action="assigned",
            entity_assignees=["mistral-bot"],
            has_comment=False,
        )
        result = detect_mode(ctx, assignee_trigger="mistral-bot")
        assert result.mode == Mode.TAG

    def test_assignee_trigger_with_at_prefix(self):
        ctx = _make_context(
            event_name=EventType.ISSUES,
            action="assigned",
            entity_assignees=["mistral-bot"],
            has_comment=False,
        )
        result = detect_mode(ctx, assignee_trigger="@mistral-bot")
        assert result.mode == Mode.TAG

    def test_label_trigger(self):
        ctx = _make_context(
            event_name=EventType.ISSUES,
            action="labeled",
            entity_labels=["mistral", "bug"],
            has_comment=False,
        )
        result = detect_mode(ctx, label_trigger="mistral")
        assert result.mode == Mode.TAG

    def test_no_trigger_in_issue_skips(self):
        ctx = _make_context(
            event_name=EventType.ISSUES,
            action="opened",
            entity_body="Just a normal issue without any AI trigger",
            has_comment=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.SKIP

    def test_label_trigger_only_on_labeled_action(self):
        """Label trigger should only fire on 'labeled' action, not 'opened'."""
        ctx = _make_context(
            event_name=EventType.ISSUES,
            action="opened",
            entity_labels=["mistral"],
            entity_body="Normal issue body",
            has_comment=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai", label_trigger="mistral")
        assert result.mode == Mode.SKIP


# ---------------------------------------------------------------------------
# Review mode — PR events
# ---------------------------------------------------------------------------


class TestReviewMode:
    def test_pr_opened(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST,
            action="opened",
            entity_type=EntityType.PULL_REQUEST,
            entity_head_ref="feature-branch",
            has_comment=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.REVIEW

    def test_pr_synchronize(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST,
            action="synchronize",
            entity_type=EntityType.PULL_REQUEST,
            entity_head_ref="feature-branch",
            has_comment=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.REVIEW

    def test_pr_reopened(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST,
            action="reopened",
            entity_type=EntityType.PULL_REQUEST,
            entity_head_ref="feature-branch",
            has_comment=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.REVIEW

    def test_pr_ready_for_review(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST,
            action="ready_for_review",
            entity_type=EntityType.PULL_REQUEST,
            entity_head_ref="feature-branch",
            has_comment=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.REVIEW

    def test_pr_with_custom_prompt_uses_agent_mode(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST,
            action="opened",
            entity_type=EntityType.PULL_REQUEST,
            entity_head_ref="feature-branch",
            has_comment=False,
        )
        result = detect_mode(ctx, custom_prompt="Run the full test suite")
        assert result.mode == Mode.AGENT

    def test_pr_with_label_trigger_uses_tag_mode(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST,
            action="opened",
            entity_type=EntityType.PULL_REQUEST,
            entity_head_ref="feature-branch",
            entity_labels=["mistral"],
            has_comment=False,
        )
        result = detect_mode(ctx, label_trigger="mistral")
        assert result.mode == Mode.TAG


# ---------------------------------------------------------------------------
# Agent mode
# ---------------------------------------------------------------------------


class TestAgentMode:
    def test_workflow_dispatch_with_prompt(self):
        ctx = _make_context(
            event_name=EventType.WORKFLOW_DISPATCH,
            action="",
            has_entity=False,
            has_comment=False,
        )
        result = detect_mode(ctx, custom_prompt="Refactor the auth module")
        assert result.mode == Mode.AGENT

    def test_workflow_dispatch_without_prompt_skips(self):
        ctx = _make_context(
            event_name=EventType.WORKFLOW_DISPATCH,
            action="",
            has_entity=False,
            has_comment=False,
        )
        result = detect_mode(ctx)
        assert result.mode == Mode.SKIP

    def test_schedule_with_prompt(self):
        ctx = _make_context(
            event_name=EventType.SCHEDULE,
            action="",
            has_entity=False,
            has_comment=False,
        )
        result = detect_mode(ctx, custom_prompt="Update dependencies")
        assert result.mode == Mode.AGENT

    def test_push_with_prompt(self):
        ctx = _make_context(
            event_name=EventType.PUSH,
            action="",
            has_entity=False,
            has_comment=False,
        )
        result = detect_mode(ctx, custom_prompt="Check for regressions")
        assert result.mode == Mode.AGENT

    def test_unknown_event_with_prompt_falls_back_to_agent(self):
        ctx = _make_context(
            event_name=EventType.UNKNOWN,
            action="",
            has_entity=False,
            has_comment=False,
        )
        result = detect_mode(ctx, custom_prompt="Do something")
        assert result.mode == Mode.AGENT

    def test_unknown_event_without_prompt_skips(self):
        ctx = _make_context(
            event_name=EventType.UNKNOWN,
            action="",
            has_entity=False,
            has_comment=False,
        )
        result = detect_mode(ctx)
        assert result.mode == Mode.SKIP


# ---------------------------------------------------------------------------
# PR review submitted
# ---------------------------------------------------------------------------


class TestPRReviewSubmitted:
    def test_review_with_trigger(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST_REVIEW,
            action="submitted",
            entity_type=EntityType.PULL_REQUEST,
            comment_body="@mistralai can you look at the edge cases?",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG

    def test_review_without_trigger_skips(self):
        ctx = _make_context(
            event_name=EventType.PULL_REQUEST_REVIEW,
            action="submitted",
            entity_type=EntityType.PULL_REQUEST,
            comment_body="LGTM, looks good to me",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.SKIP


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_comment_body(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            comment_body="",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.SKIP

    def test_trigger_phrase_as_substring(self):
        """Trigger should match even if it's part of a larger word/sentence."""
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            comment_body="Hey @mistralai, fix the tests",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG

    def test_no_entity_on_issue_comment(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            comment_body="@mistralai hello",
            has_entity=False,
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.mode == Mode.TAG

    def test_mode_result_has_reason(self):
        ctx = _make_context(
            event_name=EventType.ISSUE_COMMENT,
            action="created",
            comment_body="@mistralai implement this",
        )
        result = detect_mode(ctx, trigger_phrase="@mistralai")
        assert result.reason
        assert len(result.reason) > 0
