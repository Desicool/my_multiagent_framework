"""Tests for the leader-side review gate PreToolUse hook.

bd issue be3: Add on_review_gate to build_hooks so a leader cannot advance
while it has direct children with completion_pending=True.

All tests are self-contained unit tests — no I/O, no real Anthropic API calls.
The real Orchestrator + helper factories from test_orchestrator_agent_record.py
are used directly so the hook is exercised against the real AgentRecord /
TeamRecord types.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from beidou.orchestrator import (
    USER_SENTINEL,
    AgentRecord,
    Orchestrator,
    TeamRecord,
)
from beidou.primitives.core import Message
from beidou.sdk_agent import build_hooks

# A generic top-level team id used in tests that need a named team but
# don't care about the root-agent teamless semantics.
_TM_TOP = "tm_top"


# ---------------------------------------------------------------------------
# Fake emitter — mirrors _FakeEmitter in test_orchestrator_agent_record.py.
# bd issue be3
# ---------------------------------------------------------------------------


class _FakeEmitter:
    """Replaces EventEmitter so tests don't touch ~/.beidou or SQLite."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Optional[str], dict]] = []

    async def emit(self, event: str, agent_id: str, team_id=None, **kwargs) -> None:
        self.calls.append((event, agent_id, team_id, kwargs))

    def events_named(self, name: str) -> list[tuple[str, Optional[str], dict]]:
        return [(a, t, kw) for ev, a, t, kw in self.calls if ev == name]


# ---------------------------------------------------------------------------
# Orchestrator factory helpers — bd issue be3
# ---------------------------------------------------------------------------


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
    team_id: str | None,
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
    if team_id is not None and team_id in o._teams and agent_id not in o._teams[team_id].member_ids:
        o._teams[team_id].member_ids.append(agent_id)
    return rec


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helper to locate the on_review_gate hook inside build_hooks output.
# bd issue be3
# ---------------------------------------------------------------------------


def _get_review_gate_hook(orch: Orchestrator, caller_id: str, leader_id: str):
    """Extract the on_review_gate hook from build_hooks(...).

    Identifies it as the second PreToolUse matcher with matcher=None (match-all).
    The first match-all matcher is on_reply_gate (inserted for gate precedence).
    """
    hooks = build_hooks(orch, caller_id=caller_id, leader_id=leader_id)  # type: ignore[arg-type]
    pre_matchers = hooks.get("PreToolUse", [])
    match_all_matchers = [m for m in pre_matchers if m.matcher is None]
    if len(match_all_matchers) >= 2:
        return match_all_matchers[1].hooks[0]
    raise AssertionError(
        f"Expected at least 2 match-all PreToolUse matchers, found {len(match_all_matchers)}. Matchers: {[m.matcher for m in pre_matchers]}"
    )


def _input_data(tool_name: str, tool_input: Optional[dict] = None) -> dict:
    """Build a minimal hook input_data dict for the given tool_name."""
    return {
        "tool_name": tool_name,
        "tool_input": tool_input or {},
    }


# ---------------------------------------------------------------------------
# Tests — bd issue be3
# ---------------------------------------------------------------------------


# bd issue be3: no pending children → create_team allowed
def test_pretoolc_no_pending_children_allows_create_team(tmp_path: Path) -> None:
    """Leader with no completion_pending children can call create_team freely."""
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = False  # explicit; is the default

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(hook(_input_data("mcp__beidou__create_team"), tool_use_id="t1", context=None))

    # Allow: empty dict.
    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    # No deny event.
    assert not o.emit_event.__dict__.get("calls")  # direct emit_event is sync; check via emitter
    # The orchestrator's emit_event calls are recorded by _FakeEmitter only when routed through
    # the full async path. For sync emit_event calls, check orch directly.
    _check_no_gate_denied_event(o)


