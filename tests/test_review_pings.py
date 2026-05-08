"""Watchdog Pass A review-pending pings — v2 design committee vs. v1.

bd issue uen6: the orchestrator's review-pending pings used to tell every
leader to "Call mcp__beidou__terminate_child(...) (approve) OR send_message
rework". For v2 design-committee children, terminate_child is forbidden until
the User Approve gate (coding_v2/orchestrator/SKILL.md s2). The fix branches
on the child's skill_name and emits a v2-aware action line that names
send_message(rework/ack) only.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from beidou.orchestrator import (
    USER_SENTINEL,
    AgentRecord,
    Orchestrator,
    TeamRecord,
)
from test_review_gate import (  # type: ignore[import-not-found]
    _make_orchestrator,
    _seed_agent,
    _seed_team,
    _TM_TOP,
    run,
)


def _drain_inbox(rec: AgentRecord) -> list[str]:
    """Return all message bodies currently in the agent's inbox (FIFO)."""
    bodies: list[str] = []
    while not rec.inbox.empty():
        msg = rec.inbox.get_nowait()
        bodies.append(getattr(msg, "content", "") or getattr(msg, "body", "") or "")
    return bodies


def _make_pending_child_in_team(
    tmp_path: Path,
    *,
    skill: str,
) -> tuple[Orchestrator, AgentRecord, AgentRecord]:
    """Set up: leader L (real agent, has inbox) and child C1 with review_pending."""
    o, _ = _make_orchestrator(tmp_path)
    # Leader's team is _TM_TOP; the child's team has L as its leader.
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    leader = _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child", skill=skill)
    child.review_pending = True
    # Force the ping threshold: pretend review_pending fired 1000s ago.
    child.review_pending_ts = time.time() - 1000.0
    child.review_ping_count = 0
    return o, leader, child


def test_review_ping_for_v2_committee_member_omits_terminate_child(tmp_path: Path) -> None:
    """v2 committee child → ping body has 'Do NOT call terminate_child' and no terminate_child call site."""
    o, leader, child = _make_pending_child_in_team(tmp_path, skill="software_architect_v2")

    run(o._watchdog_tick())

    bodies = _drain_inbox(leader)
    assert len(bodies) == 1, f"expected 1 ping, got {len(bodies)}: {bodies!r}"
    body = bodies[0]

    # v2 wording present
    assert "Do NOT call terminate_child" in body, body
    assert 'send_message(to="C1"' in body, body
    # v1 wording absent
    assert 'Call mcp__beidou__terminate_child(agent_id="C1") (approve)' not in body, body
    # Standard envelope still wraps it
    assert "[REVIEW REQUIRED — STILL PENDING" in body, body
    assert "Do not end your turn before deciding." in body, body


def test_review_ping_for_v1_child_keeps_legacy_wording(tmp_path: Path) -> None:
    """v1 child (skill not in V2_DESIGN_COMMITTEE_SKILLS) → legacy terminate_child wording preserved."""
    o, leader, child = _make_pending_child_in_team(tmp_path, skill="qa_engineer")

    run(o._watchdog_tick())

    bodies = _drain_inbox(leader)
    assert len(bodies) == 1, f"expected 1 ping, got {len(bodies)}: {bodies!r}"
    body = bodies[0]

    assert 'Call mcp__beidou__terminate_child(agent_id="C1") (approve)' in body, body
    assert 'send_message(to="C1", content="rework: ...")' in body, body
    # v2 guardrail must NOT leak into v1 path.
    assert "Do NOT call terminate_child" not in body, body


