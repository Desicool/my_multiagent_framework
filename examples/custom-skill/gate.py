"""Gate handlers for the custom-skill example.

Each handler is an async function receiving a typed context and returning
``Pass()`` or ``Block(reason=...)``.

Gate handlers are **fail-closed**: if this module cannot be imported, ALL
gate hooks return ``Block``. If a handler raises an exception, it is treated
as ``Block(reason="gate_handler_error: <exception>")``.

See ``docs/skill-modules.md`` for the full specification.
"""

from __future__ import annotations

import re

from beidou.agent.context import (
    Block,
    OutputContext,
    Pass,
    ToolCallContext,
    ToolResultContext,
)


# ── Dangerous command patterns ──────────────────────────────────────────

DESTRUCTIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"rm\s+-rf\s+/"),
    re.compile(r"rm\s+-rf\s+--no-preserve-root"),
    re.compile(r">\s+/dev/sda"),
    re.compile(r"mkfs\."),
    re.compile(r"dd\s+if=.*of=/dev/sd"),
    re.compile(r":\(\)\s*\{.*:\(\)\s*;\s*\};"),   # forkbomb
    re.compile(r"DROP\s+DATABASE", re.IGNORECASE),
    re.compile(r"DROP\s+TABLE", re.IGNORECASE),
]


async def block_destructive_commands(ctx: ToolCallContext) -> Pass | Block:
    """Block bash calls containing destructive shell patterns.

    This gate fires on every tool call. It checks the ``tool_input`` for known
    dangerous command patterns and returns Block if any are found.
    """
    if ctx.tool_name not in ("Bash", "bash"):
        return Pass()

    command = str(ctx.tool_input.get("command", ""))

    for pattern in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            ctx.emit(
                "gate.blocked_destructive_command",
                {
                    "tool_use_id": ctx.tool_use_id,
                    "pattern": pattern.pattern,
                    "command_truncated": command[:120],
                },
            )
            return Block(
                reason=f"Destructive command blocked: matched pattern {pattern.pattern}"
            )

    return Pass()


# ── Tool error warnings ─────────────────────────────────────────────────

async def warn_on_error(ctx: ToolResultContext) -> Pass | Block:
    """Block (flag) tool results that returned errors.

    This is an informational gate: when a tool returns an error, the agent
    should be aware of it. The Block reason serves as a signal; the agent
    remains in control.
    """
    if ctx.is_error:
        ctx.emit(
            "gate.tool_error_detected",
            {
                "tool_use_id": ctx.tool_use_id,
                "tool_name": ctx.tool_name,
                "duration_ms": ctx.duration_ms,
            },
        )
        return Block(
            reason=(
                f"Tool {ctx.tool_name} (id={ctx.tool_use_id}) returned an error "
                f"after {ctx.duration_ms}ms. Investigate before proceeding."
            )
        )

    return Pass()


# ── Secret leakage detection ────────────────────────────────────────────

# Loose patterns — not exhaustive, just enough to demonstrate the hook point.
SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?i)AKIA[0-9A-Z]{16}"),              # AWS Access Key ID
    re.compile(r"(?i)ghp_[a-zA-Z0-9]{36}"),            # GitHub personal access token
    re.compile(r"(?i)gho_[a-zA-Z0-9]{36}"),            # GitHub OAuth access token
    re.compile(r"(?i)sk-[a-zA-Z0-9]{32,}"),            # OpenAI / Anthropic secret key
    re.compile(r"(?i)-----BEGIN\s+(RSA|EC|OPENSSH)\s+PRIVATE KEY-----"),
]


async def block_secret_leakage(ctx: OutputContext) -> Pass | Block:
    """Prevent leakage of secrets in agent output text.

    Scans the agent's output text for patterns that resemble credentials or
    private keys. If found, blocks the output and emits a warning event.
    """
    for pattern in SECRET_PATTERNS:
        if pattern.search(ctx.output_text):
            ctx.emit(
                "gate.secret_leakage_blocked",
                {
                    "output_kind": ctx.output_kind,
                    "matched_pattern": pattern.pattern[:60],
                },
            )
            return Block(
                reason=(
                    f"Output blocked: potential secret key pattern detected "
                    f"(matched: {pattern.pattern[:60]}). Remove secrets before output."
                )
            )

    return Pass()
