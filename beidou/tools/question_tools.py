"""Question/escalation tools: ask_user, check_questions, answer_question, escalate_question, await_team."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from beidou.tools.base import BaseTool

if TYPE_CHECKING:
    from beidou.context import AgentContext


class AskUserTool(BaseTool):
    """Ask a clarifying question. Blocks until the leader chain or user answers."""

    @property
    def name(self) -> str:
        return "ask_user"

    def schema(self) -> dict:
        return {
            "name": "ask_user",
            "description": (
                "Ask a clarifying question when requirements are ambiguous. The question "
                "goes to your team leader first; the leader may answer locally or escalate "
                "up the chain to the human user. BLOCKS until answered. Prefer this over "
                "guessing whenever the answer would change the shape of the deliverable."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask. Be specific — include options when possible.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context explaining why you need this — helps the leader decide whether to answer or escalate.",
                    },
                },
                "required": ["question"],
            },
        }

    async def execute(self, ctx: "AgentContext", question: str, context: str | None = None, **_: Any) -> dict:
        broker = ctx.get("question_broker")
        if broker is None:
            return {"__tool_error__": True, "message": "question_broker not configured on this task"}
        try:
            answer = await broker.ask(ctx, question, context)
            return {"answer": answer}
        except asyncio.TimeoutError:
            return {"__tool_error__": True, "message": "ask_user timed out; no answer received within max_question_wait"}


class CheckQuestionsTool(BaseTool):
    """List pending questions held in this agent's inbox."""

    @property
    def name(self) -> str:
        return "check_questions"

    def schema(self) -> dict:
        return {
            "name": "check_questions",
            "description": (
                "List any pending questions assigned to you from teammates. Each turn, "
                "if this list is non-empty, you MUST either answer_question or escalate_question "
                "for each qid before doing other work."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        }

    async def execute(self, ctx: "AgentContext", **_: Any) -> dict:
        broker = ctx.get("question_broker")
        if broker is None:
            return {"questions": []}
        items = broker.inbox_for(ctx.agent_id)
        return {
            "questions": [
                {
                    "qid": q.qid,
                    "from": q.asker_agent_id,
                    "prompt": q.prompt,
                    "context": q.context_hint,
                    "chain": q.chain,
                }
                for q in items
            ]
        }


class AnswerQuestionTool(BaseTool):
    """Answer a pending question from your inbox, unblocking the asker."""

    @property
    def name(self) -> str:
        return "answer_question"

    def schema(self) -> dict:
        return {
            "name": "answer_question",
            "description": (
                "Resolve a question from your inbox by answering it directly. The asker's "
                "ask_user tool call will return with your answer. Use this when you can "
                "decide locally without involving the human user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "qid": {"type": "string", "description": "Question id from check_questions."},
                    "answer": {"type": "string", "description": "Your answer — will be delivered verbatim to the asker."},
                },
                "required": ["qid", "answer"],
            },
        }

    async def execute(self, ctx: "AgentContext", qid: str, answer: str, **_: Any) -> dict:
        broker = ctx.get("question_broker")
        if broker is None:
            return {"__tool_error__": True, "message": "question_broker not configured"}
        return broker.answer(qid, answer)


class EscalateQuestionTool(BaseTool):
    """Escalate a pending question to YOUR team leader."""

    @property
    def name(self) -> str:
        return "escalate_question"

    def schema(self) -> dict:
        return {
            "name": "escalate_question",
            "description": (
                "Forward a question from your inbox to your own team leader (or to the human "
                "user if you are the root orchestrator). Use when the answer genuinely requires "
                "a higher-level decision — product/requirement decisions, external secrets, taste calls."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "qid": {"type": "string", "description": "Question id from check_questions."},
                    "reason": {"type": "string", "description": "Why you're escalating rather than answering."},
                },
                "required": ["qid", "reason"],
            },
        }

    async def execute(self, ctx: "AgentContext", qid: str, reason: str, **_: Any) -> dict:
        broker = ctx.get("question_broker")
        if broker is None:
            return {"__tool_error__": True, "message": "question_broker not configured"}
        return await broker.escalate(qid, ctx, reason)


class AwaitTeamTool(BaseTool):
    """Wait for a non-blocking team (spawned via create_team) to finish."""

    @property
    def name(self) -> str:
        return "await_team"

    def schema(self) -> dict:
        return {
            "name": "await_team",
            "description": (
                "Wait for a team previously created with create_team. Returns {status: 'done', result} "
                "when the team finishes, {status: 'still_running'} if it's not done within timeout_seconds "
                "(keep working and call again later), or {status: 'failed', error} if it raised. "
                "Use this to collect team results without blocking your ability to answer inbox questions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "The team_id returned by create_team."},
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Max seconds to wait this call. Default 30. Use shorter values to stay responsive to inbox.",
                    },
                },
                "required": ["team_id"],
            },
        }

    async def execute(
        self,
        ctx: "AgentContext",
        team_id: str,
        timeout_seconds: float | int | None = 30,
        **_: Any,
    ) -> dict:
        # Walk up the _kv chain to find the running_teams registry. Each agent
        # has its own running_teams (agents that called create_team), so we
        # search from self upward.
        running: dict[str, asyncio.Task] | None = None
        node = ctx
        while node is not None:
            rt = node._kv.get("running_teams")
            if rt and team_id in rt:
                running = rt
                break
            node = node.parent
        if running is None:
            return {"__tool_error__": True, "message": f"no running team with team_id={team_id}"}

        task = running[team_id]
        timeout = float(timeout_seconds) if timeout_seconds is not None else 30.0
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            # Task completed — remove from registry.
            running.pop(team_id, None)
            return {"status": "done", "result": result}
        except asyncio.TimeoutError:
            return {"status": "still_running", "team_id": team_id}
        except Exception as exc:
            running.pop(team_id, None)
            return {"status": "failed", "team_id": team_id, "error": f"{type(exc).__name__}: {exc}"}
