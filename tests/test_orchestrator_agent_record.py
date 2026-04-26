"""Tests for the completion-review foundation fields on AgentRecord.

bd issue 8z3: Add completion_pending, completion_pending_ts, last_progress_ts,
review_ping_count to AgentRecord; update inbox_put to maintain them.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from beidou.orchestrator import (
    ROOT_TEAM_ID,
    USER_SENTINEL,
    AgentRecord,
    Orchestrator,
    TeamRecord,
)
from beidou.primitives.core import Message, PrimitiveError
from beidou.sdk_agent import build_hooks


# ---------------------------------------------------------------------------
# Helpers (mirrors test_orchestrator.py patterns).
# ---------------------------------------------------------------------------


class _FakeEmitter:
    """Replaces EventEmitter so tests don't touch ~/.beidou or SQLite."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Optional[str], dict]] = []

    async def emit(self, event: str, agent_id: str, team_id=None, **kwargs) -> None:
        self.calls.append((event, agent_id, team_id, kwargs))

    def events_named(self, name: str) -> list[tuple[str, Optional[str], dict]]:
        """Return (agent_id, team_id, kwargs) tuples for every call with the given event name."""
        return [(a, t, kw) for ev, a, t, kw in self.calls if ev == name]


def _make_orchestrator(tmp_path: Path) -> tuple[Orchestrator, _FakeEmitter]:
    emitter = _FakeEmitter()
    skill_root = tmp_path / "skills"
    skill_root.mkdir(exist_ok=True)
    o = Orchestrator(
        task_id="tsk_test",
        emitter=emitter,  # type: ignore[arg-type]
        skill_root=skill_root,
    )
    return o, emitter


def _seed_team(
    o: Orchestrator,
    team_id: str,
    leader_id: str,
    *,
    depth: int = 0,
    parent: Optional[str] = None,
) -> TeamRecord:
    t = TeamRecord(
        team_id=team_id,
        name=team_id,
        task="",
        leader_id=leader_id,
        depth=depth,
        member_ids=[],
        rules=[],
        parent_team_id=parent,
    )
    o._teams[team_id] = t
    return t


def _seed_agent(
    o: Orchestrator,
    agent_id: str,
    team_id: str,
    *,
    role: str = "member",
    skill: str = "fake_skill",
) -> AgentRecord:
    rec = AgentRecord(
        agent_id=agent_id,
        task_id=o.task_id,
        team_id=team_id,
        role=role,
        skill_name=skill,
        model=None,
        inbox=asyncio.Queue(),
        create_team_lock=asyncio.Lock(),
    )
    o._agents[agent_id] = rec
    if team_id in o._teams and agent_id not in o._teams[team_id].member_ids:
        o._teams[team_id].member_ids.append(agent_id)
    return rec


def run(coro) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_agent_record_has_pending_review_fields(tmp_path):
    """AgentRecord must expose the four completion-review fields with correct defaults."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "ag1", ROOT_TEAM_ID)

    # Field existence and defaults.
    assert rec.completion_pending is False
    assert rec.completion_pending_ts is None
    assert rec.review_ping_count == 0
    # last_progress_ts is set to time.time() at construction — must be a recent float.
    assert isinstance(rec.last_progress_ts, float)
    assert rec.last_progress_ts > 0


def test_inbox_put_updates_last_progress_ts(tmp_path):
    """inbox_put must bump last_progress_ts to (approximately) the current time."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "ag1", ROOT_TEAM_ID)

    # Force an old baseline so the test isn't sensitive to sub-millisecond timing.
    rec.last_progress_ts = time.time() - 1.0
    baseline = rec.last_progress_ts

    async def body():
        await o.inbox_put(
            "ag1",
            Message(from_id="peer", content="hello", ts=time.time(), message_id="m1"),
        )
        assert rec.last_progress_ts > baseline

    run(body())


def test_inbox_put_from_leader_clears_completion_pending(tmp_path):
    """When the direct leader delivers a non-system message, completion_pending must clear."""
    o, emitter = _make_orchestrator(tmp_path)
    # Topology: leader "L" leads team "tm_child"; agent "A" is a member.
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=ROOT_TEAM_ID)
    _seed_agent(o, "L", ROOT_TEAM_ID, role="leader")
    rec = _seed_agent(o, "A", "tm_child")

    # Manually set completion_pending as if on_report_status had done it.
    rec.completion_pending = True
    rec.completion_pending_ts = time.time() - 5.0
    rec.review_ping_count = 2

    async def body():
        await o.inbox_put(
            "A",
            Message(
                from_id="L",  # leader of tm_child
                content="rework this please",
                ts=time.time(),
                message_id="m2",
            ),
        )
        # Flags must be cleared.
        assert rec.completion_pending is False
        assert rec.completion_pending_ts is None
        assert rec.review_ping_count == 0

        # completion.rework event must have been emitted.
        await asyncio.sleep(0)  # drain create_task callbacks
        rework_events = emitter.events_named("completion.rework")
        assert len(rework_events) == 1
        agent_id_emitted, _team, kw = rework_events[0]
        assert agent_id_emitted == "A"
        assert kw["leader_id"] == "L"
        assert "rework" in kw["content_preview"]

    run(body())


