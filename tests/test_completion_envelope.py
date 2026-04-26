"""Tests for the completion envelope guard and completion_pending flag in on_report_status.

bd issue k1q: Phase 1 envelope guard + Phase 2 step 2.

Tests:
- test_envelope_present_passthrough: body already has [REVIEW REQUIRED] → no synthesis.
- test_envelope_missing_synthesizes: body lacks [REVIEW REQUIRED] → synthesis fires.
- test_state_not_done_no_pending_flag: state='working' → no completion_pending set.
- test_root_no_leader_skips: root agent (leader_id == USER_SENTINEL) → completion_pending stays False.
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
# Fake AgentRecord — minimal fields needed by on_report_status.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FakeAgentRecord:
    skill_name: str = "test_skill"
    completion_pending: bool = False
    completion_pending_ts: Optional[float] = None
    last_progress_ts: float = dataclasses.field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Fake orchestrator stub for envelope tests.
# Extends the minimal FakeOrchForHooks pattern from test_sdk_agent_hooks.py.
# ---------------------------------------------------------------------------


class FakeOrchForEnvelope:
    """Minimal orchestrator stub for completion envelope tests.

    Exposes:
    - emit_event(): records (name, payload) pairs.
    - assistant_text_for_turn(): returns configurable text.
    - deliver_message(): records calls.
    - _agents: dict keyed by caller_id, values are FakeAgentRecord.
    """

    def __init__(
        self,
        *,
        assistant_text: Optional[str] = None,
        agent_id: str = "ag_child",
        skill_name: str = "junior_engineer",
    ) -> None:
        self.events: list[tuple[str, dict]] = []
        self._assistant_text = assistant_text
        self._delivered: list[dict] = []

        # Seed the agents dict with the caller's record.
        self._agents: dict[str, FakeAgentRecord] = {
            agent_id: FakeAgentRecord(skill_name=skill_name),
        }
        self._agent_id = agent_id

    def emit_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def assistant_text_for_turn(self, caller_id: str, tool_use_id: str) -> Optional[str]:
        return self._assistant_text

    def deliver_message(self, *, from_id: str, to_id: str, body: str, kind: str) -> None:
        self._delivered.append({"from_id": from_id, "to_id": to_id, "body": body, "kind": kind})

    # Convenience helpers for assertions.
    def events_named(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]

    def delivered_bodies(self) -> list[str]:
        return [d["body"] for d in self._delivered]


# ---------------------------------------------------------------------------
# Helper to extract the PostToolUse on_report_status hook.
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
    assert matcher.matcher == "mcp__beidou__report_status"
    return matcher.hooks[0]


# ---------------------------------------------------------------------------
# Shared input_data factory.
# ---------------------------------------------------------------------------


def _make_input(state: str = "done", detail: Optional[str] = None) -> dict:
    tool_input: dict[str, Any] = {"state": state}
    if detail is not None:
        tool_input["detail"] = detail
    return {
        "tool_name": "mcp__beidou__report_status",
        "tool_input": tool_input,
        "tool_response": {"is_error": False},
    }


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


class TestEnvelopeGuard:
    """Tests for the completion envelope guard in on_report_status."""

    def test_envelope_present_passthrough(self) -> None:
        """Agent includes [REVIEW REQUIRED] in detail → no synthesis, body delivered unchanged,
        completion_pending is set to True."""
        detail = "[REVIEW REQUIRED] role=foo agent=ag_child\nDeliverables: done.\n"
        orch = FakeOrchForEnvelope(
            agent_id="ag_child",
            skill_name="junior_engineer",
        )
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")

        result = asyncio.run(hook(_make_input(state="done", detail=detail), tool_use_id="toolu_1", context=None))

        # No synthesis event.
        assert orch.events_named("completion.envelope_synthesized") == []

        # Body delivered unchanged.
        bodies = orch.delivered_bodies()
        assert len(bodies) == 1
        assert bodies[0] == detail

        # completion_pending set.
        rec = orch._agents["ag_child"]
        assert rec.completion_pending is True
        assert rec.completion_pending_ts is not None
        assert rec.last_progress_ts > 0

        # completion.reported emitted.
        reported = orch.events_named("completion.reported")
        assert len(reported) == 1
        assert reported[0]["agent_id"] == "ag_child"
        assert reported[0]["leader_id"] == "ag_leader"

    def test_envelope_missing_synthesizes(self) -> None:
        """Agent reports state='done' with detail='finished' and no same-turn assistant text.
        Assert synthesis fired, body starts with [REVIEW REQUIRED], original text preserved."""
        orch = FakeOrchForEnvelope(
            assistant_text=None,
            agent_id="ag_child",
            skill_name="junior_engineer",
        )
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")

        result = asyncio.run(
            hook(_make_input(state="done", detail="finished"), tool_use_id="toolu_2", context=None)
        )

        # Synthesis event must have been emitted.
        synthesized_events = orch.events_named("completion.envelope_synthesized")
        assert len(synthesized_events) == 1
        evt = synthesized_events[0]
        assert evt["agent_id"] == "ag_child"
        assert evt["leader_id"] == "ag_leader"
        # body_chars is a string and must start with [REVIEW REQUIRED].
        assert isinstance(evt["body_chars"], str)
        assert evt["body_chars"].startswith("[REVIEW REQUIRED]")

        # Body delivered to leader.
        bodies = orch.delivered_bodies()
        assert len(bodies) == 1
        body = bodies[0]

        # Body must start with [REVIEW REQUIRED].
        assert body.startswith("[REVIEW REQUIRED]")
        # Original text must be preserved somewhere after the envelope.
        assert "finished" in body
        # Skill name and caller_id must appear in the envelope header.
        assert "junior_engineer" in body
        assert "ag_child" in body

        # completion_pending must be set.
        rec = orch._agents["ag_child"]
        assert rec.completion_pending is True
        assert rec.completion_pending_ts is not None

        # completion.reported emitted once (with synthesized body).
        reported = orch.events_named("completion.reported")
        assert len(reported) == 1

    def test_envelope_case_insensitive_match(self) -> None:
        """[review required] in mixed case → treated as present, no synthesis."""
        detail = "[Review Required] role=foo\nwork done."
        orch = FakeOrchForEnvelope(agent_id="ag_child")
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")

        asyncio.run(hook(_make_input(state="done", detail=detail), tool_use_id="toolu_3", context=None))

        # No synthesis event.
        assert orch.events_named("completion.envelope_synthesized") == []
        # Body delivered as-is.
        assert orch.delivered_bodies()[0] == detail

    def test_state_not_done_no_pending_flag(self) -> None:
        """Agent reports state='working' → completion_pending stays False, no events."""
        orch = FakeOrchForEnvelope(agent_id="ag_child")
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")

        asyncio.run(
            hook(_make_input(state="working", detail="still going"), tool_use_id="toolu_4", context=None)
        )

        rec = orch._agents["ag_child"]
        assert rec.completion_pending is False
        assert rec.completion_pending_ts is None
        # No completion events of any kind.
        assert orch.events == []
        assert orch.delivered_bodies() == []

    def test_root_no_leader_skips(self) -> None:
        """Root agent (leader_id == USER_SENTINEL) → completion.empty(root_no_leader) emitted,
        completion_pending stays False (no AgentRecord update for root path)."""
        orch = FakeOrchForEnvelope(agent_id="ag_root")
        hook = _get_posttooluse_hook(orch, caller_id="ag_root", leader_id=USER_SENTINEL)

        asyncio.run(
            hook(_make_input(state="done", detail="root done"), tool_use_id="toolu_5", context=None)
        )

        # completion.empty with root_no_leader reason.
        empty_events = orch.events_named("completion.empty")
        assert len(empty_events) == 1
        assert empty_events[0]["reason"] == "root_no_leader"
        assert empty_events[0]["agent_id"] == "ag_root"

        # completion_pending stays False (hook returns early before setting it).
        rec = orch._agents.get("ag_root")
        if rec is not None:
            assert rec.completion_pending is False

        # No delivery to any leader.
        assert orch.delivered_bodies() == []

    def test_envelope_body_chars_truncated_to_200(self) -> None:
        """body_chars field in synthesis event is at most 200 characters."""
        very_long_detail = "x" * 1000  # no [REVIEW REQUIRED], very long
        orch = FakeOrchForEnvelope(agent_id="ag_child", assistant_text=None)
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")

        asyncio.run(
            hook(_make_input(state="done", detail=very_long_detail), tool_use_id="toolu_6", context=None)
        )

        synthesized_events = orch.events_named("completion.envelope_synthesized")
        assert len(synthesized_events) == 1
        body_chars = synthesized_events[0]["body_chars"]
        assert len(body_chars) <= 200

    def test_same_turn_assistant_text_used_over_detail(self) -> None:
        """When same-turn assistant text is available, it takes priority over detail arg
        for envelope check. If it lacks [REVIEW REQUIRED], synthesis fires."""
        assistant_text = "I have completed the task: deliverable is in /workspace/out.txt"
        orch = FakeOrchForEnvelope(
            assistant_text=assistant_text,
            agent_id="ag_child",
            skill_name="engineer",
        )
        hook = _get_posttooluse_hook(orch, caller_id="ag_child", leader_id="ag_leader")

        asyncio.run(
            hook(_make_input(state="done", detail="[REVIEW REQUIRED] fallback"), tool_use_id="toolu_7", context=None)
        )

        # assistant_text took priority and lacks [REVIEW REQUIRED] → synthesis fired.
        synthesized_events = orch.events_named("completion.envelope_synthesized")
        assert len(synthesized_events) == 1
        # The delivered body must contain the original assistant text.
        body = orch.delivered_bodies()[0]
        assert assistant_text in body
