"""ObservabilityLayer — intercepts all hooks and emits events.

This is *just* a ContextLayer. No special-casing in agent.py.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from beidou.context import BaseLayer

if TYPE_CHECKING:
    from beidou.context import AgentContext
    from beidou.events import EventEmitter


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Rough cost in USD using published token prices (as of 2025)."""
    prices = {
        "claude-opus-4-7":    (0.015,  0.075),
        "claude-sonnet-4-6":  (0.003,  0.015),
        "claude-haiku-4-5":   (0.0008, 0.004),
    }
    for prefix, (in_p, out_p) in prices.items():
        if model.startswith(prefix):
            return (tokens_in * in_p + tokens_out * out_p) / 1000
    # fallback: use sonnet pricing
    return (tokens_in * 0.003 + tokens_out * 0.015) / 1000


class ObservabilityLayer(BaseLayer):
    def __init__(
        self,
        emitter: "EventEmitter",
        model: str = "",
        role: str = "member",
        template: str | None = None,
        tools: list[str] | None = None,
        skills: list[str] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._emitter = emitter
        self._model = model
        self._role = role
        self._template = template
        self._tools = list(tools) if tools else []
        self._skills = list(skills) if skills else []
        self._system_prompt = system_prompt

    def provides_tools(self):
        return []

    async def on_agent_start(self, ctx: "AgentContext") -> None:
        await self._emitter.emit(
            "agent_started",
            agent_id=ctx.agent_id,
            team_id=ctx.team_id,
            model=self._model,
            role=self._role,
            template=self._template,
            tools=self._tools,
            skills=self._skills,
            system_prompt=self._system_prompt,
        )

    async def on_agent_stop(self, ctx: "AgentContext", result: str = "") -> None:
        await self._emitter.emit(
            "agent_completed",
            agent_id=ctx.agent_id,
            team_id=ctx.team_id,
            result_length=len(result),
        )

    async def on_llm_call(
        self,
        ctx: "AgentContext",
        req: dict,
        next: Callable[[dict], Coroutine[Any, Any, Any]],
    ) -> Any:
        t0 = time.perf_counter()
        resp = await next(req)
        duration_ms = (time.perf_counter() - t0) * 1000

        tokens_in = getattr(getattr(resp, "usage", None), "input_tokens", 0) or 0
        tokens_out = getattr(getattr(resp, "usage", None), "output_tokens", 0) or 0
        cost = _estimate_cost(self._model, tokens_in, tokens_out)

        await self._emitter.emit(
            "llm_response",
            agent_id=ctx.agent_id,
            team_id=ctx.team_id,
            duration_ms=duration_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            stop_reason=getattr(resp, "stop_reason", None),
        )
        return resp

    async def on_tool_call(
        self,
        ctx: "AgentContext",
        tool: str,
        args: dict,
        next: Callable[[str, dict], Coroutine[Any, Any, Any]],
    ) -> Any:
        t0 = time.perf_counter()
        result = await next(tool, args)
        duration_ms = (time.perf_counter() - t0) * 1000

        await self._emitter.emit(
            "tool_called",
            agent_id=ctx.agent_id,
            team_id=ctx.team_id,
            tool=tool,
            duration_ms=duration_ms,
        )
        return result
