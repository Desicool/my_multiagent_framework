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
    """Set up: leader L (real agent, has inbox) and child C1 with completion_pending."""
    o, _ = _make_orchestrator(tmp_path)
    # Leader's team is _TM_TOP; the child's team has L as its leader.
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    leader = _seed_agent(o, "L", _TM_TOP, role="leader")
    child = _seed_agent(o, "C1", "tm_child", skill=skill)
    child.completion_pending = True
    # Force the ping threshold: pretend completion_pending fired 1000s ago.
    child.completion_pending_ts = time.time() - 1000.0
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
