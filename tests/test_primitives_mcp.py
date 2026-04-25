"""Tests for beidou/primitives/mcp.py.

Each test invokes the real MCP ``CallToolRequest`` handler of the
per-spawn server returned by :func:`build_mcp_server_for`. This exercises
the full SDK plumbing (schema validation, MCP ``CallToolResult`` shaping,
``is_error`` translation) without spawning an actual agent.

The ``FakeOrchestrator`` is imported from ``tests.test_primitives_core`` --
exactly the same in-memory duck-typed Orchestrator the core primitive
tests use, so the MCP layer sits on top of the same path that production
code would.

NOTE: ``tool_called`` / ``tool_result`` event contracts are now asserted in
``tests/test_sdk_agent.py`` at the drain-loop level. The MCP wrapper no
longer emits those events (by design -- see ``docs/observability.md`` and
``beidou/sdk_agent.py``).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from mcp import types as mcp_types

from beidou.primitives import build_mcp_server_for
from beidou.primitives.core import FAN_OUT_CAP, WAIT_CEILING, Message

# Reuse the FakeOrchestrator + _build helper from the core test module.
from tests.test_primitives_core import FakeOrchestrator, _build


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


async def _call(server_cfg: dict, tool_name: str, arguments: dict[str, Any]):
    """Invoke an MCP tool via the server's CallToolRequest handler.

    Returns the underlying ``CallToolResult`` (``.content`` list,
    ``.isError`` bool) from the MCP lowlevel server.
    """
    srv = server_cfg["instance"]
    handler = srv.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=tool_name, arguments=arguments),
    )
    server_result = await handler(req)
    return server_result.root


def _text_payload(call_result) -> Any:
    """Return the JSON-decoded payload from the first TextContent block."""
    assert call_result.content, "tool returned no content blocks"
    first = call_result.content[0]
    # MCP TextContent has .type and .text attributes.
    assert first.type == "text", f"expected text content, got {first.type}"
    return json.loads(first.text)


def run(coro) -> object:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def test_server_lists_all_eight_tools():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        srv = cfg["instance"]
        handler = srv.request_handlers[mcp_types.ListToolsRequest]
        req = mcp_types.ListToolsRequest(method="tools/list")
        result = (await handler(req)).root
        names = {t.name for t in result.tools}
        assert names == {
            "send_message",
            "read_messages",
            "wait_for_message",
            "list_peers",
            "ask_user",
            "report_status",
            "create_team",
            "terminate_child",
        }

    run(body())


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


def test_send_message_happy_path():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(cfg, "send_message", {"to": "B", "content": "hi"})
        assert result.isError in (False, None)
        payload = _text_payload(result)
        assert payload["delivered"] is True
        assert isinstance(payload["message_id"], str)
        assert o.inbox_size("B") == 1

    run(body())


def test_send_message_unknown_recipient_is_structured_error():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(cfg, "send_message", {"to": "ghost", "content": "x"})
        assert result.isError is True
        payload = _text_payload(result)
        assert payload["error"] == "unknown_recipient"
        assert "ghost" in payload["message"]

    run(body())


# ---------------------------------------------------------------------------
# read_messages
# ---------------------------------------------------------------------------


def test_read_messages_empty_inbox():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="B")

    async def body():
        result = await _call(cfg, "read_messages", {})
        assert result.isError in (False, None)
        payload = _text_payload(result)
        assert payload == {"messages": []}

    run(body())


def test_read_messages_drains():
    o = _build()
    cfg_b = build_mcp_server_for(o, caller_id="B")

    async def body():
        # Seed B's inbox via orchestrator directly.
        await o.inbox_put("B", Message("A", "one", time.time(), "m1", "user"))
        await o.inbox_put("B", Message("A", "two", time.time(), "m2", "user"))

        result = await _call(cfg_b, "read_messages", {})
        payload = _text_payload(result)
        assert [m["content"] for m in payload["messages"]] == ["one", "two"]

        # Second drain is empty.
        result2 = await _call(cfg_b, "read_messages", {})
        assert _text_payload(result2) == {"messages": []}

    run(body())


# ---------------------------------------------------------------------------
# wait_for_message
# ---------------------------------------------------------------------------


def test_wait_for_message_times_out():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="B")

    async def body():
        result = await _call(cfg, "wait_for_message", {"timeout": 0.05})
        assert result.isError in (False, None)
        payload = _text_payload(result)
        assert payload == {"timeout": True}

    run(body())


def test_wait_for_message_returns_posted_message():
    o = _build()
    cfg_b = build_mcp_server_for(o, caller_id="B")

    async def body():
        async def sender():
            await asyncio.sleep(0.05)
            await o.inbox_put(
                "B", Message("A", "wake", time.time(), "m1", "user")
            )

        send_task = asyncio.create_task(sender())
        result = await _call(cfg_b, "wait_for_message", {"timeout": 2.0})
        await send_task

        payload = _text_payload(result)
        assert payload["timeout"] is False
        assert payload["message"]["content"] == "wake"

    run(body())


def test_wait_for_message_with_from_filter():
    """The tool schema field is 'from' (Python reserved word); the adapter
    translates to the core primitive's ``from_`` argument."""
    o = _build()
    cfg_b = build_mcp_server_for(o, caller_id="B")

    async def body():
        # Non-matching sender then matching sender; filter must skip the first.
        await o.inbox_put("B", Message("R", "from R", time.time(), "m1", "user"))
        await o.inbox_put("B", Message("A", "from A", time.time(), "m2", "user"))

        result = await _call(
            cfg_b, "wait_for_message", {"timeout": 1.0, "from": "A"}
        )
        payload = _text_payload(result)
        assert payload["timeout"] is False
        assert payload["message"]["from"] == "A"
        assert payload["message"]["content"] == "from A"

    run(body())


