"""WorkspaceLayer — binds a workspace path into the context."""
from __future__ import annotations

from pathlib import Path

from beidou.context import BaseLayer


class WorkspaceLayer(BaseLayer):
    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path

    def provides_tools(self):
        return []