def test_inbox_put_from_non_leader_does_not_clear(tmp_path):
    """A message from a peer (not the leader) must NOT clear completion_pending."""
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=ROOT_TEAM_ID)
    _seed_agent(o, "L", ROOT_TEAM_ID, role="leader")
    _seed_agent(o, "P", "tm_child", role="peer")   # peer in same team
    rec = _seed_agent(o, "A", "tm_child")

    rec.completion_pending = True
    rec.completion_pending_ts = time.time() - 2.0
    rec.review_ping_count = 1

    async def body():
        await o.inbox_put(
            "A",
            Message(
                from_id="P",  # peer, not the leader
                content="hey",
                ts=time.time(),
                message_id="m3",
            ),
        )
        # Flags must remain untouched.
        assert rec.completion_pending is True
        assert rec.review_ping_count == 1

        await asyncio.sleep(0)
        assert len(emitter.events_named("completion.rework")) == 0

    run(body())


def test_terminate_emits_completion_approved_when_pending(tmp_path):
    """Terminating an agent with completion_pending=True must emit completion.approved."""
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=ROOT_TEAM_ID)
    _seed_agent(o, "L", ROOT_TEAM_ID, role="leader")
    rec = _seed_agent(o, "A", "tm_child")

    # Manually simulate pending completion.
    rec.completion_pending = True
    rec.completion_pending_ts = time.time() - 3.0

    async def body():
        await o.inbox_put(
            "A",
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="t1",
                kind="terminate",
            ),
        )
        await asyncio.sleep(0)  # drain background emit tasks
        approved_events = emitter.events_named("completion.approved")
        assert len(approved_events) == 1
        agent_id_emitted, _team, kw = approved_events[0]
        assert agent_id_emitted == "A"
        assert kw["leader_id"] == "L"
        # Duration must be a positive number (we waited ~3 s above).
        assert kw["completion_pending_duration_ms"] is not None
        assert kw["completion_pending_duration_ms"] > 0

    run(body())


def test_terminate_no_completion_approved_when_not_pending(tmp_path):
    """Terminating an agent with completion_pending=False must NOT emit completion.approved."""
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=ROOT_TEAM_ID)
    _seed_agent(o, "L", ROOT_TEAM_ID, role="leader")
    rec = _seed_agent(o, "A", "tm_child")
    # completion_pending defaults to False — leave it.

    async def body():
        await o.inbox_put(
            "A",
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="t2",
                kind="terminate",
            ),
        )
        await asyncio.sleep(0)
        assert len(emitter.events_named("completion.approved")) == 0

    run(body())


def test_inbox_put_from_user_gateway_clears_completion_pending(tmp_path):
    """A message from the user gateway (from_id='user') must also clear completion_pending."""
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=ROOT_TEAM_ID)
    _seed_agent(o, "L", ROOT_TEAM_ID, role="leader")
    rec = _seed_agent(o, "A", "tm_child")

    rec.completion_pending = True
    rec.completion_pending_ts = time.time() - 1.0
    rec.review_ping_count = 3

    async def body():
        await o.inbox_put(
            "A",
            Message(
                from_id="user",  # human gateway sentinel
                content="please keep going",
                ts=time.time(),
                message_id="m4",
            ),
        )
        assert rec.completion_pending is False
        assert rec.completion_pending_ts is None
        assert rec.review_ping_count == 0

        await asyncio.sleep(0)
        rework_events = emitter.events_named("completion.rework")
        assert len(rework_events) == 1
        agent_id_emitted, _team, kw = rework_events[0]
        assert agent_id_emitted == "A"

    run(body())


# ---------------------------------------------------------------------------
# Tests for bd issue ql2: terminate_child must clear completion_pending.
# ---------------------------------------------------------------------------