def test_wait_for_message_over_ceiling_is_structured_error():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="B")

    async def body():
        result = await _call(
            cfg, "wait_for_message", {"timeout": WAIT_CEILING + 1}
        )
        assert result.isError is True
        payload = _text_payload(result)
        assert payload["error"] == "timeout_over_ceiling"

    run(body())


# ---------------------------------------------------------------------------
# list_peers
# ---------------------------------------------------------------------------


def test_list_peers_default_scope():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(cfg, "list_peers", {})
        payload = _text_payload(result)
        ids = {p["agent_id"] for p in payload["peers"]}
        assert ids == {"R", "B"}

    run(body())


def test_list_peers_invalid_scope_rejected_by_schema():
    """The tool schema declares a strict enum for scope, so an invalid value
    is rejected at the MCP input-validation layer BEFORE the primitive runs.
    This is stricter than (and subsumes) the primitive's own invalid_scope
    check."""
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(cfg, "list_peers", {"scope": "galaxy"})
        assert result.isError is True
        # MCP returns a plaintext validation error in the first text block.
        assert "galaxy" in result.content[0].text

    run(body())


# ---------------------------------------------------------------------------
# ask_user
# ---------------------------------------------------------------------------


def test_ask_user_happy_path():
    o = _build()
    o.gateway_answer = "yes go"
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(
            cfg, "ask_user", {"question": "proceed?", "context": "bg"}
        )
        assert result.isError in (False, None)
        payload = _text_payload(result)
        assert payload == {"answer": "yes go"}

    run(body())


def test_ask_user_gateway_unavailable_is_structured_error():
    o = _build()
    o.gateway_available = False
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(cfg, "ask_user", {"question": "?"})
        assert result.isError is True
        payload = _text_payload(result)
        assert payload["error"] == "gateway_unavailable"

    run(body())


# ---------------------------------------------------------------------------
# report_status
# ---------------------------------------------------------------------------


def test_report_status_happy_path():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(
            cfg, "report_status", {"state": "done", "detail": "shipped"}
        )
        payload = _text_payload(result)
        assert payload == {"recorded": True}
        assert ("A", "done", "shipped") in o.statuses

    run(body())


