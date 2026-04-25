"""AgentContext — Go-style linked parent chain with interceptor layers.

External code extends an agent's capabilities by calling ctx.child(MyLayer()).
The chain is walked for tool aggregation and value lookup; interceptors fire
in order from outermost (root) to innermost (current) layer.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import anthropic

if TYPE_CHECKING:
    from beidou.tools.base import BaseTool


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@runtime_checkable
class ContextLayer(Protocol):
    """Open interface — implement any subset of lifecycle hooks.

    Each hook receives `ctx` (the current context) and `next` (the
    continuation — call it to proceed down the chain).  If you don't call
    `next`, the chain is short-circuited.
    """

    async def on_agent_start(self, ctx: "AgentContext") -> None: ...

    async def on_agent_stop(self, ctx: "AgentContext", result: str) -> None: ...

    async def on_llm_call(
        self,
        ctx: "AgentContext",
        req: dict,
        next: Callable[[dict], Coroutine[Any, Any, Any]],
    ) -> Any: ...

    async def on_tool_call(
        self,
        ctx: "AgentContext",
        tool: str,
        args: dict,
        next: Callable[[str, dict], Coroutine[Any, Any, Any]],
    ) -> Any: ...

    def provides_tools(self) -> list["BaseTool"]: ...


class BaseLayer:
    """Mixin with pass-through defaults so subclasses only override what they need."""

    async def on_agent_start(self, ctx: "AgentContext") -> None:
        pass

    async def on_agent_stop(self, ctx: "AgentContext", result: str) -> None:
        pass

    async def on_llm_call(
        self,
        ctx: "AgentContext",
        req: dict,
        next: Callable[[dict], Coroutine[Any, Any, Any]],
    ) -> Any:
        return await next(req)

    async def on_tool_call(
        self,
        ctx: "AgentContext",
        tool: str,
        args: dict,
        next: Callable[[str, dict], Coroutine[Any, Any, Any]],
    ) -> Any:
        return await next(tool, args)

    def provides_tools(self) -> list["BaseTool"]:
        return []


@dataclass
class AgentContext:
    """Go-style context: linked parent chain + per-level interceptor layers.

    Usage::

        root_ctx = AgentContext.root(task_id="tsk_abc")
        root_ctx._kv["model"] = "claude-opus-4-7"   # inject via _kv; retrieved with ctx.get("model")
        child_ctx = root_ctx.child(WorkspaceLayer(path), ObservabilityLayer(emitter))
        grandchild_ctx = child_ctx.child(ToolsLayer([bash_tool]))
    """

    agent_id: str
    task_id: str
    team_id: str | None
    parent: AgentContext | None
    layers: list[Any]  # list[ContextLayer] — Protocol not usable as annotation at runtime
    _kv: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    @classmethod
    def root(
        cls,
        task_id: str,
        team_id: str | None = None,
        layers: list[Any] | None = None,
    ) -> "AgentContext":
        return cls(
            agent_id=_new_id("agt"),
            task_id=task_id,
            team_id=team_id,
            parent=None,
            layers=layers or [],
        )

    def child(self, *layers: Any, team_id: str | None = None) -> "AgentContext":
        """Derive a child context, typically for a sub-agent or new team member."""
        return AgentContext(
            agent_id=_new_id("agt"),
            task_id=self.task_id,
            team_id=team_id if team_id is not None else self.team_id,
            parent=self,
            layers=list(layers),
        )

    def with_value(self, key: str, value: Any) -> "AgentContext":
        """Return a child context carrying an extra key-value (like context.WithValue)."""
        ctx = self.child()
        ctx._kv[key] = value
        return ctx

    # ------------------------------------------------------------------ #
    # Value lookup (walks parent chain)                                    #
    # ------------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._kv:
            return self._kv[key]
        if self.parent is not None:
            return self.parent.get(key, default)
        return default

    # ------------------------------------------------------------------ #
    # Tool aggregation (across full chain)                                 #
    # ------------------------------------------------------------------ #

    def tools(self) -> list["BaseTool"]:
        """Collect tools provided by every layer in the full ancestor chain.

        Innermost (current) layers win on name collision.
        """
        from beidou.tools.base import BaseTool  # local import to break cycle

        seen: dict[str, BaseTool] = {}
        # Collect from root to self so inner layers override outer ones
        for ctx in reversed(list(self._chain())):
            for layer in ctx.layers:
                if hasattr(layer, "provides_tools"):
                    for t in layer.provides_tools():
                        seen[t.name] = t
        return list(seen.values())

    def _chain(self) -> list["AgentContext"]:
        """Return [root, ..., self]."""
        nodes: list[AgentContext] = []
        cur: AgentContext | None = self
        while cur is not None:
            nodes.append(cur)
            cur = cur.parent
        nodes.reverse()
        return nodes

    # ------------------------------------------------------------------ #
    # Interceptor invocation                                               #
    # ------------------------------------------------------------------ #

    async def invoke_lifecycle(self, hook: str, **kwargs: Any) -> None:
        """Fire on_agent_start / on_agent_stop across all layers."""
        for ctx_node in self._chain():
            for layer in ctx_node.layers:
                fn = getattr(layer, hook, None)
                if fn is not None:
                    await fn(self, **kwargs)

    async def invoke_llm(self, req: dict) -> Any:
        """Run an Anthropic messages.create call through the full interceptor chain."""
        chain = [
            layer
            for ctx_node in self._chain()
            for layer in ctx_node.layers
            if hasattr(layer, "on_llm_call")
        ]

        async def execute(req: dict) -> Any:
            import os

            base_url = self.get("base_url") or os.environ.get("ANTHROPIC_BASE_URL")
            client = anthropic.AsyncAnthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY"),
                **({"base_url": base_url} if base_url else {}),
            )
            return await client.messages.create(**req)

        async def build_next(idx: int) -> Callable[[dict], Coroutine[Any, Any, Any]]:
            if idx >= len(chain):
                return execute

            layer = chain[idx]
            inner = await build_next(idx + 1)

            async def _next(r: dict) -> Any:
                return await layer.on_llm_call(self, r, inner)

            return _next

        fn = await build_next(0)
        return await fn(req)

    async def invoke_tool(self, name: str, args: dict) -> Any:
        """Execute a named tool through the full interceptor chain."""
        tools_by_name = {t.name: t for t in self.tools()}

        async def execute(tool_name: str, tool_args: dict) -> Any:
            t = tools_by_name.get(tool_name)
            if t is None:
                raise ValueError(f"Unknown tool: {tool_name!r}")
            return await t.execute(self, **tool_args)

        chain = [
            layer
            for ctx_node in self._chain()
            for layer in ctx_node.layers
            if hasattr(layer, "on_tool_call")
        ]

        async def build_next(idx: int) -> Callable[[str, dict], Coroutine[Any, Any, Any]]:
            if idx >= len(chain):
                return execute

            layer = chain[idx]
            inner = await build_next(idx + 1)

            async def _next(tn: str, ta: dict) -> Any:
                return await layer.on_tool_call(self, tn, ta, inner)

            return _next

        fn = await build_next(0)
        return await fn(name, args)