# bd issue be3: pending child → create_team denied
def test_pretoolc_pending_child_denies_create_team(tmp_path: Path) -> None:
    """Leader with 1 completion_pending child cannot call create_team; gets a denial."""
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True
    child.completion_pending_ts = time.time()

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(hook(_input_data("mcp__beidou__create_team"), tool_use_id="t2", context=None))

    # Must be a deny response.
    _assert_deny(result)

    # Denial reason must list the pending child id.
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "C1" in reason, f"Expected child id 'C1' in denial reason, got: {reason!r}"
    assert "terminate_child" in reason, f"Expected 'terminate_child' in denial reason"
    assert "send_message" in reason, f"Expected 'send_message' in denial reason"

    # review_gate.denied event must have been emitted.
    _assert_gate_denied_event(o, agent_id="L", tool_name="mcp__beidou__create_team", pending=["C1"])


# bd issue be3: pending child → terminate_child allowed
def test_pretoolc_pending_child_allows_terminate_child(tmp_path: Path) -> None:
    """Leader with pending child CAN call mcp__beidou__terminate_child (allowlist)."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(hook(_input_data("mcp__beidou__terminate_child", {"agent_id": "C1"}), tool_use_id="t3", context=None))

    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    _check_no_gate_denied_event(o)


# bd issue be3: pending child → send_message allowed
def test_pretoolc_pending_child_allows_send_message(tmp_path: Path) -> None:
    """Leader with pending child CAN call mcp__beidou__send_message (allowlist)."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(hook(_input_data("mcp__beidou__send_message", {"to": "C1", "content": "rework: fix the tests"}), tool_use_id="t4", context=None))

    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    _check_no_gate_denied_event(o)


# bd issue be3: pending child → Read allowed (leader can inspect artifacts)
def test_pretoolc_pending_child_allows_read(tmp_path: Path) -> None:
    """Leader with pending child CAN call Read (allowlist — inspect artifacts)."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(hook(_input_data("Read", {"file_path": "/workspace/out.txt"}), tool_use_id="t5", context=None))

    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    _check_no_gate_denied_event(o)


# bd issue be3: multiple pending children → denial lists all ids
def test_pretoolc_multiple_pending_children(tmp_path: Path) -> None:
    """2 pending children: denial lists both ids and resolution steps for each."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    c1 = _seed_agent(o, "C1", "tm_child")
    c2 = _seed_agent(o, "C2", "tm_child")
    c1.completion_pending = True
    c2.completion_pending = True

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    # Use create_team (not Write — Write is now in ALLOWED_DURING_PENDING_REVIEW since bd issue xq1)
    result = run(hook(_input_data("mcp__beidou__create_team", {"task": "sub"}), tool_use_id="t6", context=None))

    _assert_deny(result)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    # Both child ids must appear in the denial reason.
    assert "C1" in reason, f"Expected 'C1' in denial reason, got: {reason!r}"
    assert "C2" in reason, f"Expected 'C2' in denial reason, got: {reason!r}"

    # The event must list both pending children.
    _assert_gate_denied_event(o, agent_id="L", tool_name="mcp__beidou__create_team", pending=["C1", "C2"])


# ---------------------------------------------------------------------------
# Assertion helpers — bd issue be3
# ---------------------------------------------------------------------------


def _assert_deny(result: dict) -> None:
    """Assert result has the expected deny shape."""
    assert "hookSpecificOutput" in result, f"Expected hookSpecificOutput key, got: {result!r}"
    hso = result["hookSpecificOutput"]
    assert hso.get("hookEventName") == "PreToolUse", f"Expected PreToolUse, got: {hso!r}"
    assert hso.get("permissionDecision") == "deny", f"Expected deny, got: {hso!r}"
    assert hso.get("permissionDecisionReason"), f"Expected non-empty reason, got: {hso!r}"


def _check_no_gate_denied_event(o: Orchestrator) -> None:
    """Assert no review_gate.denied event was emitted via synchronous emit_event."""
    # emit_event is synchronous on Orchestrator; it routes to the emitter.
    # We can verify by inspecting the emitter's internal state via the
    # orchestrator's emit_event (which is synchronous and calls emitter).
    # Since _FakeEmitter's emit is async, the real Orchestrator wraps it in
    # create_task. We check the events list by draining any pending tasks.
    asyncio.run(_drain_bg_tasks(o))
    # The emitter's calls list is populated by async emit(); since we're
    # not running a persistent loop here, directly inspect _bg_tasks outcome.
    # Simplest path: check that any gate_denied events in the captured list
    # are absent. We patch into orch directly.
    _captured = _capture_sync_events(o)
    denied = [e for e in _captured if e[0] == "review_gate.denied"]
    assert not denied, f"Unexpected review_gate.denied events: {denied}"


