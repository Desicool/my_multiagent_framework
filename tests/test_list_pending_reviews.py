"""Tests for the list_pending_reviews primitive.

bd issue 7gm: Add list_pending_reviews() so a leader can enumerate direct
children currently in completion_pending=True state.

The FakeOrchestrator here is intentionally self-contained (same pattern as
test_sdk_agent.py and test_primitives_core.py) — tests/ has no __init__.py so
cross-file imports are not available.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import pytest

from beidou.primitives.core import (
    INBOX_CAP,
    GatewayDeclined,
    Message,
    Peer,
    PrimitiveError,
    list_pending_reviews,
)


# ---------------------------------------------------------------------------
# Minimal FakeOrchestrator that satisfies the full Protocol.
# Includes the completion-review accessor methods required by list_pending_reviews.
# ---------------------------------------------------------------------------


@dataclass
class _AgentRec:
    agent_id: str
    task_id: str
    team_id: str
    role: str = "member"
    status: str = "working"
    skill_name: str = ""
    completion_pending: bool = False
    completion_pending_ts: Optional[float] = None
    last_status_detail: str = ""


@dataclass
class _TeamRec:
    team_id: str
    name: str
    leader_id: str
    depth: int
    members: list[str] = field(default_factory=list)


class FakeOrchestrator:
    def __init__(self) -> None:
        self.agents: dict[str, _AgentRec] = {}
        self.teams: dict[str, _TeamRec] = {}
        self.inboxes: dict[str, asyncio.Queue] = {}
        self.events: list[tuple[str, dict]] = []
        self.statuses: list[tuple[str, str, Optional[str]]] = []
        self.gateway_available: bool = True
        self.gateway_answer: str = "sure"
        self.gateway_decline: bool = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._spawn_fail: Optional[PrimitiveError] = None

    # --- test-only helpers -------------------------------------------------

    def register_agent(
        self,
        agent_id: str,
        task_id: str,
        team_id: str,
        role: str = "member",
    ) -> None:
        self.agents[agent_id] = _AgentRec(agent_id, task_id, team_id, role)
        self.inboxes[agent_id] = asyncio.Queue(maxsize=INBOX_CAP)
        if team_id in self.teams and agent_id not in self.teams[team_id].members:
            self.teams[team_id].members.append(agent_id)

    def register_team(
        self,
        team_id: str,
        name: str,
        leader_id: str,
        depth: int,
        members: Optional[list[str]] = None,
    ) -> None:
        self.teams[team_id] = _TeamRec(team_id, name, leader_id, depth, list(members or []))

    # --- Protocol: registry ------------------------------------------------

    def agent_exists(self, agent_id: str) -> bool:
        return agent_id in self.agents

    def agent_task(self, agent_id: str) -> str:
        return self.agents[agent_id].task_id

    def agent_team(self, agent_id: str) -> str:
        return self.agents[agent_id].team_id

    def leader_of(self, team_id: str) -> str:
        return self.teams[team_id].leader_id

    def teams_led_by(self, agent_id: str) -> list[str]:
        return [tid for tid, t in self.teams.items() if t.leader_id == agent_id]

    def team_depth(self, team_id: str) -> int:
        return self.teams[team_id].depth

    def team_members(self, team_id: str) -> list[str]:
        return list(self.teams[team_id].members)

    def peer_snapshot(self, agent_id: str, scope: str) -> list[Peer]:
        return []

    # --- Protocol: inbox ---------------------------------------------------

    async def inbox_put(self, recipient: str, msg: Message) -> None:
        self.inboxes[recipient].put_nowait(msg)

    def inbox_size(self, agent_id: str) -> int:
        return self.inboxes[agent_id].qsize()

    # --- Protocol: team spawn ----------------------------------------------

    async def spawn_team(self, leader_id: str, name: str, task: str,
                         roles: list[dict], rules: list[str]) -> dict:
        raise NotImplementedError("not needed for list_pending_reviews tests")

    async def create_team_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._locks:
            self._locks[agent_id] = asyncio.Lock()
        return self._locks[agent_id]

    # --- Protocol: observability / gateway ---------------------------------

    def emit_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    async def gateway_ask_user(self, caller_id: str, question: str,
                               context: Optional[str]) -> str:
        if self.gateway_decline:
            raise GatewayDeclined("nope")
        return self.gateway_answer

    async def gateway_ask_user_structured(self, caller_id: str, questions: list,
                                          context: Optional[str]) -> dict:
        if self.gateway_decline:
            raise GatewayDeclined("nope")
        return {
            "answers": [{"selected_labels": [], "text": self.gateway_answer}],
            "answer_text": self.gateway_answer,
        }

    def is_gateway_available(self) -> bool:
        return self.gateway_available

    def record_status(self, caller_id: str, state: str, detail: Optional[str]) -> None:
        self.statuses.append((caller_id, state, detail))

    def was_terminated(self, caller_id: str) -> bool:
        return False

    # --- Protocol: completion-review accessors -----------------------------

    def agent_skill_name(self, agent_id: str) -> str:
        return self.agents[agent_id].skill_name

    def agent_completion_pending(self, agent_id: str) -> bool:
        return self.agents[agent_id].completion_pending

    def agent_completion_pending_ts(self, agent_id: str) -> Optional[float]:
        return self.agents[agent_id].completion_pending_ts

    def agent_last_status_detail(self, agent_id: str) -> str:
        return self.agents[agent_id].last_status_detail


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make(leader_id: str = "L") -> FakeOrchestrator:
    """Return an orchestrator with a root team led by ``leader_id``."""
    o = FakeOrchestrator()
    o.register_team("root", "root", leader_id=leader_id, depth=0, members=[])
    o.register_agent(leader_id, task_id="tsk_1", team_id="root", role="leader")
    return o


def _add_child(
    o: FakeOrchestrator,
    agent_id: str,
    team_id: str,
    *,
    role: str = "worker",
    skill: str = "junior_engineer",
    completion_pending: bool = False,
    completion_pending_ts: Optional[float] = None,
    last_status_detail: str = "",
) -> None:
    """Register a child agent under ``team_id`` with optional pending state."""
    o.register_agent(agent_id, task_id="tsk_1", team_id=team_id, role=role)
    rec = o.agents[agent_id]
    rec.skill_name = skill
    rec.completion_pending = completion_pending
    rec.completion_pending_ts = completion_pending_ts
    rec.last_status_detail = last_status_detail


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_no_pending_children_returns_empty():
    """Leader with no children at all must return []."""
    o = _make("L")
    result = list_pending_reviews(o, caller_id="L")
    assert result == []


def test_no_pending_children_with_non_pending_children():
    """Leader has children but none are pending — must return []."""
    o = _make("L")
    o.register_team("team1", "team1", leader_id="L", depth=1, members=[])
    _add_child(o, "C1", "team1", completion_pending=False)
    _add_child(o, "C2", "team1", completion_pending=False)

    result = list_pending_reviews(o, caller_id="L")
    assert result == []


def test_pending_child_returned():
    """A child with completion_pending=True must appear in the result."""
    o = _make("L")
    o.register_team("team1", "team1", leader_id="L", depth=1, members=[])
    ts = time.time() - 10.0
    _add_child(
        o,
        "C1",
        "team1",
        skill="junior_engineer",
        completion_pending=True,
        completion_pending_ts=ts,
        last_status_detail="Finished the module.",
    )

    result = list_pending_reviews(o, caller_id="L")
    assert len(result) == 1

    entry = result[0]
    assert entry["agent_id"] == "C1"
    assert entry["role"] == "junior_engineer"
    assert entry["completion_pending_ts"] == ts
    assert entry["summary"] == "Finished the module."
    # age_s should be a positive float, approximately 10 s.
    assert entry["age_s"] is not None
    assert entry["age_s"] > 9.0
    assert entry["age_s"] < 30.0  # generous upper bound


def test_non_pending_children_excluded():
    """Only the pending child must appear; the non-pending sibling must not."""
    o = _make("L")
    o.register_team("team1", "team1", leader_id="L", depth=1, members=[])
    ts = time.time() - 5.0
    _add_child(o, "C_pending", "team1", completion_pending=True, completion_pending_ts=ts)
    _add_child(o, "C_working", "team1", completion_pending=False)

    result = list_pending_reviews(o, caller_id="L")
    assert len(result) == 1
    assert result[0]["agent_id"] == "C_pending"


def test_sorted_by_oldest_first():
    """Three pending children must be ordered by completion_pending_ts ascending."""
    o = _make("L")
    o.register_team("team1", "team1", leader_id="L", depth=1, members=[])

    now = time.time()
    _add_child(o, "C_newest", "team1", completion_pending=True, completion_pending_ts=now - 1.0)
    _add_child(o, "C_oldest", "team1", completion_pending=True, completion_pending_ts=now - 100.0)
    _add_child(o, "C_middle", "team1", completion_pending=True, completion_pending_ts=now - 50.0)

    result = list_pending_reviews(o, caller_id="L")
    assert len(result) == 3
    ids = [r["agent_id"] for r in result]
    assert ids == ["C_oldest", "C_middle", "C_newest"]


def test_caller_excluded_from_results():
    """The caller itself must be excluded even if it appears as a team member."""
    o = _make("L")
    o.register_team("team1", "team1", leader_id="L", depth=1, members=[])
    # Manually add L as a member of its own child team (edge case from spec).
    o.teams["team1"].members.append("L")
    # Force L's record to look pending.
    o.agents["L"].completion_pending = True
    o.agents["L"].completion_pending_ts = time.time() - 5.0

    result = list_pending_reviews(o, caller_id="L")
    assert all(r["agent_id"] != "L" for r in result)


def test_null_ts_handled_gracefully():
    """A pending child with completion_pending_ts=None must be returned with age_s=None."""
    o = _make("L")
    o.register_team("team1", "team1", leader_id="L", depth=1, members=[])
    _add_child(
        o,
        "C_no_ts",
        "team1",
        completion_pending=True,
        completion_pending_ts=None,
        last_status_detail="Done but no ts.",
    )

    result = list_pending_reviews(o, caller_id="L")
    assert len(result) == 1
    entry = result[0]
    assert entry["agent_id"] == "C_no_ts"
    assert entry["completion_pending_ts"] is None
    assert entry["age_s"] is None


def test_null_ts_sorts_after_timestamped():
    """A pending child with no ts must appear after those with timestamps."""
    o = _make("L")
    o.register_team("team1", "team1", leader_id="L", depth=1, members=[])

    now = time.time()
    _add_child(o, "C_with_ts", "team1", completion_pending=True, completion_pending_ts=now - 5.0)
    _add_child(o, "C_no_ts", "team1", completion_pending=True, completion_pending_ts=None)

    result = list_pending_reviews(o, caller_id="L")
    assert len(result) == 2
    assert result[0]["agent_id"] == "C_with_ts"
    assert result[1]["agent_id"] == "C_no_ts"
