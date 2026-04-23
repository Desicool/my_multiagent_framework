"""ToolsLayer — supplies a fixed set of tools to the context."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from beidou.context import BaseLayer

if TYPE_CHECKING:
    from beidou.tools.base import BaseTool


class ToolsLayer(BaseLayer):
    def __init__(self, tools: list["BaseTool"]) -> None:
        self._tools = tools

    def provides_tools(self) -> list["BaseTool"]:
        return self._tools