def _assert_gate_denied_event(
    o: Orchestrator,
    *,
    agent_id: str,
    tool_name: str,
    pending: list[str],
) -> None:
    """Assert that exactly one review_gate.denied event was recorded."""
    _captured = _capture_sync_events(o)
    denied = [e for e in _captured if e[0] == "review_gate.denied"]
    assert len(denied) == 1, f"Expected 1 review_gate.denied event, got {len(denied)}: {denied}"
    _, payload = denied[0]
    assert payload.get("agent_id") == agent_id, f"agent_id mismatch: {payload}"
    assert payload.get("tool_name") == tool_name, f"tool_name mismatch: {payload}"
    for pid in pending:
        assert pid in payload.get("pending_children", []), (
            f"Expected {pid} in pending_children: {payload}"
        )


# Registry of synchronous emit_event calls patched in on _make_orchestrator.
# Because the real Orchestrator.emit_event dispatches to an async emitter via
# create_task, we intercept at the synchronous boundary instead.
_SYNC_EVENT_REGISTRY: dict[int, list[tuple[str, dict]]] = {}


def _capture_sync_events(o: Orchestrator) -> list[tuple[str, dict]]:
    """Return all (event_name, payload) pairs recorded by the patched emit_event."""
    return _SYNC_EVENT_REGISTRY.get(id(o), [])


async def _drain_bg_tasks(o: Orchestrator) -> None:
    """Yield control so background tasks created by emit_event can run."""
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Monkeypatch emit_event so we can capture sync calls in tests.
# bd issue be3
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_emit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace Orchestrator.emit_event with a recording version for all tests in this module."""
    original_emit_event = Orchestrator.emit_event

    def _recording_emit_event(self: Orchestrator, event_name: str, payload: dict) -> None:
        oid = id(self)
        if oid not in _SYNC_EVENT_REGISTRY:
            _SYNC_EVENT_REGISTRY[oid] = []
        _SYNC_EVENT_REGISTRY[oid].append((event_name, payload))
        # Also call original so other behaviours are preserved.
        original_emit_event(self, event_name, payload)

    monkeypatch.setattr(Orchestrator, "emit_event", _recording_emit_event)

    yield

    # Clean up registry entries after each test.
    _SYNC_EVENT_REGISTRY.clear()


# ---------------------------------------------------------------------------
# Watchdog tests — bd issue qj2
# ---------------------------------------------------------------------------
# All watchdog tests are async. Each test that starts a watchdog must call
# await orch.stop_watchdog() in teardown so tasks don't leak.


def _make_orch_for_watchdog(tmp_path: Path) -> tuple[Orchestrator, _FakeEmitter]:
    """Build an orchestrator pre-seeded for watchdog tests.

    The root agent is teamless (team_id=None). A generic top-level team
    (_TM_TOP) is still seeded so non-root leader/child agents in the
    watchdog tests have a home team.
    """
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    return o, emitter


def _seed_root_agent(o: Orchestrator) -> AgentRecord:
    """Seed a teamless root agent directly (bypasses _register_agent_record).

    The root agent has team_id=None per the new teamless-root model.
    """
    rec = AgentRecord(
        agent_id="root_ag",
        task_id=o.task_id,
        team_id=None,
        role="root",
        skill_name="orchestrator",
        model=None,
        inbox=asyncio.Queue(),
        create_team_lock=asyncio.Lock(),
    )
    o._agents[rec.agent_id] = rec
    o._root_id = rec.agent_id
    return rec


def _events_named(o: Orchestrator, name: str) -> list[dict]:
    """Return all payloads for events of the given name recorded in the registry."""
    return [payload for ev, payload in _SYNC_EVENT_REGISTRY.get(id(o), []) if ev == name]


# Pass A tests ---------------------------------------------------------------


def test_watchdog_pings_at_60s(tmp_path: Path) -> None:
    """Pass A: child pending for >60s → 1 ping to leader, completion.reping emitted."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        _seed_team(o, "tm_child", leader_id="leader_ag", depth=1, parent=_TM_TOP)
        _seed_root_agent(o)
        # Seed a leader agent in the root team that is the leader of tm_child.
        leader = _seed_agent(o, "leader_ag", _TM_TOP, role="leader")
        child = _seed_agent(o, "child_ag", "tm_child")
        child.completion_pending = True
        child.completion_pending_ts = time.time() - 65.0  # >60s ago

        await o._watchdog_tick()
        await asyncio.sleep(0)  # drain bg tasks

        # 1 message in leader's inbox starting with [REVIEW REQUIRED — STILL PENDING
        assert leader.inbox.qsize() == 1
        msg = leader.inbox.get_nowait()
        assert "[REVIEW REQUIRED — STILL PENDING" in msg.content

        # completion.reping event emitted
        reping = _events_named(o, "completion.reping")
        assert len(reping) == 1, f"Expected 1 completion.reping, got {reping}"

        # ping_count incremented
        assert child.review_ping_count == 1
        # ts rebased
        assert child.completion_pending_ts is not None
        assert abs(child.completion_pending_ts - time.time()) < 5.0

        await o.stop_watchdog()

    run(body())


