"""QuestionBroker — routes agent questions up the team-leader chain to the user.

Lives as a singleton on root ctx._kv['question_broker']. All agents in the task
share it because the _kv chain resolves to the same object.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from beidou import db

if TYPE_CHECKING:
    from beidou.gateways.base import BaseGateway


def _new_qid() -> str:
    return f"q_{uuid.uuid4().hex[:8]}"


@dataclass
class Question:
    qid: str
    asker_agent_id: str
    current_holder_agent_id: str | None   # None = surfaced to user
    chain: list[str]                       # audit trail, includes "USER" terminal
    questions: list[dict]                  # raw Claude Code wire shape: [{question, header, multiSelect, options:[{label, description}, ...]}]
    context_hint: str | None
    state: str                             # "pending" | "answered" | "at_user"
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)
    # Private fields — not part of the public documented shape; used by resolve_answer.
    _emitter: Any = field(default=None, repr=False)
    _emitter_agent_id: str = field(default="", repr=False)

    @property
    def prompt(self) -> str:
        """Derived plain-text fallback for terminal/TUI/legacy displays.

        Renders the questions list as a single readable string. One sub-question
        per line. Format:
            Q{i} ({header}): {question}
              - {label} — {description}
              ...
        When options is empty, shows only the question line.
        When header is empty, omits the parenthesised header.
        """
        lines: list[str] = []
        for i, sq in enumerate(self.questions, start=1):
            header: str = sq.get("header") or ""
            question_text: str = sq.get("question", "")
            if header:
                lines.append(f"Q{i} ({header}): {question_text}")
            else:
                lines.append(f"Q{i}: {question_text}")
            for opt in sq.get("options", []):
                label = opt.get("label", "")
                description = opt.get("description", "")
                if description:
                    lines.append(f"  - {label} — {description}")
                else:
                    lines.append(f"  - {label}")
        return "\n".join(lines)


class QuestionBroker:
    def __init__(self) -> None:
        self._pending: dict[str, Question] = {}
        self._agent_inbox: dict[str, list[str]] = {}
        self._terminal_lock = asyncio.Lock()
        self._gateway: BaseGateway | None = None

    def set_gateway(self, gateway: "BaseGateway") -> None:
        self._gateway = gateway

    # ---- asker side -------------------------------------------------

    async def ask(self, ctx: Any, questions: list[dict], context_hint: str | None = None) -> dict:
        """Called from AskUserTool. Blocks until the human/leader supplies an answer.

        Returns {"answers": [...], "answer_text": "..."} from resolve_answer.
        """
        holder = ctx.parent.agent_id if ctx.parent is not None else None
        loop = asyncio.get_running_loop()
        q = Question(
            qid=_new_qid(),
            asker_agent_id=ctx.agent_id,
            current_holder_agent_id=holder,
            chain=[ctx.agent_id] + ([holder] if holder else ["USER"]),
            questions=questions,
            context_hint=context_hint,
            state="at_user" if holder is None else "pending",
            future=loop.create_future(),
            _emitter=ctx.get("emitter"),
            _emitter_agent_id=ctx.agent_id,
        )
        self._pending[q.qid] = q
        prompt = q.prompt  # derived property value; stable for this question's lifetime
        # Non-blocking audit log — do not let DB errors block agents
        try:
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: db.insert_question(
                    qid=q.qid,
                    task_id=ctx.get("task_id", ""),
                    asker_agent_id=q.asker_agent_id,
                    prompt=prompt,
                    context_hint=q.context_hint,
                    chain_json=json.dumps(q.chain),
                    created_at=q.created_at,
                )
            )
        except Exception:
            pass
        if holder is None:
            if self._gateway is not None:
                asyncio.create_task(self._gateway.surface_question(q, self))
            else:
                asyncio.create_task(self._surface_to_terminal(q, ctx))
        else:
            self._agent_inbox.setdefault(holder, []).append(q.qid)

        await self._emit(ctx, "question_asked", qid=q.qid, asker=ctx.agent_id,
                         holder=holder, prompt=prompt[:200], questions=q.questions)

        try:
            result = await q.future
            # resolver emits question_answered now
            return result
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

    def resolve_answer(self, qid: str, answers: list[dict]) -> dict:
        """Normalize and resolve a structured answer. Called by web/terminal/TUI/fallback
        gateways instead of resolving the future directly.

        answers: one entry per sub-question, in order, each
            {selected_labels: list[str], text: str | None}.
        Returns {ok: bool, reason?: str}.
        """
        q = self._pending.get(qid)
        if q is None:
            return {"ok": False, "reason": "unknown_qid"}
        if q.future.done():
            return {"ok": False, "reason": "already_answered"}
        if len(answers) != len(q.questions):
            return {"ok": False, "reason": "answer_count_mismatch"}

        # Render answer_text per sub-question then join them.
        rendered_parts: list[str] = []
        for i, (sq, ans) in enumerate(zip(q.questions, answers), start=1):
            selected_labels: list[str] = ans.get("selected_labels") or []
            text: str | None = ans.get("text")
            options: list[dict] = sq.get("options") or []
            multi_select: bool = bool(sq.get("multiSelect", False))

            if not options:
                # Free-text-only question
                part = text or ""
            elif multi_select:
                # Multi-select: comma-join selected labels; append Other text if present
                joined = ", ".join(selected_labels)
                if text:
                    part = f"{joined}, {text}" if joined else text
                else:
                    part = joined
            else:
                # Single-select
                if selected_labels:
                    part = selected_labels[0]
                else:
                    # Zero selected → "Other" path — use typed text
                    part = text or ""

            header: str = sq.get("header") or ""
            if header:
                rendered_parts.append(f"{header}: {part}")
            else:
                rendered_parts.append(part)

        answer_text = "\n".join(rendered_parts)

        q.state = "answered"
        self._drop_from_inbox(q)
        q.future.set_result({"answers": answers, "answer_text": answer_text})

        # Best-effort DB persistence (non-blocking)
        try:
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: db.update_question_answered(
                    qid=qid, answer=json.dumps(answers), answered_at=time.time()
                )
            )
        except Exception:
            pass

        # Emit question_answered from the resolver (canonical emit point).
        emitter = q._emitter
        agent_id = q._emitter_agent_id
        if emitter is not None:
            try:
                asyncio.create_task(
                    emitter.emit(
                        "question_answered",
                        agent_id=agent_id,
                        qid=qid,
                        asker=q.asker_agent_id,
                        chain_len=len(q.chain),
                        answers=answers,
                        answer_text=answer_text,
                    )
                )
            except Exception:
                pass

        return {"ok": True}

    def answer(self, qid: str, text: str) -> dict:
        """Legacy string-only answer path used by leaders responding from their inbox.

        Delegates to resolve_answer with a single free-text entry so the structured
        resolution path handles DB writes and event emission.
        Note: this works correctly only when q.questions has exactly one entry.
        For multi-subquestion questions escalated to a leader, the leader must use
        resolve_answer directly with one entry per sub-question.
        """
        return self.resolve_answer(qid, [{"selected_labels": [], "text": text}])

    async def escalate(self, qid: str, by_ctx: Any, reason: str) -> dict:
        q = self._pending.get(qid)
        if q is None or q.state != "pending":
            return {"ok": False, "reason": "stale"}
        new_holder = by_ctx.parent.agent_id if by_ctx.parent is not None else None
        self._drop_from_inbox(q)
        q.current_holder_agent_id = new_holder
        q.chain.append(new_holder or "USER")
        if new_holder is None:
            q.state = "at_user"
            if self._gateway is not None:
                asyncio.create_task(self._gateway.surface_question(q, self))
            else:
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

    async def _surface_to_terminal(self, q: Question, ctx: Any) -> None:
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
                # Route through the shared resolver so DB writes and question_answered
                # event are consistent with the web/TUI paths. Terminal presents the
                # flat derived prompt and collects one text reply; wrap it as one
                # free-text entry per sub-question so resolve_answer's count check
                # passes for the single-question (most common) case.
                answers = [{"selected_labels": [], "text": answer}
                           for _ in q.questions]
                self.resolve_answer(q.qid, answers)

    async def _emit(self, ctx: Any, event: str, **kwargs: Any) -> None:
        emitter = ctx.get("emitter")
        if emitter is not None:
            try:
                await emitter.emit(event, agent_id=ctx.agent_id, **kwargs)
            except Exception:
                pass