def test_review_ping_second_iteration_v2_wording_persists(tmp_path: Path) -> None:
    """Second ping (review_ping_count==1) for a v2 child still uses v2 wording, plus the escalation tail."""
    o, leader, child = _make_pending_child_in_team(tmp_path, skill="ui_ux_designer_v2")
    child.review_ping_count = 1  # already pinged once

    run(o._watchdog_tick())

    bodies = _drain_inbox(leader)
    assert len(bodies) == 1
    body = bodies[0]

    assert "Do NOT call terminate_child" in body, body
    assert "this will escalate to the user gateway" in body, body
    assert 'Call mcp__beidou__terminate_child(agent_id="C1") (approve)' not in body, body


# ---------------------------------------------------------------------------
# bd issue 2uwj / plan tsk-f54d3beb: HOLD_UNTIL_UPSTREAM_LEADER_SKILLS gating
# (leader-skill, not child-skill) and Pass A leader-level dedup.
# ---------------------------------------------------------------------------


def _make_hold_leader_with_pending_child(
    tmp_path: Path,
    *,
    leader_skill: str,
    child_skill: str = "junior_engineer",
) -> tuple[Orchestrator, AgentRecord, AgentRecord]:
    """Set up a leader L (with a real upstream/parent leader U) and a single
    pending child C1 under L. The child has review_pending=True and the ping
    threshold met. Used to verify hold-style wording when L is itself a
    HOLD_UNTIL_UPSTREAM_LEADER_SKILLS leader.
    """
    from test_review_gate import _make_orchestrator, _seed_agent, _seed_team, _TM_TOP

    o, _ = _make_orchestrator(tmp_path)
    # Top team led by USER_SENTINEL; L's leader is U (root-ish) so L is not root.
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    # Mid team contains L; L's team is _TM_TOP and L leads tm_child.
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    leader = _seed_agent(o, "L", _TM_TOP, role="leader", skill=leader_skill)
    child = _seed_agent(o, "C1", "tm_child", skill=child_skill)
    child.review_pending = True
    child.review_pending_ts = time.time() - 1000.0
    child.review_ping_count = 0
    return o, leader, child


def test_review_ping_for_hold_until_upstream_leader_uses_hold_wording(tmp_path: Path) -> None:
    """junior_engineer_v2 leader → ping body tells leader to hold (not terminate_child).

    The child's skill is a regular junior_engineer (NOT a v2 committee member).
    Gating is on the LEADER skill: junior_engineer_v2 is in
    HOLD_UNTIL_UPSTREAM_LEADER_SKILLS, so the watchdog must omit the
    "approve via terminate_child" wording and instead instruct the leader to
    only send_message rework, deferring teardown to runtime cascade.
    """
    o, leader, _child = _make_hold_leader_with_pending_child(
        tmp_path,
        leader_skill="junior_engineer_v2",
        child_skill="junior_engineer",
    )

    run(o._watchdog_tick())

    bodies = _drain_inbox(leader)
    assert len(bodies) == 1, f"expected 1 ping, got {len(bodies)}: {bodies!r}"
    body = bodies[0]

    # Hold wording present.
    assert "do NOT call terminate_child" in body, body
    assert "cascade-terminate" in body, body
    assert 'send_message(to="C1"' in body, body
    # Legacy "approve via terminate_child" wording absent.
    assert 'Call mcp__beidou__terminate_child(agent_id="C1") (approve)' not in body, body
    # v2 committee wording absent (this is a hold-leader path, not committee).
    assert "committee members survive" not in body, body
    # Standard envelope still wraps it.
    assert "[REVIEW REQUIRED — STILL PENDING" in body, body


def test_review_ping_for_software_architect_leader_uses_hold_wording(tmp_path: Path) -> None:
    """software_architect leader (also in HOLD_UNTIL_UPSTREAM_LEADER_SKILLS) → hold wording."""
    o, leader, _child = _make_hold_leader_with_pending_child(
        tmp_path,
        leader_skill="software_architect",
        child_skill="engineer_advisor",  # any non-v2-committee child
    )
    # engineer_advisor IS in V2_DESIGN_COMMITTEE_SKILLS, which would short-circuit.
    # Pick a clearly non-committee child instead.
    o2, leader2, _c2 = _make_hold_leader_with_pending_child(
        tmp_path,
        leader_skill="software_architect",
        child_skill="qa_engineer",
    )

    run(o2._watchdog_tick())

    bodies = _drain_inbox(leader2)
    assert len(bodies) == 1, f"expected 1 ping, got {bodies!r}"
    body = bodies[0]
    assert "do NOT call terminate_child" in body, body
    assert "cascade-terminate" in body, body