def test_watchdog_no_ping_before_60s(tmp_path: Path) -> None:
    """Pass A: child pending for <60s → no ping."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        _seed_team(o, "tm_child", leader_id="leader_ag", depth=1, parent=_TM_TOP)
        _seed_root_agent(o)
        leader = _seed_agent(o, "leader_ag", _TM_TOP, role="leader")
        child = _seed_agent(o, "child_ag", "tm_child")
        child.completion_pending = True
        child.completion_pending_ts = time.time() - 30.0  # only 30s ago

        await o._watchdog_tick()
        await asyncio.sleep(0)

        assert leader.inbox.qsize() == 0
        assert _events_named(o, "completion.reping") == []
        assert child.review_ping_count == 0

        await o.stop_watchdog()

    run(body())


def test_watchdog_second_ping_warns(tmp_path: Path) -> None:
    """Pass A: second ping (count==1) body must contain escalation warning."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        _seed_team(o, "tm_child", leader_id="leader_ag", depth=1, parent=_TM_TOP)
        _seed_root_agent(o)
        leader = _seed_agent(o, "leader_ag", _TM_TOP, role="leader")
        child = _seed_agent(o, "child_ag", "tm_child")
        child.completion_pending = True
        child.completion_pending_ts = time.time() - 65.0
        child.review_ping_count = 1  # already had first ping

        await o._watchdog_tick()
        await asyncio.sleep(0)

        assert leader.inbox.qsize() == 1
        msg = leader.inbox.get_nowait()
        assert "escalate to the user gateway" in msg.content

        await o.stop_watchdog()

    run(body())


def test_watchdog_third_strike_escalates(tmp_path: Path) -> None:
    """Pass A: count==2 → review.escalated_to_user event + gateway surfaced."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        _seed_team(o, "tm_child", leader_id="leader_ag", depth=1, parent=_TM_TOP)
        _seed_root_agent(o)
        _seed_agent(o, "leader_ag", _TM_TOP, role="leader")
        child = _seed_agent(o, "child_ag", "tm_child")
        child.completion_pending = True
        child.completion_pending_ts = time.time() - 65.0
        child.review_ping_count = 2

        # Set up a mock gateway with ask() (for is_gateway_available) and
        # surface_question (new protocol — the orchestrator calls this directly
        # instead of delegating to gateway.ask_structured).
        surfaced: list[tuple] = []

        async def _surface(qid, body_str, questions):
            surfaced.append((qid, body_str, questions))

        mock_gateway = MagicMock()
        mock_gateway.ask = AsyncMock(return_value="approved")
        mock_gateway.surface_question = _surface
        o.gateway = mock_gateway

        await o._watchdog_tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # allow create_task to run

        # review.escalated_to_user event.
        events = _events_named(o, "review.escalated_to_user")
        assert len(events) == 1, f"Expected review.escalated_to_user, got: {events}"

        # gateway.surface_question called once (question surfaced to user).
        assert len(surfaced) == 1, f"Expected surface_question to be called once, got: {surfaced}"
        _qid, _body, _qs = surfaced[0]
        assert "child_ag" in _body or "child_ag" in str(_qs)

        assert child.review_ping_count == 3

        await o.stop_watchdog()

    run(body())


def test_watchdog_post_escalation_silence(tmp_path: Path) -> None:
    """Pass A: count>=3 → no further events."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        _seed_team(o, "tm_child", leader_id="leader_ag", depth=1, parent=_TM_TOP)
        _seed_root_agent(o)
        leader = _seed_agent(o, "leader_ag", _TM_TOP, role="leader")
        child = _seed_agent(o, "child_ag", "tm_child")
        child.completion_pending = True
        child.completion_pending_ts = time.time() - 65.0
        child.review_ping_count = 3  # already escalated

        await o._watchdog_tick()
        await asyncio.sleep(0)

        assert leader.inbox.qsize() == 0
        assert _events_named(o, "completion.reping") == []
        assert _events_named(o, "review.escalated_to_user") == []
        assert child.review_ping_count == 3  # unchanged

        await o.stop_watchdog()

    run(body())