def test_report_status_invalid_state_rejected_by_schema():
    """Enum schema rejects bad values at the MCP layer (stricter than, and
    subsumes, the primitive's invalid_state check)."""
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(cfg, "report_status", {"state": "exploding"})
        assert result.isError is True
        assert "exploding" in result.content[0].text

    run(body())


# ---------------------------------------------------------------------------
# create_team
# ---------------------------------------------------------------------------


def test_create_team_happy_path():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="R")

    async def body():
        result = await _call(
            cfg,
            "create_team",
            {
                "name": "impl",
                "task": "build",
                "roles": [
                    {"role": "coder", "template": "junior_engineer", "description": "x"},
                    {"role": "tester", "template": "junior_engineer", "description": "y"},
                ],
            },
        )
        payload = _text_payload(result)
        assert "team_id" in payload
        assert len(payload["members"]) == 2
        # Self-lead invariant: R leads the new team.
        assert o.leader_of(payload["team_id"]) == "R"

    run(body())


def test_create_team_fanout_exceeded_is_structured_error():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="R")

    async def body():
        roles = [{"role": f"r{i}", "template": "t"} for i in range(FAN_OUT_CAP + 1)]
        result = await _call(
            cfg, "create_team", {"name": "big", "task": "x", "roles": roles}
        )
        assert result.isError is True
        payload = _text_payload(result)
        assert payload["error"] == "fanout_exceeded"

    run(body())


# ---------------------------------------------------------------------------
# terminate_child
# ---------------------------------------------------------------------------


def test_terminate_child_happy_path():
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="R")

    async def body():
        result = await _call(cfg, "terminate_child", {"agent_id": "A"})
        payload = _text_payload(result)
        assert payload == {"sentinel_posted": True}
        # Sentinel landed in A's inbox.
        msgs = await o.inbox_drain("A")
        assert len(msgs) == 1
        assert msgs[0].kind == "terminate"
        assert msgs[0].content == "__terminate__"

    run(body())


def test_terminate_child_not_leader_is_structured_error():
    o = _build()
    # A does not lead the root team; R does.
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(cfg, "terminate_child", {"agent_id": "B"})
        assert result.isError is True
        payload = _text_payload(result)
        assert payload["error"] == "not_leader"

    run(body())


# ---------------------------------------------------------------------------
# Observability / caller_id closure
# ---------------------------------------------------------------------------


def test_caller_id_cannot_be_overridden_by_tool_input():
    """The closure binds caller_id; even if the tool input carried a stray
    'caller_id' key, the wrapper ignores it and passes the closure value."""
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        # Extra 'caller_id' in args MUST NOT reach the primitive.
        await _call(
            cfg,
            "send_message",
            {"to": "B", "content": "hi", "caller_id": "R"},
        )
        # Message is attributed to the closure's caller ("A"), not "R".
        msgs = await o.inbox_drain("B")
        assert len(msgs) == 1
        assert msgs[0].from_id == "A"

    run(body())


def test_redact_args_large_string():
    """Direct unit test of _redact_args() as a pure function.

    Tool-event observability (duration, error flags, etc.) is asserted at the
    drain-loop level in ``tests/test_sdk_agent.py``.
    """
    from beidou.primitives.mcp import _redact_args

    big = "x" * 2000
    result = _redact_args({"to": "B", "content": big})
    # Large string is redacted.
    assert result["content"].startswith("<redacted: ")
    assert "2000 chars" in result["content"]
    # Small arg passes through unchanged.
    assert result["to"] == "B"


def test_redact_args_large_dict():
    """Large nested dicts are redacted by JSON-encoded length."""
    from beidou.primitives.mcp import _redact_args

    big_list = ["x" * 100] * 20  # > 512 chars when JSON-encoded
    result = _redact_args({"items": big_list, "name": "short"})
    assert result["items"].startswith("<redacted: ")
    assert result["name"] == "short"


def test_redact_args_small_values_pass_through():
    """Values under the threshold are returned unchanged."""
    from beidou.primitives.mcp import _redact_args

    args = {"to": "B", "content": "hello world", "count": 42}
    result = _redact_args(args)
    assert result == args
