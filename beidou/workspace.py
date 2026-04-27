"""Workspace directory helpers for team workspaces."""
from __future__ import annotations

import uuid
from pathlib import Path


def new_team_id() -> str:
    return f"tm_{uuid.uuid4().hex[:8]}"


def team_workspace(project_workspace: Path, task_id: str, team_id: str) -> Path:
    """Return (and create) {project_workspace}/.beidou/tasks/{task_id}/teams/{team_id}/."""
    path = project_workspace / ".beidou" / "tasks" / task_id / "teams" / team_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def agent_workspace(project_workspace: Path, task_id: str, agent_id: str) -> Path:
    """Return (and create) {project_workspace}/.beidou/tasks/{task_id}/agents/{agent_id}/.

    Used for teamless agents (e.g. the root agent) that are not members of
    any team and therefore have no team workspace.
    """
    path = project_workspace / ".beidou" / "tasks" / task_id / "agents" / agent_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifacts_dir(project_workspace: Path, task_id: str, team_id: str) -> Path:
    d = team_workspace(project_workspace, task_id, team_id) / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def inbox_path(project_workspace: Path, task_id: str, team_id: str, agent_id: str) -> Path:
    d = team_workspace(project_workspace, task_id, team_id) / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{agent_id}.md"


def write_workspace_file(project_workspace: Path, task_id: str, team_id: str, filename: str, content: str) -> Path:
    path = team_workspace(project_workspace, task_id, team_id) / filename
    path.write_text(content)
    return path
