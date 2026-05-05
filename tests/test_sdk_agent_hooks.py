"""Unit tests for the ``build_hooks`` PreToolUse AskUserQuestion interceptor.

Post harness-audit Plan B Phase 1: the hook no longer routes the question
to the gateway. It denies the call with a redirect reason pointing the
agent at ``mcp__beidou__ask_user``, which routes through the leader chain
(``orchestrator.gateway_ask_via_chain``). This kills the hook-synth bug
class fixed in commit 82d5290 (envelope_missing) for the ask_user surface.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from beidou.sdk_agent import build_hooks


# ---------------------------------------------------------------------------
# Minimal fake orchestrator for hook tests.
# Only needs emit_event + gateway_ask_user.
# ---------------------------------------------------------------------------


class FakeOrchForHooks:
    """Minimal orchestrator stub for build_hooks unit tests."""

    def __init__(self, gateway_answer: str = "yes please") -> None:
        self.events: list[tuple[str, dict]] = []
        self._gateway_answer = gateway_answer
        self._gateway_called: bool = False
        self._gateway_calls: list[tuple[str, list, Optional[str]]] = []

    def emit_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    async def gateway_ask_user_structured(
        self,
        caller_id: str,
        questions: list,
        context: Optional[str],
    ) -> dict:
        self._gateway_called = True
        self._gateway_calls.append((caller_id, questions, context))
        return {
            "answers": [{"selected_labels": [], "text": self._gateway_answer}],
            "answer_text": self._gateway_answer,
        }

    # Legacy stub — kept so tests that happen to reference it don't crash,
    # but the SDK hook no longer calls it (m4g).
    async def gateway_ask_user(
        self,
        caller_id: str,
        question: str,
        context: Optional[str],
    ) -> str:
        return self._gateway_answer

    # report_status hook path — not used by ask_user tests but build_hooks
    # also closes over these; stubs prevent AttributeError if called.
    def assistant_text_for_turn(self, caller_id: str, tool_use_id: str) -> Optional[str]:
        return None

    def deliver_message(self, **kwargs: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Helper: get the PreToolUse AskUserQuestion hook callback from build_hooks.
# ---------------------------------------------------------------------------


def _get_pretooluse_hook(orch: FakeOrchForHooks, caller_id: str = "test_agent", leader_id: str = "leader1"):
    hooks = build_hooks(orch, caller_id=caller_id, leader_id=leader_id)
    matchers = hooks.get("PreToolUse", [])
    assert matchers, "build_hooks returned no PreToolUse matchers"
    # The first matcher should be AskUserQuestion.
    matcher = matchers[0]
    hook_fns = matcher.hooks
    assert hook_fns, "HookMatcher has no hooks"
    return hook_fns[0]


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


class TestAskUserQuestionHook:
    """Tests for the PreToolUse AskUserQuestion interceptor hook."""

    def test_hook_present_in_build_hooks(self) -> None:
        """build_hooks includes PreToolUse entries: AskUserQuestion + SendMessage +
        TodoWrite redirects + reply_gate + review_gate (both match-all)."""
        orch = FakeOrchForHooks()
        hooks = build_hooks(orch, caller_id="agent1", leader_id="leader1")
        assert "PreToolUse" in hooks, "No PreToolUse key in hooks dict"
        matchers = hooks["PreToolUse"]
        # 5 PreToolUse matchers after Phase 2: AskUserQuestion + SendMessage +
        # TodoWrite (each named) + reply_gate + review_gate (both match-all).
        assert len(matchers) == 5
        matcher_names = [m.matcher for m in matchers]
        assert "AskUserQuestion" in matcher_names, f"Expected AskUserQuestion in {matcher_names}"
        assert "SendMessage" in matcher_names, f"Expected SendMessage in {matcher_names}"
        assert "TodoWrite" in matcher_names, f"Expected TodoWrite in {matcher_names}"
        assert matcher_names.count(None) == 2, f"Expected 2 match-all entries in {matcher_names}"

    def test_posttooluse_still_present(self) -> None:
        """build_hooks includes PostToolUse hooks for report_status, signal_review, and send_message."""
        orch = FakeOrchForHooks()
        hooks = build_hooks(orch, caller_id="agent1", leader_id="leader1")
        assert "PostToolUse" in hooks
        matchers = hooks["PostToolUse"]
        assert len(matchers) == 3
        matcher_vals = [m.matcher for m in matchers]
        assert "mcp__beidou__report_status" in matcher_vals
        assert "mcp__beidou__signal_review" in matcher_vals
        assert "mcp__beidou__send_message" in matcher_vals

    def test_redirects_with_questions(self) -> None:
        """Non-empty questions list: deny with redirect reason; gateway NEVER called."""
        orch = FakeOrchForHooks(gateway_answer="should not be used")
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {
                        "question": "Color?",
                        "header": "Color",
                        "options": [{"label": "Red", "description": "rosy"}],
                    }
                ]
            },
        }

        result = asyncio.run(hook(input_data, tool_use_id="toolu_fake_123", context=None))

        # Gateway must NOT be called from this hook anymore.
        assert not orch._gateway_called, "gateway_ask_user_structured must not be invoked"

        hs_out = result.get("hookSpecificOutput", {})
        assert hs_out.get("permissionDecision") == "deny"
        assert hs_out.get("hookEventName") == "PreToolUse"
        reason = hs_out.get("permissionDecisionReason", "")
        # Must redirect to the canonical primitive and reference the spec.
        assert "mcp__beidou__ask_user" in reason
        assert "docs/tool-surface.md#ask_user" in reason
        assert "leader chain" in reason

    def test_no_synth_tool_pair_emitted(self) -> None:
        """The hook must NOT emit synthetic tool_called/tool_result events."""
        orch = FakeOrchForHooks()
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Color?", "header": "Color", "options": []}]},
        }
        asyncio.run(hook(input_data, tool_use_id="toolu_abc", context=None))

        event_names = [name for name, _ in orch.events]
        assert "tool_called" not in event_names
        assert "tool_result" not in event_names
        # A redirect telemetry event IS expected so observability still sees the call.
        assert event_names.count("ask_user_question.redirected") == 1

    def test_redirect_event_payload(self) -> None:
        """The ask_user_question.redirected event records caller and question count."""
        orch = FakeOrchForHooks()
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {"question": "Q1?", "header": "h1", "options": []},
                    {"question": "Q2?", "header": "h2", "options": []},
                ],
            },
        }
        asyncio.run(hook(input_data, tool_use_id=None, context=None))

        payload = next(p for name, p in orch.events if name == "ask_user_question.redirected")
        assert payload["caller_id"] == "test_agent"
        assert payload["questions_count"] == 2

    def test_negative_empty_questions_list(self) -> None:
        """Empty questions list: gateway NOT called; deny with guidance message."""
        orch = FakeOrchForHooks(gateway_answer="should not be used")
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": []},
        }
        result = asyncio.run(hook(input_data, tool_use_id=None, context=None))

        # Gateway must NOT have been called.
        assert not orch._gateway_called, "gateway_ask_user was called despite empty questions"

        # Should still deny with a helpful message.
        hs_out = result.get("hookSpecificOutput", {})
        assert hs_out.get("permissionDecision") == "deny"
        reason = hs_out.get("permissionDecisionReason", "")
        assert "AskUserQuestion received no questions" in reason
        assert "mcp__beidou__ask_user" in reason

    def test_negative_missing_questions_key(self) -> None:
        """Missing 'questions' key: gateway NOT called; deny with guidance message."""
        orch = FakeOrchForHooks(gateway_answer="should not be used")
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {},  # no 'questions' key
        }
        result = asyncio.run(hook(input_data, tool_use_id=None, context=None))

        assert not orch._gateway_called
        hs_out = result.get("hookSpecificOutput", {})
        assert hs_out.get("permissionDecision") == "deny"
        assert "AskUserQuestion received no questions" in hs_out.get("permissionDecisionReason", "")

    def test_wrong_tool_name_returns_empty(self) -> None:
        """Defensive guard: wrong tool_name returns {} without calling gateway."""
        orch = FakeOrchForHooks()
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "SomeOtherTool",
            "tool_input": {"questions": [{"question": "Color?"}]},
        }
        result = asyncio.run(hook(input_data, tool_use_id=None, context=None))

        assert result == {}
        assert not orch._gateway_called

    def test_gateway_never_invoked_even_when_present(self) -> None:
        """Sanity: even if the orchestrator exposes gateway_ask_user_structured,
        the hook must not call it. The agent's redirect to mcp__beidou__ask_user
        is the only path that should reach the gateway (via leader chain).
        """
        orch = FakeOrchForHooks(gateway_answer="leak")
        hook = _get_pretooluse_hook(orch)

        for q in (
            {"question": "Q1?", "header": "h", "options": []},
            {"question": "Q2?", "header": "h2", "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]},
        ):
            asyncio.run(
                hook(
                    {"tool_name": "AskUserQuestion", "tool_input": {"questions": [q]}},
                    tool_use_id=None,
                    context=None,
                )
            )

        assert not orch._gateway_called
        assert len(orch._gateway_calls) == 0
