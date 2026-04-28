"""Tests for the QuestionRegistry + orchestrator question-routing refactor.

Covers:
- QuestionRegistry state machine (register, resolve, pop, has_pending_through).
- Orchestrator.post_question routes to parent inbox or user gateway.
- Orchestrator.forward_question updates chain and dispatches.
- Orchestrator.resolve_question sets future and emits event.
- answer_question / escalate_question primitives via the new dispatch path.
- Watchdog Pass B suppresses nudges for intermediate-hop agents.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import pytest

from beidou.questions import PendingQuestion, QuestionRegistry
from beidou.primitives.core import (
    Message,
    PrimitiveError,
    answer_question,
    escalate_question,
)


# ---------------------------------------------------------------------------
# Helpers / tiny fakes
# ---------------------------------------------------------------------------


def _free_text_q() -> list[dict]:
    return [{"question": "Vite or CDN?", "header": "tool", "multiSelect": False, "options": []}]


def _answer(text: str = "Vite") -> list[dict]:
    return [{"selected_labels": [], "text": text}]


class _FakeGateway:
    """Records surface_question calls (new signature)."""

    def __init__(self) -> None:
        self.surfaced: list[tuple[str, str, list[dict]]] = []

    async def surface_question(self, qid: str, body: str, questions: list[dict]) -> None:
        self.surfaced.append((qid, body, questions))

    async def ask(self, caller_id: str, question: str, context: Optional[str]) -> str:
        return "answer"


class _FakeEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event: str, agent_id: str = "", team_id: Any = None, **kwargs) -> None:
        self.events.append((event, {"agent_id": agent_id, "team_id": team_id, **kwargs}))


class _AgentRec:
    def __init__(self, agent_id: str, team_id: Optional[str]) -> None:
        self.agent_id = agent_id
        self.team_id = team_id
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.last_progress_ts: float = time.time()
        self.inflight_tools: int = 0
        self.completion_pending: bool = False
        self.terminate_consumed: bool = False
        self.idle_nudge_count: int = 0


class _TeamRec:
    def __init__(self, team_id: str, leader_id: str) -> None:
        self.team_id = team_id
        self.leader_id = leader_id


USER_SENTINEL = "__user__"


class _FakeOrch:
    """Minimal orchestrator with real _questions registry and routing methods
    needed by the primitives and watchdog tests."""

    task_id = "tsk_chain_test"

    def __init__(self) -> None:
        self.emitter = _FakeEmitter()
        self._agents: dict[str, _AgentRec] = {}
        self._teams: dict[str, _TeamRec] = {}
        self._questions: QuestionRegistry = QuestionRegistry()
        self.gateway: Any = None
        self._root_id: Optional[str] = None

    async def inbox_put(self, agent_id: str, msg: Message) -> None:
        await self._agents[agent_id].inbox.put(msg)

    def emit_event(self, name: str, payload: dict) -> None:
        self.emitter.events.append((name, payload))

    def is_gateway_available(self) -> bool:
        return self.gateway is not None and hasattr(self.gateway, "ask")

    def parent_for_chain(self, caller_id: str) -> Optional[str]:
        rec = self._agents.get(caller_id)
        if rec is None or rec.team_id is None:
            return None
        team = self._teams.get(rec.team_id)
        if team is None:
            return None
        leader = getattr(team, "leader_id", None)
        if leader is None or leader == USER_SENTINEL:
            return None
        return leader

    async def post_question(
        self,
        *,
        asker_id: str,
        parent_id: Optional[str],
        questions: list[dict],
        context_hint: Optional[str],
    ) -> tuple[str, asyncio.Future]:
        qid, future = self._questions.register(asker_id, questions, context_hint, parent_id)
        pq = self._questions.get(qid)
        await self._deliver_question(
            qid, target=parent_id, sender=asker_id,
            questions=questions, context_hint=context_hint,
            chain=pq.chain, escalation=False,
        )
        return qid, future

    async def forward_question(
        self,
        *,
        qid: str,
        by_id: str,
        new_target_id: Optional[str],
        reason: str,
    ) -> dict:
        pq = self._questions.get(qid)
        if pq is None or pq.future.done():
            return {"ok": False, "reason": "stale"}
        self._questions.add_chain_hop(qid, new_target_id if new_target_id is not None else "USER")
        await self._deliver_question(
            qid, target=new_target_id, sender=by_id,
            questions=pq.questions, context_hint=None,
            chain=pq.chain, escalation=True, reason=reason,
        )
        return {"ok": True, "qid": qid, "new_holder": new_target_id}

    async def _deliver_question(
        self,
        qid: str,
        *,
        target: Optional[str],
        sender: str,
        questions: list[dict],
        context_hint: Optional[str],
        chain: list[str],
        escalation: bool,
        reason: Optional[str] = None,
    ) -> None:
        from beidou.questions import render_prompt_text
        prompt_preview = render_prompt_text(questions)
        if len(prompt_preview) > 600:
            prompt_preview = prompt_preview[:600] + "…"
        asker_agent_id = chain[0] if chain else sender
        body = (
            f"[INBOX QUESTION] qid={qid} from {asker_agent_id}\n"
            f"chain: {' → '.join(chain)}\n"
            f"\n{prompt_preview}\n"
        )
        if escalation and reason:
            body += f"\n(escalated by {sender}: {reason})\n"
        if context_hint:
            body += f"\ncontext: {context_hint}\n"
        body += (
            f"\nResolve this BEFORE doing anything else. Two options:\n"
            f"  1. Answer it directly via mcp__beidou__answer_question("
            f"qid=\"{qid}\", answers=[{{\"selected_labels\": [...], \"text\": \"...\"}}, ...])"
            f" if you can answer from what you already know.\n"
            f"  2. Escalate via mcp__beidou__escalate_question("
            f"qid=\"{qid}\", reason=\"...\") if only the next-up reviewer "
            f"or the user can answer.\n"
        )
        if target is None:
            if self.gateway is not None:
                surface = getattr(self.gateway, "surface_question", None)
                if surface is not None:
                    asyncio.create_task(surface(qid, body, questions))
        else:
            msg = Message(
                from_id="beidou",
                content=body,
                ts=time.time(),
                message_id=f"qmsg-{qid}",
                kind="system",
            )
            await self.inbox_put(target, msg)

    def resolve_question(self, qid: str, answers: list[dict], *, answerer: str | None = None, reason: str | None = None) -> dict:
        pq = self._questions.get(qid)
        if pq is None:
            return {"ok": False, "reason": "unknown_qid"}
        if pq.future.done():
            return {"ok": False, "reason": "already_answered"}
        out = self._questions.resolve(qid, answers)
        if not out["ok"]:
            return out
        self.emit_event(
            "question_answered",
            {
                "agent_id": pq.asker_agent_id,
                "qid": qid,
                "asker": pq.asker_agent_id,
                "answerer": answerer,
                "reason": reason[:200] if reason else None,
                "chain_len": len(pq.chain),
                "answers": answers,
                "answer_text": out["answer_text"],
            },
        )
        return {"ok": True}

    async def gateway_ask_user_structured(
        self,
        caller_id: str,
        questions: list[dict],
        context: Optional[str],
    ) -> dict:
        """Direct-to-user path (parent_id=None)."""
        from beidou.questions import render_prompt_text
        qid, future = await self.post_question(
            asker_id=caller_id, parent_id=None,
            questions=questions, context_hint=context,
        )
        self.emit_event("question_asked", {
            "agent_id": caller_id, "qid": qid, "asker": caller_id, "holder": None,
            "prompt": render_prompt_text(questions)[:200], "questions": questions,
        })
        try:
            return await future
        finally:
            self._questions.pop(qid)

    async def gateway_ask_via_chain(
        self,
        caller_id: str,
        questions: list[dict],
        context: Optional[str],
    ) -> dict:
        """Leader-chain path."""
        from beidou.questions import render_prompt_text
        parent_id = self.parent_for_chain(caller_id)
        qid, future = await self.post_question(
            asker_id=caller_id, parent_id=parent_id,
            questions=questions, context_hint=context,
        )
        self.emit_event("question_asked", {
            "agent_id": caller_id, "qid": qid, "asker": caller_id, "holder": parent_id,
            "prompt": render_prompt_text(questions)[:200], "questions": questions,
        })
        try:
            return await future
        finally:
            self._questions.pop(qid)

    def teams_led_by(self, agent_id: str) -> list[str]:
        return [tid for tid, t in self._teams.items() if t.leader_id == agent_id]


def _make_world() -> tuple[_FakeOrch, _FakeGateway]:
    """Build a small task: leader L (in team T1) and member M."""
    orch = _FakeOrch()
    gw = _FakeGateway()
    orch.gateway = gw
    orch._agents["L"] = _AgentRec("L", team_id="t1")
    orch._agents["M"] = _AgentRec("M", team_id="t1")
    orch._teams["t1"] = _TeamRec("t1", leader_id="L")
    return orch, gw


# ---------------------------------------------------------------------------
# QuestionRegistry unit tests
# ---------------------------------------------------------------------------


def test_registry_register_returns_qid_and_future():
    async def body():
        reg = QuestionRegistry()
        qid, future = reg.register("asker", _free_text_q(), None, "leader")
        assert qid.startswith("q_")
        assert not future.done()
        pq = reg.get(qid)
        assert pq is not None
        assert pq.asker_agent_id == "asker"
        assert pq.chain == ["asker", "leader"]

    asyncio.run(body())


def test_registry_register_no_parent_uses_user_sentinel():
    async def body():
        reg = QuestionRegistry()
        qid, _ = reg.register("asker", _free_text_q(), None, None)
        pq = reg.get(qid)
        assert pq.chain == ["asker", "USER"]

    asyncio.run(body())


def test_registry_resolve_sets_future_and_returns_answer():
    async def body():
        reg = QuestionRegistry()
        qid, future = reg.register("asker", _free_text_q(), None, None)
        out = reg.resolve(qid, _answer("Vite"))
        assert out["ok"] is True
        assert "Vite" in out["answer_text"]
        assert future.done()
        result = future.result()
        assert "Vite" in result["answer_text"]

    asyncio.run(body())


def test_registry_resolve_unknown_qid():
    async def body():
        reg = QuestionRegistry()
        out = reg.resolve("q_nonexistent", _answer())
        assert out["ok"] is False
        assert out["reason"] == "unknown_qid"

    asyncio.run(body())


def test_registry_resolve_already_answered():
    async def body():
        reg = QuestionRegistry()
        qid, _ = reg.register("asker", _free_text_q(), None, None)
        reg.resolve(qid, _answer())
        out = reg.resolve(qid, _answer("second"))
        assert out["ok"] is False
        assert out["reason"] == "already_answered"

    asyncio.run(body())


def test_registry_pop_removes_entry():
    async def body():
        reg = QuestionRegistry()
        qid, _ = reg.register("asker", _free_text_q(), None, None)
        assert reg.get(qid) is not None
        reg.pop(qid)
        assert reg.get(qid) is None

    asyncio.run(body())


# ---------------------------------------------------------------------------
# has_pending_through
# ---------------------------------------------------------------------------


def test_has_pending_through_excludes_asker_and_current_target():
    async def body():
        reg = QuestionRegistry()
        qid, _ = reg.register("asker", _free_text_q(), None, "target")
        # chain = ["asker", "target"]; length 2 — no intermediate positions.
        assert not reg.has_pending_through("asker")
        assert not reg.has_pending_through("target")
        assert not reg.has_pending_through("other")

    asyncio.run(body())


def test_has_pending_through_true_for_intermediate_escalator():
    async def body():
        reg = QuestionRegistry()
        qid, _ = reg.register("asker", _free_text_q(), None, "L")
        reg.add_chain_hop(qid, "root")  # chain = ["asker", "L", "root"]
        # L is now intermediate (chain[1] of 3).
        assert reg.has_pending_through("L")
        # root is current target, asker is asker — neither is intermediate.
        assert not reg.has_pending_through("root")
        assert not reg.has_pending_through("asker")

    asyncio.run(body())


def test_has_pending_through_false_after_resolve():
    async def body():
        reg = QuestionRegistry()
        qid, _ = reg.register("asker", _free_text_q(), None, "L")
        reg.add_chain_hop(qid, "root")  # L is intermediate
        assert reg.has_pending_through("L")
        reg.resolve(qid, _answer())
        # Future is done; has_pending_through skips done futures.
        assert not reg.has_pending_through("L")

    asyncio.run(body())


# ---------------------------------------------------------------------------
# post_question — routes to parent inbox or gateway
# ---------------------------------------------------------------------------


def test_post_question_routes_to_parent_inbox():
    orch, gw = _make_world()

    async def body():
        qid, future = await orch.post_question(
            asker_id="M", parent_id="L",
            questions=_free_text_q(), context_hint=None,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Leader L's inbox should have a wake-up system message.
        assert orch._agents["L"].inbox.qsize() == 1
        msg = orch._agents["L"].inbox.get_nowait()
        assert msg.kind == "system"
        assert qid in msg.content
        assert "answer_question" in msg.content

        # Gateway was NOT called.
        assert gw.surfaced == []

        # Registry still has the question pending.
        pq = orch._questions.get(qid)
        assert pq is not None
        assert not future.done()

        # Cleanup.
        orch.resolve_question(qid, _answer())
        assert future.done()

    asyncio.run(body())


def test_post_question_with_no_parent_routes_to_gateway():
    orch, gw = _make_world()

    async def body():
        qid, future = await orch.post_question(
            asker_id="L", parent_id=None,
            questions=_free_text_q(), context_hint=None,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Gateway was called.
        assert len(gw.surfaced) == 1
        surfaced_qid, body_str, qs = gw.surfaced[0]
        assert surfaced_qid == qid

        # No agent inbox touched.
        assert orch._agents["L"].inbox.qsize() == 0
        assert orch._agents["M"].inbox.qsize() == 0

        # Cleanup.
        orch.resolve_question(qid, _answer())

    asyncio.run(body())


# ---------------------------------------------------------------------------
# forward_question — updates chain, dispatches, returns immediately
# ---------------------------------------------------------------------------


def test_forward_question_returns_immediately_and_appends_chain():
    orch, gw = _make_world()
    # Add a "root" agent above L.
    orch._agents["root"] = _AgentRec("root", team_id=None)

    async def body():
        qid, future = await orch.post_question(
            asker_id="M", parent_id="L",
            questions=_free_text_q(), context_hint=None,
        )
        # Drain L's inbox message.
        orch._agents["L"].inbox.get_nowait()

        # L escalates to root.
        out = await orch.forward_question(qid=qid, by_id="L", new_target_id="root", reason="ask root")
        assert out["ok"] is True
        assert out["new_holder"] == "root"

        # Chain extended.
        pq = orch._questions.get(qid)
        assert pq.chain[-1] == "root"

        # root's inbox received the escalation message.
        await asyncio.sleep(0)
        assert orch._agents["root"].inbox.qsize() == 1
        msg = orch._agents["root"].inbox.get_nowait()
        assert "(escalated by L" in msg.content

        # Future is still pending — bubble model.
        assert not future.done()

        # Cleanup.
        orch.resolve_question(qid, _answer())

    asyncio.run(body())


def test_forward_question_does_not_block_on_future():
    orch, gw = _make_world()

    async def body():
        qid, future = await orch.post_question(
            asker_id="M", parent_id="L",
            questions=_free_text_q(), context_hint=None,
        )
        orch._agents["L"].inbox.get_nowait()  # drain

        # forward_question returns before the future is resolved.
        done = False

        async def escalate_and_flag():
            nonlocal done
            await orch.forward_question(qid=qid, by_id="L", new_target_id=None, reason="to user")
            done = True

        t = asyncio.create_task(escalate_and_flag())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert done, "forward_question should have returned by now"
        assert not future.done(), "future must still be pending after escalation"

        orch.resolve_question(qid, _answer())
        await t

    asyncio.run(body())


# ---------------------------------------------------------------------------
# resolve_question
# ---------------------------------------------------------------------------


def test_resolve_sets_future_only_on_asker_side():
    orch, gw = _make_world()

    async def body():
        qid, future = await orch.post_question(
            asker_id="M", parent_id="L",
            questions=_free_text_q(), context_hint=None,
        )
        orch._agents["L"].inbox.get_nowait()  # drain

        # Only the asker's future is affected.
        out = orch.resolve_question(qid, _answer("Vite"))
        assert out["ok"] is True
        assert future.done()
        result = future.result()
        assert "Vite" in result["answer_text"]

        # Emitted question_answered.
        events = [e[0] for e in orch.emitter.events]
        assert "question_answered" in events

    asyncio.run(body())


# ---------------------------------------------------------------------------
# answer_question primitive
# ---------------------------------------------------------------------------


def test_answer_question_resolves_pending_for_holder():
    orch, gw = _make_world()

    async def body():
        # M asks; L is the holder.
        ask_task = asyncio.create_task(
            orch.gateway_ask_via_chain("M", _free_text_q(), None)
        )
        # Emit question_asked event (normally done by gateway_ask_via_chain).
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Find the qid.
        qid = next(iter(orch._questions._pending))

        # L answers via the primitive.
        out = await answer_question(
            orch, caller_id="L", qid=qid,
            answers=[{"selected_labels": [], "text": "Vite, from PM"}],
            reason="already answered in requirements.md",
        )
        assert out == {"ok": True, "qid": qid}

        result = await asyncio.wait_for(ask_task, timeout=1.0)
        assert "Vite" in result.get("answer_text", "")

    asyncio.run(body())


def test_answer_question_rejects_non_holder():
    orch, gw = _make_world()

    async def body():
        ask_task = asyncio.create_task(
            orch.gateway_ask_via_chain("M", _free_text_q(), None)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        qid = next(iter(orch._questions._pending))

        # M is the asker — not the holder (L is chain[-1]).
        with pytest.raises(PrimitiveError) as exc:
            await answer_question(
                orch, caller_id="M", qid=qid,
                answers=[{"selected_labels": [], "text": "x"}],
                reason="test reason",
            )
        assert exc.value.code == "not_holder"

        # Cleanup.
        orch.resolve_question(qid, _answer())
        await asyncio.wait_for(ask_task, timeout=1.0)

    asyncio.run(body())


# ---------------------------------------------------------------------------
# escalate_question primitive
# ---------------------------------------------------------------------------


def test_escalate_question_to_user_when_holder_has_no_leader():
    """M asks → L is holder (L leads team t1). L escalates → L has no parent team,
    so new_target_id=None → question surfaces to user gateway."""
    orch, gw = _make_world()
    # L has no team_id (i.e. L is the root-level agent). parent_for_chain("L") = None.
    orch._agents["L"].team_id = None

    async def body():
        ask_task = asyncio.create_task(
            orch.gateway_ask_via_chain("M", _free_text_q(), None)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        qid = next(iter(orch._questions._pending))

        # L's inbox has the message (M → L routing).
        assert orch._agents["L"].inbox.qsize() == 1
        orch._agents["L"].inbox.get_nowait()  # drain

        # L escalates. L has no team → parent_for_chain("L") = None → gateway.
        out = await escalate_question(orch, caller_id="L", qid=qid, reason="user owns this")
        assert out["ok"] is True
        assert out["new_holder"] is None

        # Gateway was called.
        await asyncio.sleep(0)
        assert len(gw.surfaced) == 1
        assert gw.surfaced[0][0] == qid

        # question_escalated event emitted.
        escalated = [e for e in orch.emitter.events if e[0] == "question_escalated"]
        assert len(escalated) == 1

        # Cleanup.
        orch.resolve_question(qid, _answer("user said vite"))
        result = await asyncio.wait_for(ask_task, timeout=1.0)
        assert "vite" in result.get("answer_text", "").lower()

    asyncio.run(body())


def test_escalate_question_rejects_non_holder():
    orch, gw = _make_world()

    async def body():
        ask_task = asyncio.create_task(
            orch.gateway_ask_via_chain("M", _free_text_q(), None)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        qid = next(iter(orch._questions._pending))

        # M is the asker — not the holder.
        with pytest.raises(PrimitiveError) as exc:
            await escalate_question(orch, caller_id="M", qid=qid, reason="x")
        assert exc.value.code == "not_holder"

        orch.resolve_question(qid, _answer())
        await asyncio.wait_for(ask_task, timeout=1.0)

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Watchdog Pass B skip
# ---------------------------------------------------------------------------


def test_watchdog_pass_b_skips_throughed_agent():
    """Agents that have forwarded a pending question are suppressed by Pass B."""

    async def body():
        reg = QuestionRegistry()
        # M asked → L forwarded → root is current target.
        qid, _ = reg.register("M", _free_text_q(), None, "L")
        reg.add_chain_hop(qid, "root")  # chain = ["M", "L", "root"]

        # L is intermediate — has_pending_through returns True.
        assert reg.has_pending_through("L")
        # M is the asker (chain[0]) — not intermediate.
        assert not reg.has_pending_through("M")
        # root is current target (chain[-1]) — not intermediate.
        assert not reg.has_pending_through("root")

    asyncio.run(body())


def test_watchdog_resumes_after_resolve():
    """After the question is resolved, has_pending_through returns False."""

    async def body():
        reg = QuestionRegistry()
        qid, _ = reg.register("M", _free_text_q(), None, "L")
        reg.add_chain_hop(qid, "root")

        assert reg.has_pending_through("L")
        reg.resolve(qid, _answer())
        assert not reg.has_pending_through("L")

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Terminate asker pops registry (invariant check)
# ---------------------------------------------------------------------------


def test_terminate_asker_pops_registry_and_does_not_strand_anything():
    """pop() removes the entry; future is either resolved or stays orphaned."""

    async def body():
        reg = QuestionRegistry()
        qid, future = reg.register("M", _free_text_q(), None, "L")
        assert reg.get(qid) is not None

        # Simulate asker dying: pop the registry entry.
        reg.pop(qid)
        assert reg.get(qid) is None

        # has_pending_through returns False (entry is gone).
        assert not reg.has_pending_through("M")
        assert not reg.has_pending_through("L")

        # The orphaned future is still pending (no awaiter).
        assert not future.done()

    asyncio.run(body())


# ---------------------------------------------------------------------------
# gateway_ask_via_chain integration (uses orch directly)
# ---------------------------------------------------------------------------


def test_gateway_ask_via_chain_blocks_until_resolved():
    """gateway_ask_via_chain blocks on the future and returns when resolved."""
    orch, gw = _make_world()

    async def body():
        result_holder: list[dict] = []

        async def ask():
            r = await orch.gateway_ask_via_chain("M", _free_text_q(), None)
            result_holder.append(r)

        ask_task = asyncio.create_task(ask())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        qid = next(iter(orch._questions._pending))
        # Resolve from L's side (L is holder).
        orch.resolve_question(qid, _answer("webpack"))
        await asyncio.wait_for(ask_task, timeout=1.0)

        assert result_holder
        assert "webpack" in result_holder[0].get("answer_text", "")

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Watchdog Pass B integration — real Orchestrator with in-flight question
# ---------------------------------------------------------------------------


def test_watchdog_pass_b_suppresses_nudge_for_intermediate_hop(tmp_path):
    """Pass B must not emit liveness.nudge for an agent that forwarded a question.

    This is the root-cause fix for tsk_80cac529 (escalator was re-nudged by
    the watchdog to call ask_user again). Exercises the full _watchdog_tick
    code path, not just has_pending_through in isolation.
    """
    from pathlib import Path
    from beidou.orchestrator import (
        USER_SENTINEL, AgentRecord, Orchestrator, TeamRecord,
        IDLE_NUDGE_S,
    )
    from beidou.events import EventEmitter

    class _SyncEmitter:
        """Captures emit_event calls without going to disk."""
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, event: str, agent_id: str = "", team_id=None, **kwargs) -> None:
            self.events.append((event, {"agent_id": agent_id, **kwargs}))

    async def body():
        emitter = _SyncEmitter()
        skill_root = tmp_path / "skills"
        skill_root.mkdir(exist_ok=True)
        o = Orchestrator(
            task_id="tsk_watchdog_test",
            emitter=emitter,  # type: ignore[arg-type]
            skill_root=skill_root,
        )

        # Seed a 3-level graph:
        #   root_ag (teamless, root)
        #   pm_ag (in team tm_top, led by root_ag)
        #   eng_ag (in team tm_eng, led by pm_ag)

        top_team = TeamRecord(
            team_id="tm_top", name="top", task="",
            leader_id="root_ag", depth=1, member_ids=[], rules=[],
        )
        eng_team = TeamRecord(
            team_id="tm_eng", name="eng", task="",
            leader_id="pm_ag", depth=2, member_ids=[], rules=[],
        )
        o._teams["tm_top"] = top_team
        o._teams["tm_eng"] = eng_team

        def _add(agent_id, team_id):
            rec = AgentRecord(
                agent_id=agent_id, task_id="tsk_watchdog_test", team_id=team_id,
                role=agent_id, skill_name="fake", model=None,
                inbox=asyncio.Queue(), create_team_lock=asyncio.Lock(),
            )
            o._agents[agent_id] = rec
            if team_id and team_id in o._teams:
                o._teams[team_id].member_ids.append(agent_id)
            return rec

        root_rec = _add("root_ag", None)
        o._root_id = "root_ag"
        pm_rec = _add("pm_ag", "tm_top")
        eng_rec = _add("eng_ag", "tm_eng")

        # eng asked → pm is intermediate holder → root is current target.
        qid, future = o._questions.register("eng_ag", _free_text_q(), None, "pm_ag")
        o._questions.add_chain_hop(qid, "root_ag")
        # chain = ["eng_ag", "pm_ag", "root_ag"]

        # Make pm_ag look idle enough to be nudged under normal circumstances.
        pm_rec.last_progress_ts = time.time() - IDLE_NUDGE_S - 10.0
        pm_rec.idle_nudge_count = 0
        pm_rec.inflight_tools = 0
        pm_rec.completion_pending = False

        # Also make root_ag look idle so we can verify it IS nudged (it's the
        # current target, not an intermediate hop).
        root_rec.last_progress_ts = time.time() - IDLE_NUDGE_S - 10.0
        root_rec.idle_nudge_count = 0
        root_rec.inflight_tools = 0
        root_rec.completion_pending = False

        # Run one watchdog tick.
        await o._watchdog_tick()

        nudge_events = [e for e in emitter.events if e[0] == "liveness.nudge"]
        nudged_agents = {e[1].get("agent_id") for e in nudge_events}

        # pm_ag MUST NOT be nudged (it forwarded a pending question).
        assert "pm_ag" not in nudged_agents, (
            f"pm_ag was nudged despite being an intermediate hop; events: {nudge_events}"
        )

        # root_ag (current target = chain[-1]) is not intermediate → MAY be nudged.
        # We don't assert it IS nudged (Pass B also skips agents with no children unless
        # root), but we do assert pm_ag is not.

        # Cleanup: resolve the question.
        o._questions.resolve(qid, _answer())
        future.result()  # confirm it resolved cleanly

    asyncio.run(body())
