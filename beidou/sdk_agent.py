"""Thin wrapper around ``claude_agent_sdk.query(...)``.

This module spawns a single SDK agent, drains its async message iterator,
and translates the stream into Beidou observability events. It is called by
the orchestrator (built in the next step). The drain loop is Beidou's sole
producer of the events catalogued in ``docs/observability.md``:

* ``agent_started`` -- pre-drain lifecycle event.
* ``turn.usage``    -- deduplicated per-turn token accounting.
* ``tool_called``   -- emitted when a ``ToolUseBlock`` arrives in an ``AssistantMessage``.
* ``tool_result``   -- emitted when a ``ToolResultBlock`` arrives in a ``UserMessage``.
* ``run.cost``      -- terminal authoritative cost/duration/num_turns rollup.
* ``agent_completed`` -- post-drain lifecycle event.
* ``agent_error``   -- emitted on unexpected exceptions, before re-raising.

The drain loop is the **sole owner** of ``tool_called`` and ``tool_result``
emission. The MCP wrapper (``beidou/primitives/mcp.py``) does NOT emit
``tool_called`` -- that was the prior design, now replaced.

``contract_violation`` is emitted by the *orchestrator*, not this module. We
only set the flag on :class:`RunResult`; the orchestrator reads it and
decides whether to resume-not-terminate (see ``docs/agent-runtime.md``
section 4).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions, query

from .primitives.core import Orchestrator
from .primitives.mcp import build_mcp_server_for
from .skills.loader import (
    LoadedSkill,
    SkillError,
    load_skill,
    load_skill_file,
    render_system_prompt,
)


# ---------------------------------------------------------------------------
# Public API dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class SpawnSpec:
    """Everything Beidou needs to spawn one SDK agent.

    Fields mirror ``docs/architecture.md`` / ``docs/skills.md``. ``caller_id``
    must match the agent_id the orchestrator registered for this spawn --
    the MCP server binds it by closure and every primitive call uses it.
    """

    caller_id: str
    skill_name: str
    skill_root: Path
    task: str
    model: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    template_vars: Optional[dict[str, str]] = None
    cwd: Optional[str] = None


@dataclass
class RunResult:
    """Return value of :func:`run_agent`.

    ``terminated`` is True if and only if the orchestrator observed this
    agent consume a terminate sentinel before the SDK iterator ended.
    ``contract_violation`` is simply ``not terminated`` when the loop ended
    normally (with or without a ``ResultMessage``). On exception or
    cancellation the loop exits abnormally; we leave ``contract_violation``
    False to distinguish that case -- the orchestrator uses the exception
    path, not the contract-violation path, to react.
    """

    final_text: str
    total_cost_usd: float
    total_usage: dict
    num_turns: int
    duration_ms: int
    stop_reason: str
    session_id: Optional[str]
    terminated: bool
    contract_violation: bool = False
    model_usage: dict = field(default_factory=dict)
    duration_api_ms: int = 0


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _resolve_skill(skill_root: Path, skill_name: str) -> LoadedSkill:
    """Locate the skill by name under ``skill_root``; fall back to direct path."""
    try:
        return load_skill(skill_root, skill_name)
    except SkillError:
        # Fallback: allow callers who already know the exact file path to
        # pass a directory containing a single SKILL.md.
        candidate = Path(skill_root) / "SKILL.md"
        if candidate.is_file():
            return load_skill_file(candidate)
        raise


def _build_options(
    *,
    system_prompt: str,
    mcp_server,
    allowed_tools: list[str],
    model: Optional[str],
    cwd: Optional[str],
) -> ClaudeAgentOptions:
    """Assemble ``ClaudeAgentOptions`` for one SDK agent spawn.

    ``setting_sources=[]`` is deliberate: Beidou's custom SKILL.md loader is
    authoritative; we do not want the SDK's filesystem-discovery skills
    feature racing with it. ``permission_mode='bypassPermissions'`` matches
    ``docs/skills.md`` since every tool is either an MCP primitive we own
    or an explicitly-listed built-in.
    """
    kwargs: dict = dict(
        system_prompt=system_prompt,
        mcp_servers={"beidou": mcp_server},
        allowed_tools=list(allowed_tools),
        permission_mode="bypassPermissions",
        setting_sources=[],
    )
    if model:
        kwargs["model"] = model
    if cwd:
        kwargs["cwd"] = cwd
    return ClaudeAgentOptions(**kwargs)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


async def run_agent(orch: Orchestrator, spec: SpawnSpec) -> RunResult:
    """Spawn an SDK agent, drain its messages, emit observability events.

    The caller (the orchestrator) is responsible for the
    resume-not-terminate policy on contract violations. This function only
    reports whether the agent terminated cleanly via the
    ``RunResult.terminated`` / ``RunResult.contract_violation`` flags.
    """
    skill = _resolve_skill(spec.skill_root, spec.skill_name)
    template_vars = spec.template_vars or {}
    rendered_prompt = render_system_prompt(skill, **template_vars)

    mcp = build_mcp_server_for(orch, caller_id=spec.caller_id)

    allowed_tools = spec.allowed_tools if spec.allowed_tools is not None else list(skill.allowed_tools)
    if not allowed_tools:
        # Not fatal -- an agent with no tools will hit the contract on its
        # first turn -- but the condition almost always signals a mis-built
        # spec. Surface it through observability.
        orch.emit_event(
            "config_warning",
            {
                "caller_id": spec.caller_id,
                "warning": "empty_allowed_tools",
                "skill": skill.name,
                "ts": time.time(),
            },
        )

    options = _build_options(
        system_prompt=rendered_prompt,
        mcp_server=mcp,
        allowed_tools=allowed_tools,
        model=spec.model,
        cwd=spec.cwd,
    )

    orch.emit_event(
        "agent_started",
        {
            "caller_id": spec.caller_id,
            "skill": skill.name,
            "model_requested": spec.model,
            "ts": time.time(),
        },
    )

    # Per-drain accounting state.
    seen_message_ids: set[str] = set()
    final_text_parts: list[str] = []
    last_message_id: Optional[str] = None
    result_data: Optional[dict] = None
    # Tool-span tracking: maps tool_use_id -> monotonic start time.
    # Populated on ToolUseBlock arrival; consumed on ToolResultBlock arrival.
    pending_tool_uses: dict[str, float] = {}

    try:
        async for msg in query(prompt=spec.task, options=options):
            cls = type(msg).__name__

            if cls == "AssistantMessage":
                mid = getattr(msg, "message_id", None)
                usage = getattr(msg, "usage", None)
                model_str = getattr(msg, "model", None)
                stop_reason = getattr(msg, "stop_reason", None)

                # Dedup turn.usage by message_id. Multiple AssistantMessage
                # fragments share the same message_id + usage payload
                # (confirmed in proto_02_token_granularity.py).
                if mid and mid not in seen_message_ids and usage:
                    seen_message_ids.add(mid)
                    usage_payload = dict(usage) if isinstance(usage, dict) else {}
                    orch.emit_event(
                        "turn.usage",
                        {
                            "caller_id": spec.caller_id,
                            "message_id": mid,
                            "model": model_str,
                            "stop_reason": stop_reason,
                            "input_tokens": usage_payload.get("input_tokens"),
                            "output_tokens": usage_payload.get("output_tokens"),
                            "cache_creation_input_tokens": usage_payload.get(
                                "cache_creation_input_tokens"
                            ),
                            "cache_read_input_tokens": usage_payload.get(
                                "cache_read_input_tokens"
                            ),
                            "ts": time.time(),
                        },
                    )

                # Collect final-assistant text. Reset the accumulator at the
                # start of each NEW message_id so we end up with the last
                # turn's text, not the concatenation of every assistant
                # turn.
                if mid != last_message_id:
                    final_text_parts.clear()
                    last_message_id = mid
                for block in (getattr(msg, "content", None) or []):
                    block_cls = type(block).__name__
                    if block_cls == "TextBlock":
                        text = getattr(block, "text", "")
                        if text:
                            final_text_parts.append(text)
                    elif block_cls == "ToolUseBlock":
                        tool_use_id = getattr(block, "id", None)
                        if tool_use_id:
                            pending_tool_uses[tool_use_id] = time.monotonic()
                            orch.emit_event(
                                "tool_called",
                                {
                                    "ts": time.time(),
                                    "caller_id": spec.caller_id,
                                    "message_id": mid,
                                    "tool_use_id": tool_use_id,
                                    "name": getattr(block, "name", None),
                                    "input": getattr(block, "input", {}),
                                },
                            )

            elif cls == "UserMessage":
                # UserMessage carries tool-result echoes from the SDK.
                # Drain ToolResultBlocks to close open tool spans.
                for block in (getattr(msg, "content", None) or []):
                    if type(block).__name__ == "ToolResultBlock":
                        tool_use_id = getattr(block, "tool_use_id", None)
                        start = pending_tool_uses.pop(tool_use_id, None) if tool_use_id else None
                        duration_ms: Optional[int] = (
                            int(round((time.monotonic() - start) * 1000.0))
                            if start is not None
                            else None
                        )
                        orch.emit_event(
                            "tool_result",
                            {
                                "ts": time.time(),
                                "caller_id": spec.caller_id,
                                "tool_use_id": tool_use_id,
                                "duration_ms": duration_ms,
                                "is_error": getattr(block, "is_error", False) or False,
                            },
                        )

            elif cls == "ResultMessage":
                result_data = {
                    "total_cost_usd": getattr(msg, "total_cost_usd", 0.0) or 0.0,
                    "usage": dict(getattr(msg, "usage", {}) or {}),
                    "model_usage": dict(getattr(msg, "model_usage", {}) or {}),
                    "duration_ms": getattr(msg, "duration_ms", 0) or 0,
                    "duration_api_ms": getattr(msg, "duration_api_ms", 0) or 0,
                    "num_turns": getattr(msg, "num_turns", 0) or 0,
                    "stop_reason": getattr(msg, "stop_reason", "unknown") or "unknown",
                    "session_id": getattr(msg, "session_id", None),
                }
                orch.emit_event(
                    "run.cost",
                    {
                        "caller_id": spec.caller_id,
                        "ts": time.time(),
                        **result_data,
                    },
                )

            # SystemMessage / UserMessage / anything else: ignored by design.
            # UserMessage carries tool-result echoes; ToolUseBlocks are the
            # MCP layer's concern. Unknown types must not crash the loop.

    except asyncio.CancelledError:
        orch.emit_event(
            "agent_completed",
            {
                "caller_id": spec.caller_id,
                "terminated": False,
                "stop_reason": "cancelled",
                "ts": time.time(),
            },
        )
        raise
    except Exception as exc:
        orch.emit_event(
            "agent_error",
            {
                "caller_id": spec.caller_id,
                "exception": type(exc).__name__,
                "msg": str(exc),
                "ts": time.time(),
            },
        )
        raise

    terminated = bool(orch.was_terminated(spec.caller_id))
    stop_reason = result_data["stop_reason"] if result_data else "no_result"

    orch.emit_event(
        "agent_completed",
        {
            "caller_id": spec.caller_id,
            "terminated": terminated,
            "stop_reason": stop_reason,
            "ts": time.time(),
        },
    )

    # contract_violation == loop exited cleanly but the agent never
    # consumed a terminate sentinel. Orchestrator reads this flag and
    # applies the resume-not-terminate policy (docs/agent-runtime.md #4).
    contract_violation = not terminated

    return RunResult(
        final_text="".join(final_text_parts),
        total_cost_usd=float(result_data["total_cost_usd"]) if result_data else 0.0,
        total_usage=result_data["usage"] if result_data else {},
        num_turns=result_data["num_turns"] if result_data else 0,
        duration_ms=result_data["duration_ms"] if result_data else 0,
        duration_api_ms=result_data["duration_api_ms"] if result_data else 0,
        stop_reason=stop_reason,
        session_id=result_data["session_id"] if result_data else None,
        terminated=terminated,
        contract_violation=contract_violation,
        model_usage=result_data["model_usage"] if result_data else {},
    )


__all__ = ["SpawnSpec", "RunResult", "run_agent"]
