"""Beidou agent layer — SDK-aware runtime logic.

This package depends on ``claude_agent_sdk`` and handles the agent spawn loop,
SDK hook dispatch, system prompt assembly, and typed hook contexts.

It imports from ``beidou.engine`` for data structures but adds the SDK-aware
behaviour layer on top.

Modules:
    loop:     ``run_agent()`` drain loop, SpawnSpec, RunResult
    hooks:    ``build_builtin_hooks()``, HOOK_REVIEW_TIMEOUT_S, allowlist
    prompts:  ``build_system_prompt()``, template substitution
    context:  Typed per-hook-point context dataclasses
"""

from beidou.agent.context import (
    AgentStartContext,
    Block,
    EventContext,
    InputContext,
    OutputContext,
    Pass,
    ToolCallContext,
    ToolResultContext,
    TurnEvalContext,
)
from beidou.agent.hooks import (
    ALLOWED_DURING_PENDING_REVIEW,
    HOOK_REVIEW_TIMEOUT_S,
    build_builtin_hooks,
    build_hooks,
)
from beidou.agent.loop import RunResult, SpawnSpec, run_agent
from beidou.agent.prompts import build_system_prompt, render_system_prompt

__all__ = [
    "ALLOWED_DURING_PENDING_REVIEW",
    "AgentStartContext",
    "Block",
    "EventContext",
    "HOOK_REVIEW_TIMEOUT_S",
    "InputContext",
    "OutputContext",
    "Pass",
    "RunResult",
    "SpawnSpec",
    "ToolCallContext",
    "ToolResultContext",
    "TurnEvalContext",
    "build_builtin_hooks",
    "build_hooks",
    "build_system_prompt",
    "render_system_prompt",
    "run_agent",
]
