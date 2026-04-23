"""Workspace directory helpers for team workspaces."""
from __future__ import annotations

import uuid
from pathlib import Path

BEIDOU_DIR = Path.home() / ".beidou"


def new_team_id() -> str:
    return f"tm_{uuid.uuid4().hex[:8]}"


def team_workspace(base_dir: Path, team_id: str) -> Path:
    """Return (and create) {base_dir}/.beidou/{team_id}/ for non-root teams."""
    path = base_dir / ".beidou" / team_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifacts_dir(base_dir: Path, team_id: str) -> Path:
    d = team_workspace(base_dir, team_id) / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def inbox_path(base_dir: Path, team_id: str, agent_id: str) -> Path:
    d = team_workspace(base_dir, team_id) / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{agent_id}.md"


def write_workspace_file(base_dir: Path, team_id: str, filename: str, content: str) -> Path:
    path = team_workspace(base_dir, team_id) / filename
    path.write_text(content)
    return path
