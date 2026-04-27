"""Layer 3 — leader-chain routing for ask_user.

Covers:
- QuestionBroker.ask with ctx.parent set lands the question in the leader's
  ``_agent_inbox`` AND wakes the leader by posting a system message into
  the orchestrator's inbox queue.
- QuestionBroker.ask with ctx.parent=None goes straight to the gateway
  (existing behaviour, preserved).
- answer_question primitive resolves the asker's future and rejects
  non-holders.
- escalate_question primitive walks one hop up the chain and posts to
  the user gateway when the next hop is the user sentinel.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import pytest

from beidou.inbox import QuestionBroker
from beidou.primitives.core import (
    Message,
    PrimitiveError,
    answer_question,
    escalate_question,
)


# ---------------------------------------------------------------------------
# Tiny duck-typed fakes
# ---------------------------------------------------------------------------


class _FakeGateway:
    def __init__(self) -> None:
        self.surfaced: list[str] = []

    async def surface_question(self, q, broker) -> None:
        self.surfaced.append(q.qid)


class _FakeEmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def emit(self, event: str, **kwargs) -> None:
        self.calls.append((event, kwargs))


class _AgentRec:
    def __init__(self, agent_id: str, team_id: Optional[str]) -> None:
        self.agent_id = agent_id
        self.team_id = team_id
        self.inbox: asyncio.Queue = asyncio.Queue()


class _TeamRec:
    def __init__(self, team_id: str, leader_id: str) -> None:
        self.team_id = team_id
        self.leader_id = leader_id


class _FakeOrch:
    """Minimal duck-typed orchestrator the broker + primitives can drive."""

    task_id = "tsk_chain_test"

    def __init__(self) -> None:
        self.emitter = _FakeEmitter()
        self._agents: dict[str, _AgentRec] = {}
        self._teams: dict[str, _TeamRec] = {}
        self.gateway: Any = None  # set later

    async def inbox_put(self, agent_id: str, msg: Message) -> None:
        await self._agents[agent_id].inbox.put(msg)


class _ParentRef:
    __slots__ = ("agent_id",)

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


class _Ctx:
    def __init__(self, agent_id: str, parent_id: Optional[str], orch: _FakeOrch) -> None:
        self.agent_id = agent_id
        self.parent: Optional[_ParentRef] = _ParentRef(parent_id) if parent_id else None
        self._kv: dict[str, Any] = {
            "emitter": orch.emitter,
            "task_id": orch.task_id,
            "orchestrator": orch,
        }

    def get(self, k: str, default: Any = None) -> Any:
        return self._kv.get(k, default)


# ---------------------------------------------------------------------------
# Fixtures-ish helper
# ---------------------------------------------------------------------------


def _make_world():
    """Build a tiny task: leader L (in team T1, USER_SENTINEL=root) and member M.

    Uses ``USER_SENTINEL`` only via the team leader; the broker flow does NOT
    need the sentinel itself, just the team_id mapping.
    """
    orch = _FakeOrch()
    gw = _FakeGateway()
    broker = QuestionBroker()
    broker.set_gateway(gw)
    orch.gateway = gw
    orch._agents["L"] = _AgentRec("L", team_id="t1")
    orch._agents["M"] = _AgentRec("M", team_id="t1")
    orch._teams["t1"] = _TeamRec("t1", leader_id="L")
    return orch, broker, gw


def _free_text_q():
    return [{"question": "Vite or CDN?", "header": "tool", "multiSelect": False, "options": []}]


# ---------------------------------------------------------------------------
# 1. Chain routing — question lands in leader inbox + wakes leader
# ---------------------------------------------------------------------------


def test_ask_with_parent_lands_in_leader_inbox_and_wakes_leader():
    orch, broker, gw = _make_world()
    ctx = _Ctx(agent_id="M", parent_id="L", orch=orch)

    async def body():
        # Don't await broker.ask itself — it blocks on the future. Schedule it.
        ask_task = asyncio.create_task(broker.ask(ctx, _free_text_q(), context_hint="bg"))
        # Yield so the broker's create_task wake-up scheduling runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The question should have landed in L's _agent_inbox, NOT been
        # surfaced to the gateway.
        assert gw.surfaced == [], f"Expected no gateway surface, got {gw.surfaced}"
        qids_for_leader = list(broker._agent_inbox.get("L", []))
        assert len(qids_for_leader) == 1

        qid = qids_for_leader[0]
        assert qid in broker._pending

        # And a wake-up system message should have been posted to L's queue.
        leader_inbox = orch._agents["L"].inbox
        assert leader_inbox.qsize() == 1
        wake_msg: Message = leader_inbox.get_nowait()
        assert wake_msg.kind == "system"
        assert wake_msg.from_id == "beidou"
        assert qid in wake_msg.content
        assert "answer_question" in wake_msg.content
        assert "escalate_question" in wake_msg.content

        # Resolve the question so ask_task can finish (otherwise the test
        # would leak the pending task on teardown).
        broker.resolve_answer(qid, [{"selected_labels": [], "text": "Vite"}])
        result = await asyncio.wait_for(ask_task, timeout=1.0)
        # answer_text prefixes the sub-question header when present.
        assert "Vite" in result.get("answer_text", "")

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 2. parent=None goes straight to the gateway (preserves direct path)
# ---------------------------------------------------------------------------


def test_ask_with_no_parent_surfaces_to_gateway():
    orch, broker, gw = _make_world()
    ctx = _Ctx(agent_id="L", parent_id=None, orch=orch)

    async def body():
        ask_task = asyncio.create_task(broker.ask(ctx, _free_text_q(), context_hint=None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Surfaced to gateway; nothing in any leader inbox.
        assert len(gw.surfaced) == 1
        qid = gw.surfaced[0]
        assert qid in broker._pending
        assert all(qid not in lst for lst in broker._agent_inbox.values())

        # No wake-up message should have been posted to anyone.
        assert orch._agents["L"].inbox.qsize() == 0
        assert orch._agents["M"].inbox.qsize() == 0

        broker.resolve_answer(qid, [{"selected_labels": [], "text": "ok"}])
        await asyncio.wait_for(ask_task, timeout=1.0)

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 3. answer_question primitive — only the holder may answer
# ---------------------------------------------------------------------------


class _BridgeStub:
    """Just exposes ``_broker`` so primitives' _broker_from() finds it."""

    def __init__(self, broker: QuestionBroker) -> None:
        self._broker = broker


