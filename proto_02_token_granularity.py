"""proto_02_token_granularity.py

Throw-away probe: what token/cost accounting does claude-agent-sdk v0.1.66
expose through query()'s async iterator?

Spawns claude-haiku-4-5-20251001, registers an in-process MCP tool `echo`,
and forces a multi-turn conversation by asking it to call echo three times.
Every yielded message is dumped verbatim so we can see exactly which fields
carry usage / cost information, and at what granularity.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from typing import Any

from dotenv import load_dotenv

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

load_dotenv()

USAGE_KEY_HINTS = (
    "token",
    "cost",
    "cache",
    "usage",
    "input",
    "output",
)


# ----- MCP tool ----------------------------------------------------------

@tool(
    name="echo",
    description="Echo back the provided text unchanged.",
    input_schema={"text": str},
)
async def echo_tool(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": args["text"]}]}


# ----- helpers -----------------------------------------------------------

def _dump(obj: Any) -> Any:
    """Make dataclasses / unknown objects JSON-ish for logging."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _dump(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, list):
        return [_dump(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


def _highlight_usage(data: Any, path: str = "") -> list[tuple[str, Any]]:
    """Walk a dumped dict/list looking for usage-ish keys."""
    hits: list[tuple[str, Any]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            key_lower = str(k).lower()
            if any(h in key_lower for h in USAGE_KEY_HINTS) and not isinstance(v, (dict, list)):
                hits.append((f"{path}.{k}" if path else k, v))
            hits.extend(_highlight_usage(v, f"{path}.{k}" if path else k))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            hits.extend(_highlight_usage(v, f"{path}[{i}]"))
    return hits


# ----- main --------------------------------------------------------------

async def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set")

    mcp_server = create_sdk_mcp_server(
        name="proto_echo",
        version="0.1.0",
        tools=[echo_tool],
    )

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        mcp_servers={"proto": mcp_server},
        allowed_tools=["mcp__proto__echo"],
        permission_mode="bypassPermissions",
        system_prompt=(
            "You are a test agent. When asked to call a tool N times, call it "
            "exactly N times in separate turns, one per message, before "
            "producing any final text answer."
        ),
        max_turns=10,
    )

    prompt = (
        "Call the `echo` tool three times with the texts 'one', 'two', and "
        "'three' (one tool call per turn, in that order). After all three "
        "calls have returned, tell me in a single sentence what you called."
    )

    msg_index = 0
    per_assistant_usage: list[dict[str, Any]] = []
    result_usage: dict[str, Any] | None = None

    print("=" * 72)
    print("DRAINING query(...) ITERATOR")
    print("=" * 72)

    async for msg in query(prompt=prompt, options=options):
        msg_index += 1
        cls = type(msg).__name__
        dumped = _dump(msg)
        hits = _highlight_usage(dumped)

        print(f"\n--- msg[{msg_index}] type={cls} ---")
        # Truncate long repr; still print raw for cross-check.
        raw_repr = repr(msg)
        if len(raw_repr) > 800:
            raw_repr = raw_repr[:800] + "...<truncated>"
        print(f"repr: {raw_repr}")
        print("fields:")
        print(json.dumps(dumped, indent=2, default=str)[:2000])
        if hits:
            print("USAGE/COST HITS:")
            for path, val in hits:
                print(f"  {path} = {val}")

        if isinstance(msg, AssistantMessage):
            per_assistant_usage.append(
                {
                    "message_id": msg.message_id,
                    "model": msg.model,
                    "stop_reason": msg.stop_reason,
                    "usage": msg.usage,
                }
            )
        elif isinstance(msg, ResultMessage):
            result_usage = {
                "total_cost_usd": msg.total_cost_usd,
                "duration_ms": msg.duration_ms,
                "duration_api_ms": msg.duration_api_ms,
                "num_turns": msg.num_turns,
                "usage": msg.usage,
                "model_usage": msg.model_usage,
            }

    # ----- summary -------------------------------------------------------
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"total messages yielded: {msg_index}")
    print(f"assistant messages with usage: "
          f"{sum(1 for u in per_assistant_usage if u['usage'])}")
    print("\nPer-AssistantMessage usage payloads:")
    for i, u in enumerate(per_assistant_usage, 1):
        print(f"  [{i}] id={u['message_id']} stop={u['stop_reason']}")
        print(f"      usage={u['usage']}")

    print("\nFinal ResultMessage rollup:")
    print(json.dumps(result_usage, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
