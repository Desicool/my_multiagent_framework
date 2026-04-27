"""End-to-end tests for beidou/primitives/core.py.

No pytest-asyncio dependency: each test is a plain function that uses
``asyncio.run`` to drive an ``async`` body. The FakeOrchestrator is a simple
in-memory implementation of the Orchestrator Protocol -- not a mock -- so
the primitives are exercised through the same code path they'd use in
production.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import pytest

from beidou.primitives.core import (
    FAN_OUT_CAP,
    INBOX_CAP,
    MAX_DEPTH,
    GatewayDeclined,
    Message,
    Peer,
    PrimitiveError,
    ask_user,
    create_team,
    list_peers,
    report_status,
    send_message,
    terminate_child,
)


# ---------------------------------------------------------------------------
# Fake orchestrator: in-memory, protocol-compatible.
# ---------------------------------------------------------------------------


@dataclass
class _AgentRec:
    agent_id: str
    task_id: str
    team_id: str          # parent team the agent is a member of
    role: str = "member"
    status: str = "working"
    skill_name: str = ""
    completion_pending: bool = False
    completion_pending_ts: Optional[float] = None
    last_status_detail: str = ""
    terminate_consumed: bool = False


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
        # Alias so core.py's ``orch._agents.get(agent_id)`` works without
        # hitting the real Orchestrator's private attribute name.
        self._agents = self.agents
        self.teams: dict[str, _TeamRec] = {}
        self.inboxes: dict[str, asyncio.Queue] = {}
        self.events: list[tuple[str, dict]] = []
        self.statuses: list[tuple[str, str, Optional[str]]] = []
        self.gateway_available: bool = True
        self.gateway_answer: str = "sure"
        self.gateway_decline: bool = False
        self.gateway_raise_generic: Optional[Exception] = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._spawn_fail: Optional[PrimitiveError] = None
        self._team_counter = 0
        self._agent_counter = 0

    # --- test-only helpers -------------------------------------------------

    def register_agent(
        self,
        agent_id: str,
        task_id: str,
        team_id: str,
        role: str = "member",
    ) -> None:
        self.agents[agent_id] = _AgentRec(agent_id, task_id, team_id, role)
        # Bound to INBOX_CAP so back-pressure actually fires on overflow.
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

    # --- Orchestrator Protocol: registry ----------------------------------

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
        out: list[Peer] = []
        if scope == "team":
            my_team = self.agent_team(agent_id)
            for mid in self.team_members(my_team):
                if mid == agent_id:
                    continue
                a = self.agents[mid]
                out.append(Peer(mid, a.role, a.team_id, a.status, self.teams_led_by(mid)))
        elif scope == "children":
            for tid in self.teams_led_by(agent_id):
                for mid in self.team_members(tid):
                    a = self.agents[mid]
                    out.append(Peer(mid, a.role, a.team_id, a.status, self.teams_led_by(mid)))
        elif scope == "all":
            my_task = self.agent_task(agent_id)
            for aid, a in self.agents.items():
                if aid == agent_id or a.task_id != my_task:
                    continue
                out.append(Peer(aid, a.role, a.team_id, a.status, self.teams_led_by(aid)))
        return out

    # --- Orchestrator Protocol: inbox -------------------------------------

    async def inbox_put(self, recipient: str, msg: Message) -> None:
        # Primitives enforce the cap; here we just honour put_nowait to
        # surface bugs rather than silently blocking.
        self.inboxes[recipient].put_nowait(msg)

    async def inbox_drain(self, agent_id: str) -> list[Message]:
        q = self.inboxes[agent_id]
        out: list[Message] = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    async def inbox_get_one(
        self,
        agent_id: str,
        from_filter: Optional[str],
        timeout: float,
    ) -> Optional[Message]:
        q = self.inboxes[agent_id]
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(q.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if from_filter is None or msg.from_id == from_filter:
                return msg
            # drop non-matching; keep waiting.

    def inbox_size(self, agent_id: str) -> int:
        return self.inboxes[agent_id].qsize()

    # --- Orchestrator Protocol: team spawn --------------------------------

    async def spawn_team(
        self,
        leader_id: str,
        name: str,
        task: str,
        roles: list[dict],
        rules: list[str],
    ) -> dict:
        if self._spawn_fail is not None:
            raise self._spawn_fail
        self._team_counter += 1
        team_id = f"team_{self._team_counter}"
        leader_team = self.agent_team(leader_id)
        depth = self.team_depth(leader_team) + 1
        members: list[dict] = []
        for r in roles:
            self._agent_counter += 1
            aid = f"ag_{self._agent_counter}"
            self.agents[aid] = _AgentRec(
                aid,
                self.agent_task(leader_id),
                team_id,
                r.get("role", "member"),
            )
            self.inboxes[aid] = asyncio.Queue(maxsize=INBOX_CAP)
            members.append({"agent_id": aid, "role": r.get("role", "member")})
        self.teams[team_id] = _TeamRec(
            team_id,
            name,
            leader_id,
            depth,
            [m["agent_id"] for m in members],
        )
        return {"team_id": team_id, "members": members}

    async def create_team_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._locks:
            self._locks[agent_id] = asyncio.Lock()
        return self._locks[agent_id]

    # --- Orchestrator Protocol: observability -----------------------------

    def emit_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    async def gateway_ask_user(
        self,
        caller_id: str,
        question: str,
        context: Optional[str],
    ) -> str:
        if self.gateway_decline:
            raise GatewayDeclined("nope")
        if self.gateway_raise_generic is not None:
            raise self.gateway_raise_generic
        return self.gateway_answer

    async def gateway_ask_user_structured(
        self,
        caller_id: str,
        questions: list,
        context: Optional[str],
    ) -> dict:
        if self.gateway_decline:
            raise GatewayDeclined("nope")
        if self.gateway_raise_generic is not None:
            raise self.gateway_raise_generic
        return {
            "answers": [{"selected_labels": [], "text": self.gateway_answer}],
            "answer_text": self.gateway_answer,
        }

    def is_gateway_available(self) -> bool:
        return self.gateway_available

    def record_status(self, caller_id: str, state: str, detail: Optional[str]) -> None:
        self.statuses.append((caller_id, state, detail))

    def was_terminated(self, caller_id: str) -> bool:
        # Test default: no terminate sentinel consumed. Tests that care flip
        # this via the ``_terminated`` set they manage directly.
        return False

    # --- Completion-review accessors (used by list_pending_reviews) --------

    def agent_skill_name(self, agent_id: str) -> str:
        return self.agents[agent_id].skill_name

    def agent_completion_pending(self, agent_id: str) -> bool:
        return self.agents[agent_id].completion_pending

    def agent_completion_pending_ts(self, agent_id: str) -> Optional[float]:
        return self.agents[agent_id].completion_pending_ts

    def agent_last_status_detail(self, agent_id: str) -> str:
        return self.agents[agent_id].last_status_detail

    def agent_name(self, agent_id: str) -> str | None:
        # FakeOrchestrator agents have no name field; return None for tests.
        return None


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------


def _build(default_team_depth: int = 0) -> FakeOrchestrator:
    """Orchestrator with a root team, leader R, and two peers A and B."""
    o = FakeOrchestrator()
    o.register_team("root", "root", leader_id="R", depth=default_team_depth, members=[])
    o.register_agent("R", task_id="tsk_1", team_id="root", role="leader")
    o.register_agent("A", task_id="tsk_1", team_id="root", role="worker")
    o.register_agent("B", task_id="tsk_1", team_id="root", role="worker")
    return o


def run(coro) -> object:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


def test_send_message_happy_path():
    o = _build()

    async def body():
        out = await send_message(o, caller_id="A", to="B", content="hi")
        assert out["delivered"] is True
        assert isinstance(out["message_id"], str) and len(out["message_id"]) > 10
        assert o.inbox_size("B") == 1
        assert any(ev[0] == "message_sent" for ev in o.events)

    run(body())


def test_send_message_unknown_recipient():
    o = _build()

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await send_message(o, caller_id="A", to="ghost", content="x")
        assert ei.value.code == "unknown_recipient"

    run(body())


def test_send_message_task_mismatch():
    o = _build()
    # A second task with its own agent.
    o.register_team("root2", "root2", leader_id="X", depth=0)
    o.register_agent("X", task_id="tsk_2", team_id="root2")

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await send_message(o, caller_id="A", to="X", content="hi")
        assert ei.value.code == "task_mismatch"

    run(body())


def test_send_message_inbox_full():
    o = _build()

    async def body():
        # Fill B's inbox to the cap.
        for _ in range(INBOX_CAP):
            await o.inbox_put("B", Message("A", "x", time.time(), "m", "user"))
        assert o.inbox_size("B") == INBOX_CAP
        with pytest.raises(PrimitiveError) as ei:
            await send_message(o, caller_id="A", to="B", content="one more")
        assert ei.value.code == "inbox_full"

    run(body())


# ---------------------------------------------------------------------------
# list_peers
# ---------------------------------------------------------------------------


def test_list_peers_team_scope():
    o = _build()

    async def body():
        out = await list_peers(o, caller_id="A", scope="team")
        ids = {p["agent_id"] for p in out["peers"]}
        # A's teammates on root team are R and B (A excluded).
        assert ids == {"R", "B"}

    run(body())


def test_list_peers_invalid_scope():
    o = _build()

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await list_peers(o, caller_id="A", scope="galaxy")
        assert ei.value.code == "invalid_scope"

    run(body())


# ---------------------------------------------------------------------------
# ask_user
# ---------------------------------------------------------------------------


_FREE_TEXT_Q = [
    {
        "question": "proceed?",
        "header": "",
        "multiSelect": False,
        "options": [],
    }
]


def test_ask_user_happy_path():
    o = _build()
    o.gateway_answer = "yes go"

    async def body():
        out = await ask_user(o, caller_id="A", questions=_FREE_TEXT_Q)
        assert out == {
            "answers": [{"selected_labels": [], "text": "yes go"}],
            "answer_text": "yes go",
        }

    run(body())


def test_ask_user_gateway_unavailable():
    o = _build()
    o.gateway_available = False

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await ask_user(o, caller_id="A", questions=_FREE_TEXT_Q)
        assert ei.value.code == "gateway_unavailable"

    run(body())


# ---------------------------------------------------------------------------
# ask_user — validation tests
# ---------------------------------------------------------------------------


def test_ask_user_empty_questions_list():
    o = _build()

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await ask_user(o, caller_id="A", questions=[])
        assert ei.value.code == "invalid_input"

    run(body())


def test_ask_user_too_many_questions():
    o = _build()
    q = {"question": "x?", "header": "", "multiSelect": False, "options": []}

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await ask_user(o, caller_id="A", questions=[q, q, q, q, q])
        assert ei.value.code == "invalid_input"

    run(body())


def test_ask_user_header_too_long():
    o = _build()

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await ask_user(
                o,
                caller_id="A",
                questions=[
                    {
                        "question": "proceed?",
                        "header": "TooLongHeader",  # 13 chars > 12
                        "multiSelect": False,
                        "options": [],
                    }
                ],
            )
        assert ei.value.code == "invalid_input"

    run(body())


def test_ask_user_options_length_one():
    o = _build()

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await ask_user(
                o,
                caller_id="A",
                questions=[
                    {
                        "question": "choose?",
                        "header": "",
                        "multiSelect": False,
                        "options": [{"label": "Only", "description": "sole option"}],
                    }
                ],
            )
        assert ei.value.code == "invalid_input"

    run(body())


def test_ask_user_options_length_five():
    o = _build()
    opt = {"label": "X", "description": "desc"}

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await ask_user(
                o,
                caller_id="A",
                questions=[
                    {
                        "question": "choose?",
                        "header": "",
                        "multiSelect": False,
                        "options": [opt, opt, opt, opt, opt],
                    }
                ],
            )
        assert ei.value.code == "invalid_input"

    run(body())


def test_ask_user_malformed_option_dict():
    o = _build()

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await ask_user(
                o,
                caller_id="A",
                questions=[
                    {
                        "question": "choose?",
                        "header": "",
                        "multiSelect": False,
                        "options": [
                            {"label": "Yes", "description": "affirmative"},
                            "not_a_dict",  # malformed
                        ],
                    }
                ],
            )
        assert ei.value.code == "invalid_input"

    run(body())


# ---------------------------------------------------------------------------
# report_status
# ---------------------------------------------------------------------------


def test_report_status_records_and_emits():
    o = _build()

    async def body():
        out = await report_status(o, caller_id="A", state="done", detail="shipped")
        assert out == {"recorded": True}
        assert o.statuses == [("A", "done", "shipped")]
        assert any(ev[0] == "status" for ev in o.events)

    run(body())


def test_report_status_invalid_state():
    o = _build()

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await report_status(o, caller_id="A", state="exploding")
        assert ei.value.code == "invalid_state"

    run(body())


# ---------------------------------------------------------------------------
# create_team
# ---------------------------------------------------------------------------


def test_create_team_happy_path():
    o = _build()

    async def body():
        roles = [
            {"role": "coder", "skill": "junior_engineer", "description": "x"},
            {"role": "tester", "skill": "junior_engineer", "description": "y"},
        ]
        out = await create_team(o, caller_id="R", name="impl", task="build", roles=roles)
        assert "team_id" in out
        assert len(out["members"]) == 2
        # Self-lead invariant: R is leader of the new team.
        assert o.leader_of(out["team_id"]) == "R"

    run(body())


def test_create_team_fanout_exceeded():
    o = _build()

    async def body():
        roles = [{"role": f"r{i}", "skill": "t"} for i in range(FAN_OUT_CAP + 1)]
        with pytest.raises(PrimitiveError) as ei:
            await create_team(o, caller_id="R", name="big", task="x", roles=roles)
        assert ei.value.code == "fanout_exceeded"

    run(body())


def test_create_team_depth_exceeded():
    o = FakeOrchestrator()
    # Put the caller on a team already at MAX_DEPTH so +1 trips the cap.
    o.register_team("deep", "deep", leader_id="L", depth=MAX_DEPTH)
    o.register_agent("L", task_id="tsk_1", team_id="deep")

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await create_team(
                o,
                caller_id="L",
                name="child",
                task="x",
                roles=[{"role": "r", "skill": "t"}],
            )
        assert ei.value.code == "depth_exceeded"

    run(body())


def test_create_team_concurrent_rejected():
    """Second create_team by the same caller while the first is in flight
    must surface ``concurrent_create_team`` (limits.md #6)."""
    o = _build()

    # Slow down spawn_team so we can hit the lock while it's held.
    original = o.spawn_team
    gate = asyncio.Event()

    async def slow_spawn(**kw):
        await gate.wait()
        return await original(**kw)

    o.spawn_team = slow_spawn  # type: ignore[assignment]

    async def body():
        roles = [{"role": "r", "skill": "t"}]
        first = asyncio.create_task(
            create_team(o, caller_id="R", name="t1", task="x", roles=roles)
        )
        # Let ``first`` advance into the critical section before firing the
        # contender.
        await asyncio.sleep(0.01)
        with pytest.raises(PrimitiveError) as ei:
            await create_team(o, caller_id="R", name="t2", task="x", roles=roles)
        assert ei.value.code == "concurrent_create_team"
        gate.set()
        await first  # let the first call finish cleanly.

    run(body())


# ---------------------------------------------------------------------------
# terminate_child
# ---------------------------------------------------------------------------


def test_terminate_child_happy_path():
    """Happy path: child has reported done (completion_pending=True) → approve path."""
    o = _build()
    # The gate requires the child to have called report_status(state="done") first.
    o.agents["A"].completion_pending = True

    async def body():
        out = await terminate_child(o, caller_id="R", agent_id="A")
        assert out == {"sentinel_posted": True}
        # Drain A's inbox directly to verify the sentinel landed.
        msgs = await o.inbox_drain("A")
        assert len(msgs) == 1
        assert msgs[0].kind == "terminate"
        assert msgs[0].from_id == "beidou"
        assert msgs[0].content == "__terminate__"
        assert any(ev[0] == "terminate_posted" for ev in o.events)
        # No terminate.forced event emitted on the approve path.
        assert not any(ev[0] == "terminate.forced" for ev in o.events)

    run(body())


def test_terminate_child_not_leader():
    o = _build()

    async def body():
        # A does not lead the root team; R does.
        with pytest.raises(PrimitiveError) as ei:
            await terminate_child(o, caller_id="A", agent_id="B")
        assert ei.value.code == "not_leader"

    run(body())


def test_terminate_child_unknown_agent():
    o = _build()

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await terminate_child(o, caller_id="R", agent_id="ghost")
        assert ei.value.code == "unknown_agent"

    run(body())


def test_terminate_child_blocks_when_child_not_done():
    """Completion gate: no force + no pending + not terminated → PrimitiveError."""
    o = _build()
    # completion_pending=False, terminate_consumed=False (defaults)

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await terminate_child(o, caller_id="R", agent_id="A")
        assert ei.value.code == "child_not_pending_review"
        # No terminate sentinel should have been posted.
        assert o.inbox_size("A") == 0

    run(body())


def test_terminate_child_approve_path_works():
    """Completion gate approve path: completion_pending=True passes without force."""
    o = _build()
    o.agents["A"].completion_pending = True

    async def body():
        out = await terminate_child(o, caller_id="R", agent_id="A")
        assert out == {"sentinel_posted": True}
        # Sentinel must land in inbox.
        assert o.inbox_size("A") == 1
        msgs = await o.inbox_drain("A")
        assert msgs[0].kind == "terminate"
        # No terminate.forced event on the approve path.
        assert not any(ev[0] == "terminate.forced" for ev in o.events)

    run(body())


def test_terminate_child_force_overrides_gate_and_emits_event():
    """force=True with a not-done child: gate bypassed, terminate.forced emitted."""
    o = _build()
    # completion_pending=False, terminate_consumed=False (defaults — gate would block)

    async def body():
        out = await terminate_child(o, caller_id="R", agent_id="A", force=True)
        assert out == {"sentinel_posted": True}
        # Sentinel must still land in inbox.
        assert o.inbox_size("A") == 1
        # terminate.forced event emitted with the correct payload.
        forced_events = [ev for ev in o.events if ev[0] == "terminate.forced"]
        assert len(forced_events) == 1
        payload = forced_events[0][1]
        assert payload["reason"] == "leader_force"
        assert payload["caller_id"] == "R"
        assert payload["agent_id"] == "A"
        assert payload["team_id"] == o.agents["A"].team_id

    run(body())


def test_terminate_child_force_when_already_pending_does_not_emit_forced_event():
    """force=True when completion_pending=True: gate is not blocked, no forced event."""
    o = _build()
    o.agents["A"].completion_pending = True

    async def body():
        out = await terminate_child(o, caller_id="R", agent_id="A", force=True)
        assert out == {"sentinel_posted": True}
        # Gate would not have blocked (pending=True), so no terminate.forced event.
        assert not any(ev[0] == "terminate.forced" for ev in o.events)

    run(body())


def test_terminate_child_force_when_already_terminated_idempotent():
    """force=True when terminate_consumed=True: succeeds, no terminate.forced event."""
    o = _build()
    o.agents["A"].terminate_consumed = True

    async def body():
        out = await terminate_child(o, caller_id="R", agent_id="A", force=True)
        assert out == {"sentinel_posted": True}
        # Gate would not have blocked (terminate_consumed=True), so no forced event.
        assert not any(ev[0] == "terminate.forced" for ev in o.events)

    run(body())