def test_answer_question_resolves_pending_for_holder():
    orch, broker, _ = _make_world()
    orch.gateway = _BridgeStub(broker)
    ctx = _Ctx(agent_id="M", parent_id="L", orch=orch)

    async def body():
        ask_task = asyncio.create_task(broker.ask(ctx, _free_text_q(), context_hint=None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        qid = list(broker._agent_inbox["L"])[0]

        # Holder L answers via the primitive.
        out = await answer_question(
            orch,
            caller_id="L",
            qid=qid,
            answers=[{"selected_labels": [], "text": "Vite, from prior PM answer"}],
        )
        assert out == {"ok": True, "qid": qid}

        # Asker's future resolves with that text.
        result = await asyncio.wait_for(ask_task, timeout=1.0)
        assert "Vite" in result.get("answer_text", "")

    asyncio.run(body())


def test_answer_question_rejects_non_holder():
    orch, broker, _ = _make_world()
    orch.gateway = _BridgeStub(broker)
    ctx = _Ctx(agent_id="M", parent_id="L", orch=orch)

    async def body():
        ask_task = asyncio.create_task(broker.ask(ctx, _free_text_q(), context_hint=None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        qid = list(broker._agent_inbox["L"])[0]

        # Some other agent (the asker M itself) tries to answer — must reject.
        with pytest.raises(PrimitiveError) as exc:
            await answer_question(
                orch, caller_id="M", qid=qid, answers=[{"selected_labels": [], "text": "x"}]
            )
        assert exc.value.code == "not_holder"

        # Cleanup so the ask_task doesn't leak.
        broker.resolve_answer(qid, [{"selected_labels": [], "text": "ok"}])
        await asyncio.wait_for(ask_task, timeout=1.0)

    asyncio.run(body())


# ---------------------------------------------------------------------------
# 4. escalate_question primitive — to user when next-up is sentinel-or-none
# ---------------------------------------------------------------------------


def test_escalate_question_to_user_when_holder_has_no_leader():
    """L is leader of t1; if t1's leader is USER_SENTINEL (root), escalating
    L's pending question moves it to the gateway."""
    from beidou.orchestrator import USER_SENTINEL

    orch, broker, gw = _make_world()
    # Re-wire team t1 so that L's "leader" is the user sentinel — i.e. L is
    # acting as the root for this test.
    orch._teams["t1"].leader_id = USER_SENTINEL
    orch.gateway = _BridgeStub(broker)
    ctx = _Ctx(agent_id="M", parent_id="L", orch=orch)

    async def body():
        ask_task = asyncio.create_task(broker.ask(ctx, _free_text_q(), context_hint=None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        qid = list(broker._agent_inbox["L"])[0]

        # L escalates — next hop is USER_SENTINEL → goes to user gateway.
        out = await escalate_question(orch, caller_id="L", qid=qid, reason="user owns this")
        assert out["ok"] is True
        assert out["new_holder"] is None

        # Gateway saw the question; L's _agent_inbox no longer has it.
        await asyncio.sleep(0)
        assert qid in gw.surfaced
        assert qid not in broker._agent_inbox.get("L", [])

        # Cleanup.
        broker.resolve_answer(qid, [{"selected_labels": [], "text": "user said vite"}])
        result = await asyncio.wait_for(ask_task, timeout=1.0)
        assert "vite" in result.get("answer_text", "")

    asyncio.run(body())


def test_escalate_question_rejects_non_holder():
    orch, broker, _ = _make_world()
    orch.gateway = _BridgeStub(broker)
    ctx = _Ctx(agent_id="M", parent_id="L", orch=orch)

    async def body():
        ask_task = asyncio.create_task(broker.ask(ctx, _free_text_q(), context_hint=None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        qid = list(broker._agent_inbox["L"])[0]

        with pytest.raises(PrimitiveError) as exc:
            await escalate_question(orch, caller_id="M", qid=qid, reason="x")
        assert exc.value.code == "not_holder"

        broker.resolve_answer(qid, [{"selected_labels": [], "text": "ok"}])
        await asyncio.wait_for(ask_task, timeout=1.0)

    asyncio.run(body())
