"""QuestionBroker — routes agent questions up the team-leader chain to the user.

Lives as a singleton on root ctx._kv['question_broker']. All agents in the task
share it because the _kv chain resolves to the same object.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from beidou.context import AgentContext


def _new_qid() -> str:
    return f"q_{uuid.uuid4().hex[:8]}"


@dataclass
class Question:
    qid: str
    asker_agent_id: str
    current_holder_agent_id: str | None   # None = surfaced to user
    chain: list[str]                       # audit trail, includes "USER" terminal
    prompt: str
    context_hint: str | None
    state: str                             # "pending" | "answered" | "at_user" | "timed_out"
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)


class QuestionBroker:
    def __init__(self) -> None:
        self._pending: dict[str, Question] = {}
        self._agent_inbox: dict[str, list[str]] = {}
        self._terminal_lock = asyncio.Lock()

    # ---- asker side -------------------------------------------------

    async def ask(self, ctx: "AgentContext", prompt: str, context_hint: str | None = None) -> str:
        """Called from AskUserTool. Returns the human/leader answer or raises TimeoutError."""
        holder = ctx.parent.agent_id if ctx.parent is not None else None
        loop = asyncio.get_running_loop()
        q = Question(
            qid=_new_qid(),
            asker_agent_id=ctx.agent_id,
            current_holder_agent_id=holder,
            chain=[ctx.agent_id] + ([holder] if holder else ["USER"]),
            prompt=prompt,
            context_hint=context_hint,
            state="at_user" if holder is None else "pending",
            future=loop.create_future(),
        )
        self._pending[q.qid] = q
        if holder is None:
            asyncio.create_task(self._surface_to_terminal(q, ctx))
        else:
            self._agent_inbox.setdefault(holder, []).append(q.qid)

        await self._emit(ctx, "question_asked", qid=q.qid, asker=ctx.agent_id,
                         holder=holder, prompt=prompt[:200])

        timeout = ctx.get("max_question_wait", 300)
        try:
            answer = await asyncio.wait_for(q.future, timeout=timeout)
            await self._emit(ctx, "question_answered", qid=q.qid, asker=ctx.agent_id,
                             chain_len=len(q.chain))
            return answer
        except asyncio.TimeoutError:
            q.state = "timed_out"
            await self._emit(ctx, "question_timeout", qid=q.qid, asker=ctx.agent_id)
            raise
        finally:
            self._pending.pop(q.qid, None)
            # Best-effort cleanup of inbox entries
            for lst in self._agent_inbox.values():
                if q.qid in lst:
                    lst.remove(q.qid)

    # ---- holder side -------------------------------------------------

    def inbox_for(self, agent_id: str) -> list[Question]:
        qids = list(self._agent_inbox.get(agent_id, []))
        return [self._pending[qid] for qid in qids if qid in self._pending
                and self._pending[qid].state == "pending"]

    def answer(self, qid: str, text: str) -> dict:
        q = self._pending.get(qid)
        if q is None:
            return {"ok": False, "reason": "unknown_qid"}
        if q.state != "pending":
            return {"ok": False, "reason": f"already_{q.state}"}
        if q.future.done():
            return {"ok": False, "reason": "future_done"}
        q.state = "answered"
        self._drop_from_inbox(q)
        q.future.set_result(text)
        return {"ok": True}

    async def escalate(self, qid: str, by_ctx: "AgentContext", reason: str) -> dict:
        q = self._pending.get(qid)
        if q is None or q.state != "pending":
            return {"ok": False, "reason": "stale"}
        new_holder = by_ctx.parent.agent_id if by_ctx.parent is not None else None
        self._drop_from_inbox(q)
        q.current_holder_agent_id = new_holder
        q.chain.append(new_holder or "USER")
        if new_holder is None:
            q.state = "at_user"
            asyncio.create_task(self._surface_to_terminal(q, by_ctx))
        else:
            self._agent_inbox.setdefault(new_holder, []).append(qid)
        await self._emit(by_ctx, "question_escalated", qid=qid, by=by_ctx.agent_id,
                         new_holder=new_holder, reason=reason[:200])
        return {"ok": True}

    # ---- internals ---------------------------------------------------

    def _drop_from_inbox(self, q: Question) -> None:
        for lst in self._agent_inbox.values():
            if q.qid in lst:
                lst.remove(q.qid)

    async def _surface_to_terminal(self, q: Question, ctx: "AgentContext") -> None:
        # Serialize terminal I/O so concurrent escalations don't interleave on stdin.
        async with self._terminal_lock:
            if q.future.done():
                return
            console = ctx.get("console")
            loop = asyncio.get_running_loop()
            chain_str = " → ".join(q.chain)
            prompt_txt = (
                f"\n[bold yellow]Question from {q.asker_agent_id}[/]  "
                f"[dim](chain: {chain_str})[/dim]\n"
                f"{q.prompt}\n"
            )
            if q.context_hint:
                prompt_txt += f"[dim]context: {q.context_hint}[/dim]\n"
            prompt_txt += "[bold green]Answer:[/] "
            try:
                if console is not None:
                    answer = await loop.run_in_executor(None, lambda: console.input(prompt_txt))
                else:
                    answer = await loop.run_in_executor(None, lambda: input(prompt_txt))
            except Exception as exc:
                if not q.future.done():
                    q.future.set_exception(exc)
                return
            if not q.future.done():
                q.future.set_result(answer)

    async def _emit(self, ctx: "AgentContext", event: str, **kwargs: Any) -> None:
        emitter = ctx.get("emitter")
        if emitter is not None:
            try:
                await emitter.emit(event, agent_id=ctx.agent_id, **kwargs)
            except Exception:
                pass