# Pass B tests ---------------------------------------------------------------


def test_idle_nudge_fires_for_idle_root(tmp_path: Path) -> None:
    """Pass B: root agent idle >120s with no children → liveness.nudge emitted."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        root = _seed_root_agent(o)
        root.last_progress_ts = time.time() - 130.0
        root.last_drain_ts = time.time() - 130.0  # stale drain; last_progress_ts <= last_drain_ts so freshness = last_drain_ts
        root.inflight_tools = 0
        root.completion_pending = False

        await o._watchdog_tick()
        await asyncio.sleep(0)

        nudges = _events_named(o, "liveness.nudge")
        assert len(nudges) == 1, f"Expected 1 liveness.nudge, got {nudges}"

        # Nudge body in inbox starts with [BEIDOU LIVENESS CHECK]
        assert root.inbox.qsize() == 1
        msg = root.inbox.get_nowait()
        assert msg.content.startswith("[BEIDOU LIVENESS CHECK]")

        await o.stop_watchdog()

    run(body())


def test_idle_nudge_skipped_when_inflight_tool(tmp_path: Path) -> None:
    """Pass B: inflight_tools>0 → skip liveness nudge even with stale drain."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        root = _seed_root_agent(o)
        root.last_progress_ts = time.time() - 130.0
        root.last_drain_ts = time.time() - 130.0  # stale drain; proves inflight_tools blocks nudge regardless
        root.inflight_tools = 1  # tool running → freshness=0 → no nudge

        await o._watchdog_tick()
        await asyncio.sleep(0)

        assert _events_named(o, "liveness.nudge") == []
        assert root.inbox.qsize() == 0

        await o.stop_watchdog()

    run(body())


def test_idle_nudge_fires_for_idle_worker_no_children(tmp_path: Path) -> None:
    """Pass B: non-root leaf worker with stale drain → liveness.nudge emitted (regression guard).

    This is the regression that motivated the freshness-signal redesign — the old leaf-worker
    skip at orchestrator.py:1407-1410 silently suppressed nudges for leaf workers, causing
    silent hangs. See plan ~/.claude/plans/i-want-to-see-transient-fox.md and bd my_simple_agent-npkn.

    Under the new contract:
    - last_drain_ts stale + last_progress_ts <= last_drain_ts → freshness = last_drain_ts
    - inflight_tools=0, completion_pending=False → no zero-freshness short-circuit
    - now - freshness >= IDLE_NUDGE_S → Pass B MUST nudge, regardless of leaf/non-leaf shape
    """

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        _seed_root_agent(o)
        _seed_team(o, "tm_child", leader_id="root_ag", depth=1, parent=_TM_TOP)
        worker = _seed_agent(o, "worker_ag", "tm_child")
        worker.last_progress_ts = time.time() - 200.0
        worker.last_drain_ts = time.time() - 200.0  # stale drain; last_progress_ts <= last_drain_ts → freshness = last_drain_ts
        worker.inflight_tools = 0
        worker.completion_pending = False
        # worker leads no teams → no direct children

        await o._watchdog_tick()
        await asyncio.sleep(0)

        nudges = _events_named(o, "liveness.nudge")
        assert len(nudges) == 1, f"Expected 1 liveness.nudge for idle leaf worker, got {nudges}"

        assert worker.inbox.qsize() == 1
        msg = worker.inbox.get_nowait()
        assert msg.content.startswith("[BEIDOU LIVENESS CHECK]")

        await o.stop_watchdog()

    run(body())


