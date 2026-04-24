"""ResilienceLayer — retry for LLM errors, structured feedback for tool errors.

LLM errors (Anthropic SDK exceptions): retry transient/overloaded with jittered
exponential backoff, fallback_model on 529, re-raise on fatal/exhausted.

Tool errors (exceptions from tool.execute): NOT retried (side effects unsafe).
Caught and surfaced as {"__tool_error__": True, "message": str}. The caller
(agent.py) translates this to a tool_result with is_error:true so the LLM sees
the failure and can self-correct.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from beidou.context import BaseLayer

if TYPE_CHECKING:
    from beidou.context import AgentContext


class ErrorClass(Enum):
    TRANSIENT = "transient"
    OVERLOADED = "overloaded"
    FATAL = "fatal"


def classify_llm(exc: Exception) -> ErrorClass:
    """Classify an Anthropic SDK exception for retry routing."""
    try:
        import anthropic  # local import so this module is importable without the SDK
    except Exception:
        return ErrorClass.FATAL

    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return ErrorClass.TRANSIENT
    if isinstance(exc, anthropic.RateLimitError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, anthropic.InternalServerError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, anthropic.APIStatusError) and getattr(exc, "status_code", None) == 529:
        return ErrorClass.OVERLOADED
    return ErrorClass.FATAL


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_ms: int = 500
    max_ms: int = 30_000
    factor: float = 2.0


class ResilienceLayer(BaseLayer):
    """Handles LLM errors (retry) and tool errors (structured is_error feedback)."""

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        fallback_model: str | None = None,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self.fallback_model = fallback_model

    # ---- LLM error path -------------------------------------------------

    async def on_llm_call(
        self,
        ctx: "AgentContext",
        req: dict,
        next: Callable[[dict], Coroutine[Any, Any, Any]],
    ) -> Any:
        emitter = ctx.get("emitter")
        for attempt in range(self.policy.max_attempts):
            try:
                return await next(req)
            except Exception as exc:
                cls = classify_llm(exc)
                if emitter is not None:
                    try:
                        await emitter.emit(
                            "llm_retry",
                            agent_id=ctx.agent_id,
                            attempt=attempt,
                            error_class=cls.value,
                            error=str(exc)[:300],
                        )
                    except Exception:
                        pass

                if cls == ErrorClass.FATAL or attempt == self.policy.max_attempts - 1:
                    if emitter is not None:
                        try:
                            await emitter.emit(
                                "llm_exhausted",
                                agent_id=ctx.agent_id,
                                error_class=cls.value,
                                error=str(exc)[:300],
                            )
                        except Exception:
                            pass
                    raise

                # Swap to fallback model on sustained overload
                if cls == ErrorClass.OVERLOADED and self.fallback_model:
                    req = {**req, "model": self.fallback_model}

                delay_s = min(self.policy.base_ms * (self.policy.factor ** attempt),
                              self.policy.max_ms) / 1000.0
                delay_s *= 0.5 + random.random()  # jitter 0.5x..1.5x
                await asyncio.sleep(delay_s)
        # Unreachable — loop either returns or raises
        raise RuntimeError("resilience layer fell through retry loop")

    # ---- tool error path ------------------------------------------------

    async def on_tool_call(
        self,
        ctx: "AgentContext",
        tool: str,
        args: dict,
        next: Callable[[str, dict], Coroutine[Any, Any, Any]],
    ) -> Any:
        emitter = ctx.get("emitter")
        try:
            return await next(tool, args)
        except TypeError as exc:
            schema_str = "{}"
            try:
                tools_by_name = {t.name: t for t in ctx.tools()}
                hinted = tools_by_name.get(tool)
                if hinted is not None:
                    schema_str = json.dumps(hinted.schema().get("input_schema", {}))
            except Exception:
                pass
            msg = f"Schema error calling {tool}: {exc}. Expected args: {schema_str}"
            if emitter is not None:
                try:
                    await emitter.emit("tool_error", agent_id=ctx.agent_id, tool=tool,
                                       error_class="schema", error=str(exc)[:300])
                except Exception:
                    pass
            return {"__tool_error__": True, "message": msg}
        except Exception as exc:
            msg = f"{tool} failed: {type(exc).__name__}: {exc}"
            if emitter is not None:
                try:
                    await emitter.emit("tool_error", agent_id=ctx.agent_id, tool=tool,
                                       error_class="execution", error=str(exc)[:300])
                except Exception:
                    pass
            return {"__tool_error__": True, "message": msg}
