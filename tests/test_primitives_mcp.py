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
from typing import Any

import pytest
from mcp import types as mcp_types

from beidou.primitives import build_mcp_server_for
from beidou.primitives.core import FAN_OUT_CAP, Message

# Reuse the FakeOrchestrator + _build helper from the core test module.
from test_primitives_core import FakeOrchestrator, _build


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


def test_server_lists_all_tools():
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
            "list_peers",
            "ask_user",
            "report_status",
            "create_team",
            "terminate_child",
            "list_pending_reviews",
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


_FREE_TEXT_Q = [
    {
        "question": "proceed?",
        "header": "",
        "multiSelect": False,
        "options": [],
    }
]


def test_ask_user_happy_path():
    o = _build()
    o.gateway_answer = "yes go"
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(
            cfg, "ask_user", {"questions": _FREE_TEXT_Q, "context": "bg"}
        )
        assert result.isError in (False, None)
        payload = _text_payload(result)
        assert payload["answer_text"] == "yes go"
        assert "answers" in payload

    run(body())


def test_ask_user_gateway_unavailable_is_structured_error():
    o = _build()
    o.gateway_available = False
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        result = await _call(cfg, "ask_user", {"questions": _FREE_TEXT_Q})
        assert result.isError is True
        payload = _text_payload(result)
        assert payload["error"] == "gateway_unavailable"

    run(body())


def test_ask_user_tool_schema_shape():
    """The ask_user MCP tool exposes the Claude Code wire shape for questions."""
    o = _build()
    cfg = build_mcp_server_for(o, caller_id="A")

    async def body():
        srv = cfg["instance"]
        handler = srv.request_handlers[mcp_types.ListToolsRequest]
        req = mcp_types.ListToolsRequest(method="tools/list")
        result = (await handler(req)).root
        ask_user_tool = next(t for t in result.tools if t.name == "ask_user")
        schema = ask_user_tool.inputSchema

        # Top-level must require "questions" array.
        assert "questions" in schema.get("required", [])
        props = schema["properties"]
        assert "questions" in props

        questions_schema = props["questions"]
        assert questions_schema["type"] == "array"

        item_schema = questions_schema["items"]
        item_props = item_schema["properties"]

        # camelCase multiSelect on the wire.
        assert "multiSelect" in item_props

        # options items must require ["label", "description"].
        options_items = item_props["options"]["items"]
        assert set(options_items.get("required", [])) == {"label", "description"}

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
                    {"role": "coder", "skill": "junior_engineer", "description": "x"},
                    {"role": "tester", "skill": "junior_engineer", "description": "y"},
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
        roles = [{"role": f"r{i}", "skill": "t"} for i in range(FAN_OUT_CAP + 1)]
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
    """Happy path via MCP: child has reported done (approve path)."""
    o = _build()
    # Set completion_pending=True so the completion gate allows the call.
    o.agents["A"].completion_pending = True
    cfg = build_mcp_server_for(o, caller_id="R")

    async def body():
        # force is optional in the MCP schema; omit it for the default approve path.
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