def test_review_pings_dedup_when_leader_review_pending(tmp_path: Path) -> None:
    """Pass A leader-level dedup: when leader is itself review_pending, its
    children's pings are suppressed entirely. With 8 children all
    review_pending under a leader who is also review_pending, the watchdog
    must not deliver any ping to the leader for those children, and must not
    escalate them to the user gateway.
    """
    from test_review_gate import _make_orchestrator, _seed_agent, _seed_team, _TM_TOP

    o, _ = _make_orchestrator(tmp_path)
    # Three-level hierarchy: U (root, USER_SENTINEL) -> L (the parked leader)
    # -> C1..C8 (parked children).
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    # U is implicit (USER_SENTINEL); L lives in _TM_TOP and leads tm_child.
    leader = _seed_agent(o, "L", _TM_TOP, role="leader", skill="junior_engineer_v2")
    # L is itself in review_pending (waiting for its own upstream to approve).
    leader.review_pending = True
    leader.review_pending_ts = time.time() - 1000.0
    leader.review_ping_count = 0

    children: list[AgentRecord] = []
    for i in range(8):
        cid = f"C{i+1}"
        c = _seed_agent(o, cid, "tm_child", skill="junior_engineer")
        c.review_pending = True
        c.review_pending_ts = time.time() - 1000.0
        c.review_ping_count = 0
        children.append(c)

    # Capture escalate events on the orchestrator.
    escalate_events: list[dict] = []
    real_emit = o.emit_event

    def _capture(event_name: str, data: dict, *args, **kwargs):
        if event_name == "review.escalated_to_user":
            escalate_events.append(dict(data))
        return real_emit(event_name, data, *args, **kwargs)

    o.emit_event = _capture  # type: ignore[assignment]

    # Tick the watchdog three times to push past the third-ping escalation
    # threshold for any child that managed to slip through.
    run(o._watchdog_tick())
    # Reset their timestamps so the "delta < REVIEW_PING_INTERVAL_S" gate
    # doesn't suppress on later ticks.
    for c in children:
        c.review_pending_ts = time.time() - 1000.0
    run(o._watchdog_tick())
    for c in children:
        c.review_pending_ts = time.time() - 1000.0
    run(o._watchdog_tick())

    # Children's bodies in the leader inbox should NOT include any ping
    # referring to a child id (C1..C8). The leader's own escalation (a
    # ping addressed to L's upstream — USER_SENTINEL — is skipped because
    # leader_team is _TM_TOP whose leader is USER_SENTINEL), so leader inbox
    # should be empty for child-pings.
    bodies = _drain_inbox(leader)
    for b in bodies:
        for cid in (f"C{i+1}" for i in range(8)):
            assert cid not in b, (
                f"Pass A delivered a ping referencing child {cid} to the parked leader: {b!r}"
            )

    # No child should have been escalated to the user gateway.
    escalated_child_ids = {e.get("agent_id") for e in escalate_events}
    for cid in (f"C{i+1}" for i in range(8)):
        assert cid not in escalated_child_ids, (
            f"Pass A escalated child {cid} to user gateway despite leader being parked: {escalate_events!r}"
        )

    # And no child should have had its review_ping_count incremented past 0
    # since the dedup short-circuit happens before the per-child ping logic.
    for c in children:
        assert c.review_ping_count == 0, (
            f"child {c.agent_id} review_ping_count incremented to {c.review_ping_count} despite leader-level dedup"
        )
