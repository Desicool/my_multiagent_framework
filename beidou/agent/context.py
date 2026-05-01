"""Typed per-hook-point context dataclasses.

These are the context objects passed to gate and eval handlers at each hook
point. They provide structured access to the data relevant to that point
without exposing internal runtime objects.

Return types for gate handlers:
    - ``Pass()`` — allow the operation to proceed.
    - ``Block(reason)`` — deny with an explanation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Pass:
    """Return value from a gate handler indicating the operation should proceed."""


@dataclass
class Block:
    """Return value from a gate handler indicating the operation should be denied.

    Attributes:
        reason: Human-readable explanation of why the operation was blocked.
    """
    reason: str


@dataclass
class AgentStartContext:
    """``before_agent_start`` — before first SDK message, after spawn.

    Allows eval handlers to observe the agent's configuration and record a
    baseline before any SDK interaction begins.
    """

    agent_id: str
    skill_name: str
    team_id: str | None
    leader_id: str
    cwd: str | None
    emit: Callable[[str, dict], None]  # emit event to JSONL


@dataclass
class InputContext:
    """``filter_input`` — before a message enters the agent's context window.

    Gate handlers can block incoming messages based on content or sender.
    """

    agent_id: str
    from_id: str                 # who sent this message
    message_kind: str            # "task", "message", "completion_report", "rework"
    content: str
    emit: Callable[[str, dict], None]


@dataclass
class ToolCallContext:
    """``validate_tool_call`` — before a tool is executed (PreToolUse).

    Gate handlers can block dangerous or unauthorized tool calls.
    """

    agent_id: str
    tool_name: str
    tool_input: dict
    tool_use_id: str
    emit: Callable[[str, dict], None]


@dataclass
class ToolResultContext:
    """``validate_tool_result`` — after a tool returns (PostToolUse).

    Gate handlers can inspect (but not modify) tool results and block
    malicious or unexpected results.
    """

    agent_id: str
    tool_name: str
    tool_use_id: str
    result: Any                   # the raw tool result
    is_error: bool
    duration_ms: int
    emit: Callable[[str, dict], None]


@dataclass
class OutputContext:
    """``filter_output`` — before agent text is delivered to peers/user.

    Gate handlers can block sensitive or malformed agent output.
    """

    agent_id: str
    output_text: str             # the text about to be delivered
    output_kind: str             # "assistant_text", "completion_report", "message"
    emit: Callable[[str, dict], None]


@dataclass
class TurnEvalContext:
    """``evaluate_turn`` — end of each agent turn.

    Eval handlers can score turn quality or collect metrics.
    """

    agent_id: str
    turn_index: int
    assistant_text: str | None
    tool_calls: list[str]        # tool names called this turn
    token_usage: dict | None     # from turn.usage event
    emit: Callable[[str, dict], None]


@dataclass
class EventContext:
    """``on_event`` — any named Beidou event emitted.

    Eval handlers subscribe to specific event types and receive the full
    event payload.
    """

    agent_id: str
    event_type: str              # Beidou event name
    event_payload: dict          # full event dict
    emit: Callable[[str, dict], None]


__all__ = [
    "AgentStartContext",
    "Block",
    "EventContext",
    "InputContext",
    "OutputContext",
    "Pass",
    "ToolCallContext",
    "ToolResultContext",
    "TurnEvalContext",
]
