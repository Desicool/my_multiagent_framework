"""Team — leader + parallel member agents, recursive sub-team support."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from beidou import workspace as ws
from beidou.context import AgentContext
from beidou.events import EventEmitter

if TYPE_CHECKING:
    pass


def _load_template(template_name: str) -> dict:
    """Load a YAML template by name from the beidou/templates/ directory."""
    import sys
    import yaml

    try:
        # sys._MEIPASS is set by PyInstaller when running as a frozen bundle
        base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
        pkg_dir = base / "beidou" / "templates" if getattr(sys, "frozen", False) else base / "templates"
        path = pkg_dir / f"{template_name}.yaml"
        if not path.exists():
            path = pkg_dir / "default.yaml"
        with path.open() as f:
            data = yaml.safe_load(f)
        # Inject SkillTool instances for any skills: listed in the template
        skill_names = data.pop("skills", []) or []
        if skill_names:
            from beidou.skills.loader import load_skills, skills_as_tools
            all_skills = load_skills()
            skill_tools = skills_as_tools(skill_names, all_skills)
            data["_skill_tools"] = skill_tools
        return data
    except Exception:
        return {"name": template_name, "tools": ["bash", "file_read", "file_write"], "system_prompt": "You are a helpful agent."}


def _build_tools(tool_names: list[str]):
    """Instantiate tools by name."""
    from beidou.tools.bash import ShellTool
    from beidou.tools.files import ReadFileTool, WriteFileTool
    from beidou.tools.question_tools import AskUserTool, CheckQuestionsTool, AnswerQuestionTool, EscalateQuestionTool, AwaitTeamTool
    from beidou.tools.team_tools import CreateTeamTool, ReadMessagesTool, SendMessageTool
    from beidou.tools.web import WebFetchTool, WebSearchTool

    registry = {
        "bash": ShellTool,
        "file_read": ReadFileTool,
        "file_write": WriteFileTool,
        "web_search": WebSearchTool,
        "web_fetch": WebFetchTool,
        "create_team": CreateTeamTool,
        "send_message": SendMessageTool,
        "read_messages": ReadMessagesTool,
        "ask_user": AskUserTool,
        "check_questions": CheckQuestionsTool,
        "answer_question": AnswerQuestionTool,
        "escalate_question": EscalateQuestionTool,
        "await_team": AwaitTeamTool,
    }
    return [registry[n]() for n in tool_names if n in registry]


@dataclass
class Team:
    team_name: str
    task: str
    roles: list[dict]  # [{"role": str, "description": str, "template": str}]
    parent_ctx: AgentContext
    rules: str | None = None
    team_id: str = field(default_factory=ws.new_team_id)

    async def run(self) -> str:
        task_id = self.parent_ctx.task_id
        base_dir: Path = self.parent_ctx.get("cwd") or Path.cwd()
        workspace_path = ws.team_workspace(base_dir, self.team_id)

        # Reuse the parent's emitter (stored as a context value)
        emitter: EventEmitter = self.parent_ctx.get("emitter")

        # Emit team_created event
        if emitter:
            await emitter.emit(
                "team_created",
                agent_id=self.parent_ctx.agent_id,
                team_id=self.parent_ctx.team_id,
                new_team_id=self.team_id,
                team_name=self.team_name,
                workspace_path=str(workspace_path),
                leader_agent_id=self.parent_ctx.agent_id,
            )

        # Write workspace files
        await self._write_workspace(base_dir, workspace_path)

        # Spawn all member agents in parallel
        async with asyncio.TaskGroup() as tg:
            member_tasks: list[asyncio.Task] = []
            for role_def in self.roles:
                t = tg.create_task(self._run_member(role_def, workspace_path, emitter))
                member_tasks.append(t)

        results = [t.result() for t in member_tasks]
        summary = "\n\n".join(f"**{self.roles[i]['role']}**:\n{r}" for i, r in enumerate(results))
        return summary

    async def _write_workspace(self, base_dir: Path, workspace_path: Path) -> None:
        rules_content = self.rules or self._default_rules()
        roster_lines = [f"- **{r['role']}**: {r['description']}" for r in self.roles]
        roster_content = f"# Roster — {self.team_name}\n\n" + "\n".join(roster_lines)
        plan_content = f"# Plan — {self.team_name}\n\n## Objective\n\n{self.task}\n"

        ws.write_workspace_file(base_dir, self.team_id, "RULES.md", rules_content)
        ws.write_workspace_file(base_dir, self.team_id, "ROSTER.md", roster_content)
        ws.write_workspace_file(base_dir, self.team_id, "PLAN.md", plan_content)

    def _default_rules(self) -> str:
        return (
            f"# Rules — {self.team_name}\n\n"
            "1. Complete your assigned sub-task and write results to artifacts/.\n"
            "2. Use send_message to coordinate with teammates when blocked.\n"
            "3. If your sub-task is itself complex, create a sub-team.\n"
            "4. Prefer parallel work over sequential.\n"
            "5. Be concise and concrete in all outputs.\n"
        )

    async def _run_member(
        self,
        role_def: dict,
        workspace_path: Path,
        emitter: EventEmitter | None,
    ) -> str:
        from beidou.agent import Agent
        from beidou.layers.observability_layer import ObservabilityLayer
        from beidou.layers.tools_layer import ToolsLayer
        from beidou.layers.workspace_layer import WorkspaceLayer

        template_name = role_def.get("template", "default")
        tmpl = _load_template(template_name)
        # role_def model > template default_model > parent context model
        model = role_def.get("model") or tmpl.get("model") or self.parent_ctx.get("model") or "claude-sonnet-4-6"
        role = role_def["role"]

        tools = _build_tools(tmpl.get("tools", [])) + list(tmpl.get("_skill_tools") or [])

        layers = [ToolsLayer(tools), WorkspaceLayer(workspace_path)]
        if emitter:
            layers.append(ObservabilityLayer(emitter, model=model, role=role))

        member_ctx = self.parent_ctx.child(*layers, team_id=self.team_id)

        system_prompt = tmpl.get("system_prompt", "").format(
            role=role,
            role_description=role_def.get("description", ""),
            team_name=self.team_name,
            workspace_path=str(workspace_path),
        )

        agent = Agent(model=model, role=role, ctx=member_ctx, system_prompt=system_prompt)
        member_task = f"{role_def['description']}\n\nTeam: {self.team_name}\nWorkspace: {workspace_path}\n\nFull objective: {self.task}"
        return await agent.run(member_task)
