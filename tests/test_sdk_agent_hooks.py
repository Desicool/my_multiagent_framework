"""Unit tests for the ``build_hooks`` PreToolUse AskUserQuestion interceptor.

Tests the ``on_ask_user_question`` hook that intercepts raw ``AskUserQuestion``
tool calls (emitted by models using the Anthropic-compatible proxy, e.g.
MiniMax-M2.7) and routes them to the human gateway.
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
        """build_hooks includes PreToolUse entries: AskUserQuestion + review_gate (match-all)."""
        orch = FakeOrchForHooks()
        hooks = build_hooks(orch, caller_id="agent1", leader_id="leader1")
        assert "PreToolUse" in hooks, "No PreToolUse key in hooks dict"
        matchers = hooks["PreToolUse"]
        # 2 PreToolUse matchers: AskUserQuestion hook (be3) + match-all review gate (be3).
        assert len(matchers) == 2
        matcher_names = [m.matcher for m in matchers]
        assert "AskUserQuestion" in matcher_names, f"Expected AskUserQuestion in {matcher_names}"
        assert None in matcher_names, f"Expected match-all (None) in {matcher_names}"

    def test_posttooluse_still_present(self) -> None:
        """build_hooks still includes the PostToolUse report_status hook."""
        orch = FakeOrchForHooks()
        hooks = build_hooks(orch, caller_id="agent1", leader_id="leader1")
        assert "PostToolUse" in hooks
        matchers = hooks["PostToolUse"]
        assert len(matchers) == 1
        assert matchers[0].matcher == "mcp__beidou__report_status"

    def test_positive_single_question(self) -> None:
        """Hook routes question to gateway and returns answer as deny reason."""
        orch = FakeOrchForHooks(gateway_answer="yes please")
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

        # Should deny with the user's answer.
        hs_out = result.get("hookSpecificOutput", {})
        assert hs_out.get("permissionDecision") == "deny"
        assert hs_out.get("permissionDecisionReason") == "yes please"
        assert hs_out.get("hookEventName") == "PreToolUse"

    def test_gateway_called_with_correct_args(self) -> None:
        """Gateway receives the raw questions array unchanged (no flattening after m4g)."""
        orch = FakeOrchForHooks(gateway_answer="blue")
        hook = _get_pretooluse_hook(orch)

        questions_in = [
            {
                "question": "Favourite color?",
                "header": "Color",
                "options": [
                    {"label": "Red", "description": "rosy"},
                    {"label": "Blue", "description": "calm"},
                ],
            }
        ]
        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": questions_in},
        }

        asyncio.run(hook(input_data, tool_use_id=None, context=None))

        assert orch._gateway_called
        assert len(orch._gateway_calls) == 1
        caller_id, questions_received, context_received = orch._gateway_calls[0]
        assert caller_id == "test_agent"
        # The hook must NOT flatten — it passes the questions list through unchanged.
        assert questions_received == questions_in
        # No context argument in the input_data, so None is passed.
        assert context_received is None

    def test_two_synthetic_events_emitted(self) -> None:
        """emit_event is called exactly twice: tool_called then tool_result."""
        orch = FakeOrchForHooks(gateway_answer="yes please")
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [{"question": "Color?", "header": "Color", "options": []}]
            },
        }
        asyncio.run(hook(input_data, tool_use_id="toolu_abc", context=None))

        event_names = [name for name, _ in orch.events]
        assert event_names.count("tool_called") == 1
        assert event_names.count("tool_result") == 1

        # tool_called before tool_result.
        called_idx = event_names.index("tool_called")
        result_idx = event_names.index("tool_result")
        assert called_idx < result_idx

    def test_tool_result_event_is_not_error_on_success(self) -> None:
        """tool_result has is_error=False when gateway succeeds."""
        orch = FakeOrchForHooks(gateway_answer="fine")
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "OK?", "header": "", "options": []}]},
        }
        asyncio.run(hook(input_data, tool_use_id=None, context=None))

        tool_result_payload = next(p for name, p in orch.events if name == "tool_result")
        assert tool_result_payload["is_error"] is False

    def test_synthetic_id_used_not_original(self) -> None:
        """The emitted events use a generated id, not the original tool_use_id."""
        orch = FakeOrchForHooks(gateway_answer="fine")
        hook = _get_pretooluse_hook(orch)

        original_id = "toolu_original_xyz"
        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "OK?", "header": "", "options": []}]},
        }
        asyncio.run(hook(input_data, tool_use_id=original_id, context=None))

        emitted_ids = {p["tool_use_id"] for _, p in orch.events}
        # Must have exactly one synthetic id, which is NOT the original.
        assert len(emitted_ids) == 1
        synthetic_id = next(iter(emitted_ids))
        assert synthetic_id != original_id
        assert synthetic_id.startswith("hook_askuserquestion_")

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

    def test_gateway_exception_sets_is_error_true(self) -> None:
        """When gateway raises, tool_result has is_error=True and answer is error string."""

        class ErrorOrch(FakeOrchForHooks):
            async def gateway_ask_user_structured(self, caller_id, questions, context):
                raise RuntimeError("simulated gateway failure")

        orch = ErrorOrch()
        hook = _get_pretooluse_hook(orch)

        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "OK?", "header": "", "options": []}]},
        }
        result = asyncio.run(hook(input_data, tool_use_id=None, context=None))

        # tool_result must have is_error=True.
        tool_result_payload = next(p for name, p in orch.events if name == "tool_result")
        assert tool_result_payload["is_error"] is True

        # The deny reason should contain the error description.
        hs_out = result.get("hookSpecificOutput", {})
        assert "RuntimeError" in hs_out.get("permissionDecisionReason", "")

    def test_multi_question_passthrough(self) -> None:
        """Multiple questions are passed to gateway unchanged — no composite flattening."""
        orch = FakeOrchForHooks(gateway_answer="both answered")
        hook = _get_pretooluse_hook(orch)

        questions_in = [
            {"question": "First question?", "header": "First", "options": []},
            {"question": "Second question?", "header": "Second", "options": []},
        ]
        input_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": questions_in},
        }
        asyncio.run(hook(input_data, tool_use_id=None, context=None))

        assert orch._gateway_called
        _, questions_received, _ = orch._gateway_calls[0]
        # Both sub-questions must arrive as-is.
        assert questions_received == questions_in
