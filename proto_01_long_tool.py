"""Throwaway prototype: does claude-agent-sdk impose a per-tool-call timeout?

Approach:
  - Register a single in-process MCP tool `sleep_then_return(seconds)` that does
    `await asyncio.sleep(seconds)` and returns `{"slept": seconds}`.
  - Spawn a cheap Haiku agent whose system prompt tells it to call that tool
    exactly once and then reply with the result.
  - Drain the async iterator from `query(...)`, logging every message type
    and a short repr as it arrives. Time the end-to-end call.
  - Run first with seconds=60, then with seconds=180.

If the SDK enforces a tool timeout shorter than the sleep, the run either
raises, ends early, or yields an error/tool_result indicating timeout.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
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

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("ANTHROPIC_API_KEY not set; aborting.", file=sys.stderr)
    sys.exit(2)


@tool(
    "sleep_then_return",
    "Sleep for the given number of seconds, then return {\"slept\": seconds}. "
    "Blocks the tool handler for the full duration via asyncio.sleep.",
    {"seconds": int},
)
async def sleep_then_return(args: dict[str, Any]) -> dict[str, Any]:
    seconds = int(args["seconds"])
    t0 = time.monotonic()
    print(f"    [tool] sleep_then_return: sleeping {seconds}s", flush=True)
    await asyncio.sleep(seconds)
    elapsed = time.monotonic() - t0
    print(f"    [tool] sleep_then_return: woke after {elapsed:.2f}s", flush=True)
    return {
        "content": [
            {"type": "text", "text": f'{{"slept": {seconds}}}'}
        ]
    }


def _short(obj: Any, n: int = 240) -> str:
    s = repr(obj)
    return s if len(s) <= n else s[:n] + f"... <+{len(s)-n} chars>"


async def run_once(seconds: int) -> dict[str, Any]:
    """Run the agent once, asking it to call sleep_then_return(seconds).

    Returns a dict with outcome, elapsed, error info, and a list of (type, repr)
    pairs describing each yielded message.
    """
    server = create_sdk_mcp_server(
        name="proto-long-tool",
        version="0.0.1",
        tools=[sleep_then_return],
    )

    system_prompt = (
        "You are a test agent. You MUST do exactly the following: "
        "call the MCP tool `sleep_then_return` exactly once with seconds={s}, "
        "wait for its result, and then reply with a single short message "
        "that quotes the tool's JSON result. Do not call any other tools. "
        "Do not call the tool more than once."
    ).format(s=seconds)

    # The SDK exposes MCP tools to the model under the name
    # `mcp__<server_name>__<tool_name>`. We allow only that one.
    allowed_name = "mcp__proto-long-tool__sleep_then_return"

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        system_prompt=system_prompt,
        mcp_servers={"proto-long-tool": server},
        allowed_tools=[allowed_name],
        permission_mode="bypassPermissions",
        max_turns=4,
    )

    prompt = f"Call sleep_then_return with seconds={seconds} and then tell me the result."

    messages: list[tuple[str, str]] = []
    outcome: str
    error_info: dict[str, Any] | None = None

    t0 = time.monotonic()
    try:
        async for msg in query(prompt=prompt, options=options):
            elapsed = time.monotonic() - t0
            tname = type(msg).__name__
            short = _short(msg)
            print(f"  [{elapsed:7.2f}s] {tname}: {short}", flush=True)
            messages.append((tname, short))
        outcome = "completed"
    except BaseException as e:  # capture KeyboardInterrupt / asyncio cancels too
        elapsed = time.monotonic() - t0
        tb = traceback.format_exc()
        error_info = {
            "exception_type": type(e).__name__,
            "message": str(e),
            "traceback_tail": "\n".join(tb.splitlines()[-12:]),
        }
        print(
            f"  [{elapsed:7.2f}s] EXCEPTION {type(e).__name__}: {e}",
            flush=True,
        )
        outcome = "raised"

    elapsed = time.monotonic() - t0
    return {
        "seconds_requested": seconds,
        "outcome": outcome,
        "elapsed": elapsed,
        "error": error_info,
        "num_messages": len(messages),
        "messages": messages,
    }


async def main() -> None:
    results = []
    for secs in (60, 180):
        print(f"\n===== RUN: sleep_then_return(seconds={secs}) =====", flush=True)
        res = await run_once(secs)
        print(
            f"----- RESULT: seconds={secs} outcome={res['outcome']} "
            f"elapsed={res['elapsed']:.2f}s messages={res['num_messages']}",
            flush=True,
        )
        if res["error"]:
            print(f"       error: {res['error']}", flush=True)
        results.append(res)

    print("\n===== SUMMARY =====", flush=True)
    for r in results:
        line = (
            f"seconds={r['seconds_requested']:>3}  "
            f"outcome={r['outcome']:<10}  "
            f"elapsed={r['elapsed']:7.2f}s  "
            f"messages={r['num_messages']}"
        )
        if r["error"]:
            line += f"  error={r['error']['exception_type']}: {r['error']['message']}"
        print(line, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
