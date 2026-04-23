"""SkillTool — wraps a Skill as a BaseTool that spawns a beidou Agent."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from beidou.skills.base import Skill
from beidou.tools.base import BaseTool

if TYPE_CHECKING:
    from beidou.context import AgentContext


class SkillTool(BaseTool):
    """When the LLM calls invoke_{skill_name}(task=...), this spins up a child
    beidou Agent configured by the skill's SKILL.md and returns its result.

    All execution is local (in-process asyncio coroutine). No Anthropic Managed
    Agents API calls are made here.
    """

    def __init__(self, skill: Skill, all_skills: dict[str, Skill]) -> None:
        self._skill = skill
        self._all_skills = all_skills

    @property
    def name(self) -> str:
        return f"invoke_{self._skill.name}"

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self._skill.description,
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task description for this skill.",
                    },
                },
                "required": ["task"],
            },
        }

    async def execute(self, ctx: "AgentContext", task: str, **_: Any) -> dict:
        from beidou.agent import Agent
        from beidou.layers.observability_layer import ObservabilityLayer
        from beidou.layers.tools_layer import ToolsLayer
        from beidou.layers.workspace_layer import WorkspaceLayer
        from beidou.skills.loader import skills_as_tools
        from beidou.team import _build_tools

        model = self._skill.model or ctx.get("model") or "claude-sonnet-4-6"
        emitter = ctx.get("emitter")

        # Standard beidou tools from allowed_tools list
        standard_tools = _build_tools(self._skill.allowed_tools)

        # Sub-skill tools injected from the skills: field
        sub_tools = skills_as_tools(self._skill.sub_skills, self._all_skills)

        all_tools = standard_tools + sub_tools

        # Workspace: reuse parent workspace path from WorkspaceLayer if available
        workspace_path: Path = ctx.get("cwd") or Path.cwd()

        layers: list[Any] = [ToolsLayer(all_tools), WorkspaceLayer(workspace_path)]
        if emitter:
            layers.append(ObservabilityLayer(emitter, model=model, role=self._skill.name))

        child_ctx = ctx.child(*layers)
        # Propagate shared values
        child_ctx._kv["model"] = model
        child_ctx._kv["emitter"] = emitter
        child_ctx._kv["cwd"] = workspace_path
        if ctx.get("base_url"):
            child_ctx._kv["base_url"] = ctx.get("base_url")

        agent = Agent(
            model=model,
            role=self._skill.name,
            ctx=child_ctx,
            system_prompt=self._skill.system_prompt,
        )

        result = await agent.run(task)
        return {"skill": self._skill.name, "result": result}
