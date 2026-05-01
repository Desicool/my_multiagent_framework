"""Example eval handlers for custom-skill.

Eval handlers are fire-and-forget. They return ``None`` and emit results as
events via ``ctx.emit()``. They are fail-open: exceptions are logged but never
block the agent.
"""

import time

from beidou.agent.context import AgentStartContext, EventContext, TurnEvalContext


async def baseline_recorder(ctx: AgentStartContext) -> None:
    """Record agent configuration at spawn time."""
    ctx.emit(
        "eval.baseline",
        {
            "agent_id": ctx.agent_id,
            "skill_name": ctx.skill_name,
            "team_id": ctx.team_id,
            "spawn_ts": time.time(),
        },
    )


async def quality_scorer(ctx: TurnEvalContext) -> None:
    """Score each turn (0-3): output quality + action + efficiency."""
    score = 0
    if ctx.assistant_text and len(ctx.assistant_text) > 50:
        score += 1
    if ctx.tool_calls:
        score += 1
    if ctx.token_usage:
        total = ctx.token_usage.get("input_tokens", 0) + ctx.token_usage.get(
            "output_tokens", 0
        )
        if total < 2000:
            score += 1

    ctx.emit(
        "eval.score",
        {
            "agent_id": ctx.agent_id,
            "turn": ctx.turn_index,
            "score": score,
            "max_score": 3,
            "tool_calls": ctx.tool_calls,
        },
    )


async def metrics_collector(ctx: EventContext) -> None:
    """Collect metrics from tool_result and run.cost events."""
    if ctx.event_type == "tool_result":
        payload = ctx.event_payload
        ctx.emit(
            "eval.tool_metric",
            {
                "agent_id": ctx.agent_id,
                "tool_name": payload.get("name", "unknown"),
                "duration_ms": payload.get("duration_ms", 0),
                "is_error": payload.get("is_error", False),
            },
        )
    elif ctx.event_type == "run.cost":
        payload = ctx.event_payload
        ctx.emit(
            "eval.cost_metric",
            {
                "agent_id": ctx.agent_id,
                "total_cost_usd": payload.get("total_cost_usd", 0),
                "num_turns": payload.get("num_turns", 0),
            },
        )
