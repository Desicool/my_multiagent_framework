"""Team coordination tools: CreateTeamTool, SendMessageTool, ReadMessagesTool."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from beidou.tools.base import BaseTool

if TYPE_CHECKING:
    from beidou.context import AgentContext


class CreateTeamTool(BaseTool):
    """Spawn a team to tackle a sub-task in parallel."""

    @property
    def name(self) -> str:
        return "create_team"

    def schema(self) -> dict:
        return {
            "name": "create_team",
            "description": (
                "Create a team of agents to work on a sub-task. You become the team leader. "
                "Use this whenever the task has 2+ distinct concerns or requires parallel work. "
                "Returns the team result when all members complete."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Short name for the team (e.g. 'backend', 'research')."},
                    "task": {"type": "string", "description": "Full description of the team's objective."},
                    "roles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "description": {"type": "string"},
                                "template": {
                                    "type": "string",
                                    "description": "Tool template: 'default', 'coder', 'researcher'. Default: 'default'.",
                                },
                            },
                            "required": ["role", "description"],
                        },
                        "description": "List of member roles to spawn.",
                    },
                    "rules": {
                        "type": "string",
                        "description": "Optional markdown rules/guidelines for the team. Auto-generated if omitted.",
                    },
                },
                "required": ["team_name", "task", "roles"],
            },
        }

    async def execute(
        self,
        ctx: "AgentContext",
        team_name: str,
        task: str,
        roles: list[dict],
        rules: str | None = None,
        **_: Any,
    ) -> dict:
        import asyncio as _asyncio

        # Import here to avoid circular imports at module load time
        from beidou.team import Team

        team = Team(
            team_name=team_name,
            task=task,
            roles=roles,
            rules=rules,
            parent_ctx=ctx,
        )

        # Non-blocking: spawn team.run() as a background task and return immediately.
        # The calling agent continues its loop and can answer inbox questions from
        # members while the team runs. It collects the result later via await_team.
        task_obj = _asyncio.create_task(team.run(), name=f"team:{team.team_id}")
        running = ctx._kv.setdefault("running_teams", {})
        running[team.team_id] = task_obj

        return {
            "team_id": team.team_id,
            "team_name": team_name,
            "status": "running",
            "hint": "Use await_team(team_id) when ready to collect the result.",
        }


class SendMessageTool(BaseTool):
    """Send a message to another agent's inbox in the team workspace."""

    @property
    def name(self) -> str:
        return "send_message"

    def schema(self) -> dict:
        return {
            "name": "send_message",
            "description": "Send a message to another agent's workspace inbox.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "to_agent_id": {"type": "string", "description": "Target agent ID."},
                    "message": {"type": "string", "description": "Message content (markdown)."},
                },
                "required": ["to_agent_id", "message"],
            },
        }

    async def execute(self, ctx: "AgentContext", to_agent_id: str, message: str, **_: Any) -> dict:
        from pathlib import Path

        from beidou import workspace as ws

        if ctx.team_id is None:
            return {"error": "Agent is not in a team; cannot send messages."}

        base_dir: Path = ctx.get("cwd") or Path.cwd()
        inbox = ws.inbox_path(base_dir, ctx.team_id, to_agent_id)
        entry = f"\n---\n**From:** {ctx.agent_id}  **At:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n{message}\n"
        with inbox.open("a") as f:
            f.write(entry)

        return {"delivered": True, "to": to_agent_id, "bytes": len(entry)}


class ReadMessagesTool(BaseTool):
    """Read all messages from the agent's workspace inbox."""

    @property
    def name(self) -> str:
        return "read_messages"

    def schema(self) -> dict:
        return {
            "name": "read_messages",
            "description": "Read all pending messages from your workspace inbox.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        }

    async def execute(self, ctx: "AgentContext", **_: Any) -> dict:
        from pathlib import Path

        from beidou import workspace as ws

        if ctx.team_id is None:
            return {"messages": [], "note": "Agent is not in a team."}

        base_dir: Path = ctx.get("cwd") or Path.cwd()
        inbox = ws.inbox_path(base_dir, ctx.team_id, ctx.agent_id)
        if not inbox.exists():
            return {"messages": []}
        content = inbox.read_text()
        return {"messages": content or "(empty)"}