def test_idle_nudge_skipped_when_completion_pending(tmp_path: Path) -> None:
    """Pass B: completion_pending=True → Pass A handles it; Pass B must skip even with stale drain."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        root = _seed_root_agent(o)
        root.last_progress_ts = time.time() - 130.0
        root.last_drain_ts = time.time() - 130.0  # stale drain; proves completion_pending blocks the nudge regardless
        root.completion_pending = True  # Pass A territory → freshness=0 → no Pass B nudge

        await o._watchdog_tick()
        await asyncio.sleep(0)

        # No liveness.nudge (Pass A handles it, but in this test there's also no
        # completion_pending_ts so Pass A skips too — we just verify Pass B skips).
        assert _events_named(o, "liveness.nudge") == []

        await o.stop_watchdog()

    run(body())


def test_inflight_tools_skip(tmp_path: Path) -> None:
    """Focused: inflight_tools>0 blocks watchdog nudge even for a very idle root with stale drain."""

    async def body():
        o, _ = _make_orch_for_watchdog(tmp_path)
        root = _seed_root_agent(o)
        root.last_progress_ts = time.time() - 9999.0  # extremely idle
        root.last_drain_ts = time.time() - 9999.0  # stale drain; proves inflight_tools short-circuits freshness to 0
        root.inflight_tools = 2  # still running → freshness=0 → no nudge

        await o._watchdog_tick()
        await asyncio.sleep(0)

        assert _events_named(o, "liveness.nudge") == []

        await o.stop_watchdog()

    run(body())


def test_watchdog_starts_lazily_on_register(tmp_path: Path) -> None:
    """Watchdog must not exist until first agent is registered via _register_agent_record."""

    async def body():
        o, _ = _make_orchestrator(tmp_path)
        _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)

        # Before any registration, task must be None.
        assert o._watchdog_task is None

        # Build a record and call _register_agent_record inside a running loop.
        rec = AgentRecord(
            agent_id="ag_lazy",
            task_id=o.task_id,
            team_id=_TM_TOP,
            role="root",
            skill_name="orchestrator",
            model=None,
            inbox=asyncio.Queue(),
            create_team_lock=asyncio.Lock(),
        )
        o._register_agent_record(rec)
        if _TM_TOP in o._teams:
            o._teams[_TM_TOP].member_ids.append(rec.agent_id)

        # Now the watchdog task must exist.
        assert o._watchdog_task is not None

        await o.stop_watchdog()

    run(body())


# ---------------------------------------------------------------------------
# bd issue xq1: SDK-builtin classic name allowlist + terminate_consumed skip
# ---------------------------------------------------------------------------


# bd issue xq1: SendMessage classic name is allowed while pending child exists
def test_review_gate_allows_SendMessage_classic(tmp_path: Path) -> None:
    """Leader with 1 pending child can call SDK-builtin SendMessage (classic name).

    Concrete repro from tsk_e92d672b.jsonl (10:10:29): leader called
    SendMessage(to=ag_2145d837, ...) while child had completion_pending=True
    → review_gate.denied fired incorrectly. After xq1 fix, must be allowed.
    """
    # bd issue xq1
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True
    child.completion_pending_ts = time.time()

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    # Input shape from the events trace (tsk_e92d672b.jsonl)
    result = run(
        hook(
            _input_data(
                "SendMessage",
                {"to": "ag_2145d837", "message": "proceed", "type": "directive",
                 "recipient": "ag_2145d837", "content": "proceed"},
            ),
            tool_use_id="xq1_sm",
            context=None,
        )
    )

    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    _check_no_gate_denied_event(o)


# bd issue xq1: AskUserQuestion classic name is allowed while pending child exists
def test_review_gate_allows_AskUserQuestion_classic(tmp_path: Path) -> None:
    """Leader with 1 pending child can call SDK-builtin AskUserQuestion.

    on_ask_user_question handles the review-gate semantics when pending children
    exist; on_review_gate must pass through so both hooks can cooperate.
    """
    # bd issue xq1
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True
    child.completion_pending_ts = time.time()

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(
        hook(
            _input_data("AskUserQuestion", {"question": "Should I proceed?"}),
            tool_use_id="xq1_auq",
            context=None,
        )
    )

    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    _check_no_gate_denied_event(o)


# bd issue xq1: Write classic name is allowed while pending child exists
def test_review_gate_allows_Write_classic(tmp_path: Path) -> None:
    """Leader with 1 pending child can call SDK-builtin Write (e.g. to save notes)."""
    # bd issue xq1
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True
    child.completion_pending_ts = time.time()

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(
        hook(
            _input_data("Write", {"file_path": "/workspace/notes.txt", "content": "review notes"}),
            tool_use_id="xq1_wr",
            context=None,
        )
    )

    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    _check_no_gate_denied_event(o)


# bd issue xq1: Edit classic name is allowed while pending child exists
def test_review_gate_allows_Edit_classic(tmp_path: Path) -> None:
    """Leader with 1 pending child can call SDK-builtin Edit."""
    # bd issue xq1
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True
    child.completion_pending_ts = time.time()

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(
        hook(
            _input_data(
                "Edit",
                {"file_path": "/workspace/out.txt", "old_string": "foo", "new_string": "bar"},
            ),
            tool_use_id="xq1_ed",
            context=None,
        )
    )

    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    _check_no_gate_denied_event(o)


# bd issue xq1: terminate_consumed=True skips child from gating the leader
def test_review_gate_skips_terminate_consumed(tmp_path: Path) -> None:
    """Leader with 1 child that has completion_pending=True AND terminate_consumed=True.

    The terminated child's SDK session has ended; it must not gate the leader.
    Leader tries create_team → should be ALLOWED, no review_gate.denied.
    """
    # bd issue xq1
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child")
    child.completion_pending = True
    child.completion_pending_ts = time.time()
    child.terminate_consumed = True  # SDK session ended — must not gate

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(
        hook(
            _input_data("mcp__beidou__create_team", {"task": "next phase"}),
            tool_use_id="xq1_tc",
            context=None,
        )
    )

    assert result == {}, f"Expected allow (empty dict), got {result!r}"
    _check_no_gate_denied_event(o)


# bd issue xq1: mixed — only the live (non-terminated) child gates the leader
def test_review_gate_skips_terminate_consumed_mixed(tmp_path: Path) -> None:
    """Leader has 2 children: one terminated (completion_pending+terminate_consumed=True),
    one live (completion_pending=True only).

    Leader tries create_team → DENIED by the live child only. The terminated child
    must NOT appear in pending_children or the denial reason.
    """
    # bd issue xq1
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")

    # Terminated child — must be excluded from gating.
    dead = _seed_agent(o, "C_dead", "tm_child")
    dead.completion_pending = True
    dead.completion_pending_ts = time.time()
    dead.terminate_consumed = True

    # Live child — must gate the leader.
    live = _seed_agent(o, "C_live", "tm_child")
    live.completion_pending = True
    live.completion_pending_ts = time.time()
    live.terminate_consumed = False

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)
    result = run(
        hook(
            _input_data("mcp__beidou__create_team", {"task": "next phase"}),
            tool_use_id="xq1_tcm",
            context=None,
        )
    )

    # Must be denied (live child gates).
    _assert_deny(result)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]

    # Live child id must appear in the denial reason.
    assert "C_live" in reason, f"Expected 'C_live' in denial reason, got: {reason!r}"

    # Terminated child must NOT appear in the denial reason.
    assert "C_dead" not in reason, (
        f"Terminated child 'C_dead' must not appear in denial reason, got: {reason!r}"
    )

    # Event must list ONLY the live child.
    _captured = _capture_sync_events(o)
    denied_events = [e for e in _captured if e[0] == "review_gate.denied"]
    assert len(denied_events) == 1, f"Expected 1 review_gate.denied event, got {denied_events}"
    _, payload = denied_events[0]
    pending_children = payload.get("pending_children", [])
    assert "C_live" in pending_children, f"Expected C_live in pending_children: {payload}"
    assert "C_dead" not in pending_children, (
        f"Terminated child C_dead must not be in pending_children: {payload}"
    )
