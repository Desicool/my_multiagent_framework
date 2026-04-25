"""InboxLayer — provides question tools + nudges the LLM when inbox is non-empty."""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from beidou.context import BaseLayer
from beidou.tools.question_tools import (
    AnswerQuestionTool,
    AskUserTool,
    AwaitTeamTool,
    CheckQuestionsTool,
    EscalateQuestionTool,
)

if TYPE_CHECKING:
    from beidou.context import AgentContext
    from beidou.tools.base import BaseTool


class InboxLayer(BaseLayer):
    """Provides ask_user / check_questions / answer_question / escalate_question / await_team.

    On every LLM call, if the current agent holds pending questions, injects a
    synthetic user message reminding it to answer or escalate. The injection is
    non-mutating — we build a new req dict so the agent's persistent messages
    list (agent.py:21) isn't affected.
    """

    def __init__(self) -> None:
        self._tools: list["BaseTool"] = [
            AskUserTool(),
            CheckQuestionsTool(),
            AnswerQuestionTool(),
            EscalateQuestionTool(),
            AwaitTeamTool(),
        ]

    def provides_tools(self) -> list["BaseTool"]:
        return self._tools

    async def on_llm_call(
        self,
        ctx: "AgentContext",
        req: dict,
        next: Callable[[dict], Coroutine[Any, Any, Any]],
    ) -> Any:
        broker = ctx.get("question_broker")
        if broker is None:
            return await next(req)

        pending = broker.inbox_for(ctx.agent_id)
        if not pending:
            return await next(req)

        # Build a nudge describing each pending question so the LLM can decide.
        lines = [f"[INBOX] You hold {len(pending)} pending question(s). "
                 "You MUST call answer_question(qid, answer) or escalate_question(qid, reason) "
                 "for each one this turn before other work."]
        for q in pending:
            prompt_preview = q.prompt if len(q.prompt) <= 500 else q.prompt[:500] + "…"
            ctx_line = f" [context: {q.context_hint}]" if q.context_hint else ""
            lines.append(f"  - qid={q.qid} from={q.asker_agent_id}{ctx_line}\n    prompt: {prompt_preview}")

        nudge = {"role": "user", "content": "\n".join(lines)}

        # Non-mutating copy — DO NOT mutate req["messages"] in place; the agent
        # loop holds a reference to the same list and would see duplicates.
        new_req = {**req, "messages": list(req["messages"]) + [nudge]}
        return await next(new_req)
