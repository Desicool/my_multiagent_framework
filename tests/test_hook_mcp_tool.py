"""Regression test: PostToolUse hook fires for MCP-namespaced tools.

Proves that ``claude_agent_sdk``'s ``PostToolUse`` hook fires for
``mcp__beidou__test_echo`` — an MCP-namespaced tool invocation.

WHY THIS TEST EXISTS:
A larger refactor depends on PostToolUse hooks being dispatched for
``mcp__beidou__*`` tool invocations. Two pre-implementation reviewers flagged
that there was no local proof of this. This test is the regression anchor.

Skipped unless both ANTHROPIC_API_KEY and the ``claude`` CLI are present
(same policy as the integration test in ``tests/test_sdk_agent.py``).
Mark: ``@pytest.mark.integration``
"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

import pytest

# Honour .env (project uses python-dotenv per CLAUDE.md).
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except ImportError:
    pass

_CLAUDE_CLI = shutil.which("claude")
_API_KEY = os.getenv("ANTHROPIC_API_KEY")


@pytest.mark.integration
@pytest.mark.skipif(
    not _API_KEY or not _CLAUDE_CLI,
    reason="requires ANTHROPIC_API_KEY and claude CLI on PATH",
)
def test_post_tool_use_hook_fires_for_mcp_namespaced_tool():
    """PostToolUse hook callback is invoked when the agent calls mcp__beidou__test_echo."""
    import claude_agent_sdk
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool
    from claude_agent_sdk.types import HookMatcher

    # ------------------------------------------------------------------
    # 1. Build a minimal MCP server exposing only test_echo.
    # ------------------------------------------------------------------

    @tool(
        "test_echo",
        "Echo back a message.",
        {
            "type": "object",
            "properties": {
                "msg": {
                    "type": "string",
                    "description": "Message to echo.",
                },
            },
            "required": ["msg"],
        },
    )
    async def _test_echo(args: dict[str, Any]) -> dict[str, Any]:
        return {"echo": args.get("msg", "")}

    mcp_server = create_sdk_mcp_server(
        name="beidou",
        version="1.0.0",
        tools=[_test_echo],
    )

    # ------------------------------------------------------------------
    # 2. Hook capture list (closed over by the async callback).
    # ------------------------------------------------------------------

    hook_calls: list[tuple[Any, Any, Any]] = []

    async def on_post(
        input_data: Any,
        tool_use_id: str | None,
        context: Any,
    ) -> dict:
        # HookInput is a TypedDict (dict subclass); use .get() for safety.
        hook_calls.append(
            (
                input_data.get("tool_name"),
                input_data.get("tool_input"),
                input_data.get("tool_response"),
            )
        )
        return {}

    # ------------------------------------------------------------------
    # 3. Build ClaudeAgentOptions.
    # ------------------------------------------------------------------

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        mcp_servers={"beidou": mcp_server},
        allowed_tools=["mcp__beidou__test_echo"],
        permission_mode="bypassPermissions",
        setting_sources=[],
        hooks={"PostToolUse": [HookMatcher(matcher="mcp__beidou__test_echo", hooks=[on_post])]},
        system_prompt=(
            "You are a minimal test agent. "
            "Call mcp__beidou__test_echo(msg='hi') exactly once, "
            "then stop."
        ),
    )

    # ------------------------------------------------------------------
    # 4. Run and drain the message iterator.
    # ------------------------------------------------------------------

    async def _drain() -> None:
        async for _ in claude_agent_sdk.query(
            prompt="Call the tool now.",
            options=options,
        ):
            pass

    asyncio.run(_drain())

    # ------------------------------------------------------------------
    # 5. Assertions.
    # ------------------------------------------------------------------

    assert len(hook_calls) >= 1, (
        f"PostToolUse hook was never called; hook_calls={hook_calls!r}"
    )

    tool_name, tool_input, _tool_response = hook_calls[0]

    assert tool_name == "mcp__beidou__test_echo", (
        f"Expected tool_name='mcp__beidou__test_echo', got {tool_name!r}"
    )

    assert isinstance(tool_input, dict), (
        f"tool_input should be a dict, got {type(tool_input)!r}: {tool_input!r}"
    )

    assert tool_input.get("msg") == "hi", (
        f"Expected tool_input['msg']=='hi', got tool_input={tool_input!r}"
    )
