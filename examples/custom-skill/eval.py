"""Eval handlers for the custom-skill example.

Each handler is an async function receiving a typed context and returning
``None``. Results are emitted as events via ``ctx.emit()``.

Eval handlers are **fail-open**: if this module cannot be imported, eval
hooks are skipped with a warning. If a handler raises an exception, the
error is logged and eval continues.

See ``docs/skill-modules.md`` for the full specification.
"""

from __future__ import annotations

from beidou.agent.context import AgentStartContext, EventContext, TurnEvalContext


async def log_agent_start(ctx: AgentStartContext) -> None:
    """Record agent startup configuration to the event log.

    This handler runs *before* the agent's first SDK message, making it a
    good place to establish baseline observability.
    """
    ctx.emit(
        "eval.agent_start",
        {
            "skill_name": ctx.skill_name,
            "team_id": ctx.team_id,
            "leader_id": ctx.leader_id,
            "cwd": ctx.cwd,
        },
    )


async def score_turn_quality(ctx: TurnEvalContext) -> None:
    """Score each agent turn by tool diversity and output length, then emit.

    Demonstrates how to inspect turn-level metadata and emit structured
    eval events for downstream analysis (e.g., Grafana dashboard).

    Scores:
        - ``tool_diversity``: number of unique tool names called this turn.
        - ``has_output``: whether the agent produced any assistant text.
        - ``output_length``: character count of assistant text (or 0).
    """
    tool_diversity = len(set(ctx.tool_calls))
    output_length = len(ctx.assistant_text) if ctx.assistant_text else 0

    ctx.emit(
        "eval.turn_score",
        {
            "turn_index": ctx.turn_index,
            "tool_diversity": tool_diversity,
            "has_output": ctx.assistant_text is not None,
            "output_length": output_length,
            "total_tokens": (
                ctx.token_usage.get("total_tokens")
                if ctx.token_usage
                else None
            ),
        },
    )


async def log_error_events(ctx: EventContext) -> None:
    """Log a human-readable summary when certain error-level events fire.

    Subscribes via ``module.toml`` to: ``tool_result`` (where is_error=true),
    ``agent_error``, ``contract_violation``, and ``liveness.escalated_to_user``.
    """
    summary = _summarise_event(ctx.event_type, ctx.event_payload)
    if summary:
        ctx.emit("eval.event_summary", {"summary": summary})


def _summarise_event(event_type: str, payload: dict) -> str | None:
    """Return a one-line summary for an event, or None to skip."""
    if event_type == "tool_result" and payload.get("is_error"):
        return (
            f"Tool error: {payload.get('tool_name', '?')} "
            f"failed after {payload.get('duration_ms', '?')}ms"
        )
    elif event_type == "agent_error":
        return f"Agent error: {payload.get('error', '?')}"
    elif event_type == "contract_violation":
        return (
            f"Contract violation by {payload.get('agent_id', '?')}: "
            f"{payload.get('reason', '?')}"
        )
    elif event_type == "liveness.escalated_to_user":
        return (
            f"Liveness escalated to user by agent "
            f"{payload.get('agent_id', '?')}"
        )
    return None
