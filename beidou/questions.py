"""QuestionRegistry — thin qid → PendingQuestion map owned by the orchestrator.

Replaces beidou/inbox.py (QuestionBroker). All I/O side-effects (DB writes,
event emission) are deliberately absent here; they live on the orchestrator
which owns the emitter and the executor. This module is a pure data structure.

See docs/agent-runtime.md §2.5 and docs/tool-surface.md for the question-chain
contract and the agent-facing tool shapes.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


def _new_qid() -> str:
    return f"q_{uuid.uuid4().hex[:8]}"


def render_prompt_text(questions: list[dict]) -> str:
    """Render a questions list as a plain-text string for DB/terminal display.

    One sub-question per line. Format:
        Q{i} ({header}): {question}
          - {label} — {description}
          ...
    When options is empty, shows only the question line.
    When header is empty, omits the parenthesised header.

    Ported verbatim from inbox.py Question.prompt @property.
    """
    lines: list[str] = []
    for i, sq in enumerate(questions, start=1):
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


@dataclass
class PendingQuestion:
    qid: str
    asker_agent_id: str
    questions: list[dict]           # raw Claude Code wire shape
    context_hint: Optional[str]
    chain: list[str]                # ["asker", "holder1", ..., final_target_or_"USER"]
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)


class QuestionRegistry:
    """Per-task qid → PendingQuestion table. Owned by the orchestrator.

    Designed as a pure data structure: no event emission, no DB writes.
    The orchestrator (which holds the emitter and the executor) owns all
    I/O side-effects triggered at register/resolve time.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingQuestion] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register(
        self,
        asker_id: str,
        questions: list[dict],
        context_hint: Optional[str],
        initial_target_id: Optional[str],
    ) -> tuple[str, asyncio.Future]:
        """Create a new PendingQuestion and return (qid, future).

        ``initial_target_id`` is the first holder (the asker's team leader),
        or None when the question routes straight to the user gateway.
        The chain is initialised as [asker_id, initial_target_id or "USER"].
        """
        qid = _new_qid()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        pq = PendingQuestion(
            qid=qid,
            asker_agent_id=asker_id,
            questions=questions,
            context_hint=context_hint,
            chain=[asker_id, initial_target_id if initial_target_id is not None else "USER"],
            future=future,
        )
        self._pending[qid] = pq
        return qid, future

    def add_chain_hop(self, qid: str, agent_id_or_user_sentinel: str) -> None:
        """Append one hop to the question's chain on escalation."""
        pq = self._pending.get(qid)
        if pq is not None:
            pq.chain.append(agent_id_or_user_sentinel)

    def get(self, qid: str) -> Optional[PendingQuestion]:
        return self._pending.get(qid)

    def pop(self, qid: str) -> None:
        """Remove a question from the registry (called by the asker's finally)."""
        self._pending.pop(qid, None)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, qid: str, answers: list[dict]) -> dict:
        """Validate, normalize, and resolve the question future.

        Validation and answer-normalization logic ported verbatim from
        QuestionBroker.resolve_answer (inbox.py:153-254).

        Returns:
            {"ok": True, "answer_text": str, "answers": list[dict]}  on success.
            {"ok": False, "reason": str}                               on failure.

        Side-effects: sets future.set_result; does NOT emit events or write DB
        — those are the orchestrator's responsibility.
        """
        pq = self._pending.get(qid)
        if pq is None:
            return {"ok": False, "reason": "unknown_qid"}
        if pq.future.done():
            return {"ok": False, "reason": "already_answered"}
        if len(answers) != len(pq.questions):
            return {"ok": False, "reason": "answer_count_mismatch"}

        # Render answer_text per sub-question then join them.
        rendered_parts: list[str] = []
        for sq, ans in zip(pq.questions, answers):
            selected_labels: list[str] = ans.get("selected_labels") or []
            text: Optional[str] = ans.get("text")
            options: list[dict] = sq.get("options") or []
            multi_select: bool = bool(sq.get("multiSelect", False))

            # Derive selected_values from the matching option's `value` (or label
            # fallback) so callers can match on a stable machine discriminator.
            label_to_value: dict[str, str] = {}
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                lbl = opt.get("label")
                if lbl is None:
                    continue
                label_to_value[lbl] = opt.get("value") or lbl
            ans["selected_values"] = [
                label_to_value.get(lbl, lbl) for lbl in selected_labels
            ]

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
        pq.future.set_result({"answers": answers, "answer_text": answer_text})
        return {"ok": True, "answer_text": answer_text, "answers": answers}

    # ------------------------------------------------------------------
    # Watchdog helper
    # ------------------------------------------------------------------

    def has_pending_through(self, agent_id: str) -> bool:
        """Return True iff agent_id is an intermediate hop in a still-pending question.

        "Intermediate hop" means the agent appears in some pending question's
        chain at a position that is neither the asker (chain[0]) nor the
        current target (chain[-1]). An escalator that has forwarded and returned
        falls into this category and should be suppressed by the watchdog's
        Pass B nudge — they cannot do anything to advance the question, so
        nudging them to "call ask_user if blocked" would trigger the duplicate-
        ask bug from tsk_80cac529.

        The current target (chain[-1]) is intentionally excluded so a stalled
        holder who has not yet decided to answer or escalate is still visible
        to the watchdog.
        """
        for pq in self._pending.values():
            if pq.future.done():
                continue
            chain = pq.chain
            if len(chain) < 3:
                # Only asker and one target — no intermediate positions possible.
                continue
            # Intermediate positions: chain[1:-1]
            if agent_id in chain[1:-1]:
                return True
        return False