def test_terminate_child_clears_completion_pending(tmp_path):
    """Terminating a completion_pending child must clear all pending-review fields.

    Regression for tsk_e92d672b: after terminate, the AgentRecord was left with
    completion_pending=True, which caused the leader's on_review_gate hook to
    deny every subsequent tool call (create_team, etc.).
    """
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=ROOT_TEAM_ID)
    _seed_agent(o, "L", ROOT_TEAM_ID, role="leader")
    rec = _seed_agent(o, "A", "tm_child")

    # Simulate pending completion with known timestamps.
    known_ts = time.time() - 3.0
    rec.completion_pending = True
    rec.completion_pending_ts = known_ts
    rec.review_ping_count = 2
    rec.idle_nudge_count = 1

    async def body():
        await o.inbox_put(
            "A",
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="t_ql2_a",
                kind="terminate",
            ),
        )
        await asyncio.sleep(0)  # drain background tasks

        # (a) completion.approved must have been emitted with a positive duration.
        approved = emitter.events_named("completion.approved")
        assert len(approved) == 1, f"Expected 1 completion.approved, got {approved}"
        _agent_id, _team, kw = approved[0]
        assert _agent_id == "A"
        assert kw["leader_id"] == "L"
        assert kw["completion_pending_duration_ms"] is not None
        assert kw["completion_pending_duration_ms"] > 0, (
            f"duration_ms should be >0 (was {kw['completion_pending_duration_ms']})"
        )

        # (b) All pending-review fields must be cleared.
        assert rec.completion_pending is False, "completion_pending must be False after terminate"
        assert rec.completion_pending_ts is None, "completion_pending_ts must be None after terminate"
        assert rec.review_ping_count == 0, f"review_ping_count must be 0, got {rec.review_ping_count}"
        assert rec.idle_nudge_count == 0, f"idle_nudge_count must be 0, got {rec.idle_nudge_count}"

    run(body())


def test_terminate_child_no_event_when_not_pending(tmp_path):
    """Terminating an agent with completion_pending=False must NOT emit completion.approved.

    Existing behaviour preserved (mirrors test_terminate_no_completion_approved_when_not_pending).
    """
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=ROOT_TEAM_ID)
    _seed_agent(o, "L", ROOT_TEAM_ID, role="leader")
    _seed_agent(o, "A", "tm_child")
    # completion_pending defaults to False — leave it.

    async def body():
        await o.inbox_put(
            "A",
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="t_ql2_b",
                kind="terminate",
            ),
        )
        await asyncio.sleep(0)
        assert len(emitter.events_named("completion.approved")) == 0, (
            "No completion.approved event expected when completion_pending was False"
        )

    run(body())


def test_terminate_child_pending_then_leader_can_proceed(tmp_path):
    """Integration regression: after terminate clears pending, on_review_gate allows create_team.

    Exact scenario from tsk_e92d672b:
      1. Child reports done (completion_pending=True).
      2. Leader calls terminate_child → inbox_put delivers terminate sentinel.
      3. Leader now calls create_team (Phase 2).
      4. Before fix: review gate still sees completion_pending=True and denies.
         After fix: review gate sees completion_pending=False and allows.
    """
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=ROOT_TEAM_ID)
    leader_rec = _seed_agent(o, "L", ROOT_TEAM_ID, role="leader")
    child_rec = _seed_agent(o, "C", "tm_child")

    # Simulate child having called report_status(state="done").
    child_rec.completion_pending = True
    child_rec.completion_pending_ts = time.time() - 2.0

    async def body():
        # Step 1: leader terminates the child (delivers terminate sentinel).
        await o.inbox_put(
            "C",
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="t_ql2_c",
                kind="terminate",
            ),
        )
        await asyncio.sleep(0)  # drain bg tasks so pending state is cleared

        # Verify the fix took effect on the record.
        assert child_rec.completion_pending is False, (
            "After terminate, child's completion_pending must be False"
        )

        # Step 2: build the leader's hooks and call on_review_gate for create_team.
        hooks = build_hooks(o, caller_id="L", leader_id=USER_SENTINEL)  # type: ignore[arg-type]
        pre_matchers = hooks.get("PreToolUse", [])
        # on_review_gate is the match-all matcher (matcher=None).
        on_review_gate = None
        for m in pre_matchers:
            if m.matcher is None:
                on_review_gate = m.hooks[0]
                break
        assert on_review_gate is not None, "Could not locate on_review_gate in build_hooks output"

        result = await on_review_gate(
            {"tool_name": "mcp__beidou__create_team", "tool_input": {}},
            tool_use_id="hook_t1",
            context=None,
        )

        # Step 3: assert gate allows (empty dict) and no deny event was emitted.
        assert result == {}, (
            f"Expected allow (empty dict) from on_review_gate, got {result!r}"
        )

        await asyncio.sleep(0)  # drain any bg tasks from the hook
        denied = emitter.events_named("review_gate.denied")
        assert len(denied) == 0, (
            f"Expected no review_gate.denied events, got {denied}"
        )

    run(body())
