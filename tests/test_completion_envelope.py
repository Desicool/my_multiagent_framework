"""Tests for the completion handoff contract in on_signal_review.

After the envelope-at-primitive change, the hook is a strict pass-through:
- The primitive validates [REVIEW REQUIRED] before the hook sees the call.
- On is_error=True (primitive rejected), the hook is a no-op.
- On is_error=False, the hook reads detail verbatim and delivers it.
- review_pending is set at the END of the success path.

bd issue k1q / plan tsk-1cac0370:
  Phase 1: validation moved to primitive; Phase 2: hook stripped of synth.

Tests:
- test_success_path_delivers_detail_verbatim: enveloped detail → delivered unchanged,
  review_pending=True at end, no envelope_synthesized event.
- test_tool_errored_is_strict_noop: is_error=True → no delivery, no state mutation.
- test_empty_detail_still_delivers_and_sets_pending: empty detail → still delivers and sets pending.
- test_root_routes_through_user_gateway_approve: root → gateway question, approve → terminate_root.
- test_root_routes_through_user_gateway_rework: root → gateway question, rework → rework message.
- test_review_pending_not_set_before_delivery_succeeds: pending only set at end.
- test_root_tool_errored_gateway_not_called: is_error=True on root → no gateway call.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Any, Optional

import pytest

from beidou.orchestrator import USER_SENTINEL
from beidou.sdk_agent import build_hooks


# ---------------------------------------------------------------------------
# Fake AgentRecord — minimal fields needed by on_signal_review.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FakeAgentRecord:
    skill_name: str = "test_skill"
    review_pending: bool = False
    review_pending_ts: Optional[float] = None
    last_progress_ts: float = dataclasses.field(default_factory=time.time)
    pending_replies: dict = dataclasses.field(default_factory=dict)
    reply_gate_active: bool = False
    idle_nudge_count: int = 0


# ---------------------------------------------------------------------------
# Fake orchestrator stub for envelope tests.
# ---------------------------------------------------------------------------


class FakeOrchForEnvelope:
    """Minimal orchestrator stub for completion envelope tests.

    Exposes:
    - emit_event(): records (name, payload) pairs.
    - assistant_text_for_turn(): returns configurable text (kept for
      compatibility with other callers; hook no longer calls it).
    - deliver_message(): records calls.
    - _agents: dict keyed by caller_id, values are FakeAgentRecord.
    - gateway_ask_user_structured(): returns a configurable canned answer and
      records the call args.
    - terminate_root(): records that it was called.
    """

    def __init__(
        self,
        *,
        assistant_text: Optional[str] = None,
        agent_id: str = "ag_child",
        skill_name: str = "junior_engineer",
        gateway_answer: Optional[dict] = None,
        gateway_raises: Optional[BaseException] = None,
    ) -> None:
        self.events: list[tuple[str, dict]] = []
        self._assistant_text = assistant_text
        self._delivered: list[dict] = []
        self._gateway_calls: list[dict] = []
        self._gateway_answer = gateway_answer
        self._gateway_raises = gateway_raises
        self.terminate_root_called: int = 0

        # Seed the agents dict with the caller's record.
        self._agents: dict[str, FakeAgentRecord] = {
            agent_id: FakeAgentRecord(skill_name=skill_name),
        }
        self._agent_id = agent_id

    def emit_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def assistant_text_for_turn(self, caller_id: str, tool_use_id: str) -> Optional[str]:
        return self._assistant_text

    def deliver_message(self, *, from_id: str, to_id: str, body: str, kind: str = "user") -> None:
        self._delivered.append({"from_id": from_id, "to_id": to_id, "body": body, "kind": kind})

    async def gateway_ask_user_structured(
        self, caller_id: str, questions: list[dict], context: Optional[str]
    ) -> dict:
        self._gateway_calls.append(
            {"caller_id": caller_id, "questions": questions, "context": context}
        )
        if self._gateway_raises is not None:
            raise self._gateway_raises
        return self._gateway_answer or {"answers": [{"selected_values": ["approve"], "selected_labels": ["Approve"], "text": None}]}

    async def terminate_root(self) -> None:
        self.terminate_root_called += 1

    # Convenience helpers for assertions.
    def events_named(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]

    def delivered_bodies(self) -> list[str]:
        return [d["body"] for d in self._delivered]


# ---------------------------------------------------------------------------
# Helper to extract the PostToolUse on_signal_review hook.
# ---------------------------------------------------------------------------


def _get_posttooluse_hook(
    orch: FakeOrchForEnvelope,
    caller_id: str,
    leader_id: str,
):
    hooks = build_hooks(orch, caller_id=caller_id, leader_id=leader_id)  # type: ignore[arg-type]
    matchers = hooks.get("PostToolUse", [])
    assert matchers, "build_hooks returned no PostToolUse matchers"
    matcher = matchers[0]
    assert matcher.matcher == "mcp__beidou__signal_review"
    return matcher.hooks[0]


# ---------------------------------------------------------------------------
# Shared input_data factory.
# ---------------------------------------------------------------------------

_ENVELOPE = (
    "[REVIEW REQUIRED]\nrole=junior_engineer     agent=ag_child\n"
    "Deliverables: done.\nOpen questions / risks: none\n"
    "Leader action required: approve (terminate_child) OR rework (send_message)"
)


def _make_input(
    detail: Optional[str] = None,
    is_error: bool = False,
) -> dict:
    tool_input: dict[str, Any] = {}
    if detail is not None:
        tool_input["detail"] = detail
    return {
        "tool_name": "mcp__beidou__signal_review",
        "tool_input": tool_input,
        "tool_response": {"is_error": is_error},
    }


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


class TestCompletionHandoff:
    """Tests for the simplified on_signal_review hook (post-primitive-validation)."""

    def test_success_path_delivers_detail_verbatim(self) -> None:
        """Enveloped detail arrives with is_error=False → hook delivers it unchanged,
        no envelope_synthesized event, review_pending=True at end."""
        orch = FakeOrchForEnvelope(agent_id="ag_child", skill_name="junior_engineer")
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")

        asyncio.run(hook(_make_input(detail=_ENVELOPE), tool_use_id="toolu_1", context=None))

        # No synthesis event.
        assert orch.events_named("completion.envelope_synthesized") == []

        # Detail delivered verbatim.
        bodies = orch.delivered_bodies()
        assert len(bodies) == 1
        assert bodies[0] == _ENVELOPE

        # review_pending set at end of success path.
        rec = orch._agents["ag_child"]
        assert rec.review_pending is True
        assert rec.review_pending_ts is not None
        assert rec.last_progress_ts > 0

        # completion.reported emitted.
        reported = orch.events_named("completion.reported")
        assert len(reported) == 1
        assert reported[0]["agent_id"] == "ag_child"
        assert reported[0]["leader_id"] == "ag_leader"
        assert reported[0]["via"] == "hook"

    def test_tool_errored_is_strict_noop(self) -> None:
        """When is_error=True (primitive rejected the call), hook must be a no-op:
        no delivery, no completion.reported, no state mutation, no completion.envelope_synthesized."""
        orch = FakeOrchForEnvelope(agent_id="ag_child")
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")
        rec = orch._agents["ag_child"]
        initial_pending = rec.review_pending

        asyncio.run(
            hook(_make_input(detail="all good", is_error=True), tool_use_id="toolu_2", context=None)
        )

        # No delivery.
        assert orch.delivered_bodies() == []
        # No events at all.
        assert orch.events == []
        # State NOT mutated.
        assert rec.review_pending == initial_pending

    def test_empty_detail_still_delivers_and_sets_pending(self) -> None:
        """Agent calls signal_review with empty detail → still delivers to leader and sets pending."""
        orch = FakeOrchForEnvelope(agent_id="ag_child")
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")

        asyncio.run(
            hook(_make_input(detail=""), tool_use_id="toolu_3", context=None)
        )

        rec = orch._agents["ag_child"]
        assert rec.review_pending is True
        assert rec.review_pending_ts is not None
        # Even empty detail is delivered to the leader.
        bodies = orch.delivered_bodies()
        assert len(bodies) == 1
        assert bodies[0] == ""

    def test_root_tool_errored_gateway_not_called(self) -> None:
        """When primitive rejected (is_error=True) on a root agent, gateway is NOT called."""
        orch = FakeOrchForEnvelope(agent_id="ag_root")
        hook = _get_posttooluse_hook(orch, caller_id="ag_root", leader_id=USER_SENTINEL)

        asyncio.run(
            hook(_make_input(detail="no envelope", is_error=True), tool_use_id="toolu_4", context=None)
        )

        assert len(orch._gateway_calls) == 0
        assert orch.terminate_root_called == 0
        assert orch.delivered_bodies() == []
        assert orch.events == []

    def test_root_routes_through_user_gateway_approve(self) -> None:
        """Root agent (leader_id == USER_SENTINEL) → gateway question; approve → terminate_root,
        review_pending=True set at end of success path."""
        orch = FakeOrchForEnvelope(
            agent_id="ag_root",
            gateway_answer={
                "answers": [{"selected_values": ["approve"], "selected_labels": ["Approve"], "text": None}],
                "answer_text": "Approve",
            },
        )
        hook = _get_posttooluse_hook(orch, caller_id="ag_root", leader_id=USER_SENTINEL)

        asyncio.run(
            hook(_make_input(detail=_ENVELOPE), tool_use_id="toolu_5", context=None)
        )

        # Gateway was asked.
        assert len(orch._gateway_calls) == 1
        call = orch._gateway_calls[0]
        assert call["caller_id"] == "ag_root"
        questions = call["questions"]
        assert len(questions) == 1
        opts = questions[0]["options"]
        values = [o.get("value") for o in opts]
        assert "approve" in values and "rework" in values
        rework_opt = next(o for o in opts if o.get("value") == "rework")
        assert rework_opt.get("requires_text") is True

        # terminate_root was called once.
        assert orch.terminate_root_called == 1
        # No rework message delivered.
        assert orch.delivered_bodies() == []
        # review_pending was set (at end of success path).
        rec = orch._agents.get("ag_root")
        assert rec is not None and rec.review_pending is True
        # completion.reported emitted with decision=approve.
        reported = orch.events_named("completion.reported")
        assert len(reported) == 1 and reported[0]["decision"] == "approve"

    def test_root_routes_through_user_gateway_rework(self) -> None:
        """Root agent → gateway question; rework → rework message delivered, no terminate."""
        orch = FakeOrchForEnvelope(
            agent_id="ag_root",
            gateway_answer={
                "answers": [{"selected_values": ["rework"], "selected_labels": ["Rework"], "text": "fix the typo"}],
                "answer_text": "fix the typo",
            },
        )
        hook = _get_posttooluse_hook(orch, caller_id="ag_root", leader_id=USER_SENTINEL)

        asyncio.run(
            hook(_make_input(detail=_ENVELOPE), tool_use_id="toolu_6", context=None)
        )

        assert orch.terminate_root_called == 0
        bodies = orch.delivered_bodies()
        assert len(bodies) == 1
        assert bodies[0].startswith("rework: ")
        assert "fix the typo" in bodies[0]
        reported = orch.events_named("completion.reported")
        assert len(reported) == 1 and reported[0]["decision"] == "rework"
        # review_pending set.
        rec = orch._agents.get("ag_root")
        assert rec is not None and rec.review_pending is True

    def test_root_terminal_freetext_approve_still_terminates(self) -> None:
        """TerminalGateway sends selected_labels=[] with the typed text.
        A typed 'approve' / 'yes' must still terminate the root."""
        orch = FakeOrchForEnvelope(
            agent_id="ag_root",
            gateway_answer={
                "answers": [{"selected_labels": [], "text": "approve"}],
                "answer_text": "approve",
            },
        )
        hook = _get_posttooluse_hook(orch, caller_id="ag_root", leader_id=USER_SENTINEL)

        asyncio.run(
            hook(_make_input(detail=_ENVELOPE), tool_use_id="toolu_term", context=None)
        )

        assert orch.terminate_root_called == 1
        assert orch.delivered_bodies() == []
        reported = orch.events_named("completion.reported")
        assert reported and reported[-1]["decision"] == "approve"

    def test_root_gateway_failure_falls_back_to_completion_empty(self) -> None:
        """If the gateway raises, on_signal_review emits completion.empty(gateway_failure: ...)
        and review_pending must NOT be set (failure before success path end)."""
        orch = FakeOrchForEnvelope(
            agent_id="ag_root",
            gateway_raises=RuntimeError("gateway down"),
        )
        hook = _get_posttooluse_hook(orch, caller_id="ag_root", leader_id=USER_SENTINEL)

        asyncio.run(
            hook(_make_input(detail=_ENVELOPE), tool_use_id="toolu_7", context=None)
        )

        assert orch.terminate_root_called == 0
        empty = orch.events_named("completion.empty")
        assert len(empty) == 1
        assert empty[0]["reason"].startswith("gateway_failure:")
        # Gateway raised before the success path end → review_pending not set.
        rec = orch._agents.get("ag_root")
        assert rec is not None and rec.review_pending is False

    def test_review_pending_not_set_if_delivery_raises(self) -> None:
        """State mutation order: review_pending only set after delivery succeeds.
        If deliver_message raises, agent must NOT be left with review_pending=True."""

        class RaisingOrch(FakeOrchForEnvelope):
            def deliver_message(self, **kwargs):
                raise RuntimeError("delivery failed")

        orch = RaisingOrch(agent_id="ag_child")
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")
        rec = orch._agents["ag_child"]

        with pytest.raises(RuntimeError, match="delivery failed"):
            asyncio.run(hook(_make_input(detail=_ENVELOPE), tool_use_id="toolu_8", context=None))

        # review_pending must NOT have been set before the raise.
        assert rec.review_pending is False

    def test_pending_replies_cleared_on_success(self) -> None:
        """Pending reply obligations are cleared when completion is successfully reported."""
        orch = FakeOrchForEnvelope(agent_id="ag_child")
        rec = orch._agents["ag_child"]
        rec.pending_replies = {
            "msg_1": {"from_id": "ag_leader", "ts": time.time()},
        }
        rec.reply_gate_active = True

        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")
        asyncio.run(hook(_make_input(detail=_ENVELOPE), tool_use_id="toolu_9", context=None))

        # Pending replies cleared.
        assert rec.pending_replies == {}
        assert rec.reply_gate_active is False
        # reply.abandoned event emitted.
        assert orch.events_named("reply.abandoned") != []
