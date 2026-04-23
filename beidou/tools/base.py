"""BaseTool ABC — every tool must implement name, schema(), and execute()."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from beidou.context import AgentContext


class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def schema(self) -> dict:
        """Return an Anthropic-compatible tool schema dict."""
        ...

    @abstractmethod
    async def execute(self, ctx: "AgentContext", **kwargs: Any) -> Any:
        """Execute the tool and return a JSON-serialisable result."""
        ...
