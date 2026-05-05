"""Unit tests for the on_disallowed_alias PreToolUse hook.

The hook is the visibility layer for SDK shadow tools (SendMessage,
TodoWrite). The SDK's disallowed_tools normally drops these tools before
any PreToolUse hook fires, so in normal operation this hook is a backstop.
If a future SDK regression or env leak resurfaces them, this hook turns
the silent SDK drop into a model-visible deny+reason.

These tests exercise the hook directly (bypassing the SDK layer) to
verify the deny-path semantics and the deny reason content.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from beidou.sdk_agent import build_hooks


class FakeOrchForHooks:
    """Minimal orchestrator stub for build_hooks unit tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        # report_status hook closes over deliver_message; not exercised here
        # but we provide a stub so build_hooks doesn't crash.
        self._gateway_called = False
        self._gateway_calls: list[tuple[str, list, Optional[str]]] = []

    def emit_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    async def gateway_ask_user_structured(self, caller_id, questions, context):
        self._gateway_called = True
        self._gateway_calls.append((caller_id, questions, context))
        return {"answers": [], "answer_text": ""}

    def assistant_text_for_turn(self, caller_id: str, tool_use_id: str) -> Optional[str]:
        return None

    def deliver_message(self, **kwargs: Any) -> None:
        pass


def _get_pretooluse_hook_for(orch: FakeOrchForHooks, matcher_name: str):
    hooks = build_hooks(orch, caller_id="test_agent", leader_id="leader1")
    matchers = hooks.get("PreToolUse", [])
    for m in matchers:
        if m.matcher == matcher_name:
            return m.hooks[0]
    raise AssertionError(f"No PreToolUse matcher named {matcher_name!r}; got {[m.matcher for m in matchers]}")


class TestDisallowedAliasHook:
    """Verify the hook denies SendMessage and TodoWrite with actionable reasons."""

    def test_hook_registered_for_sendmessage(self) -> None:
        """build_hooks registers a SendMessage matcher."""
        orch = FakeOrchForHooks()
        hooks = build_hooks(orch, caller_id="a", leader_id="b")
        names = [m.matcher for m in hooks["PreToolUse"]]
        assert "SendMessage" in names

    def test_hook_registered_for_todowrite(self) -> None:
        """build_hooks registers a TodoWrite matcher."""
        orch = FakeOrchForHooks()
        hooks = build_hooks(orch, caller_id="a", leader_id="b")
        names = [m.matcher for m in hooks["PreToolUse"]]
        assert "TodoWrite" in names

    def test_sendmessage_deny_reason_redirects_to_primitive(self) -> None:
        """SendMessage call gets denied with a reason pointing at mcp__beidou__send_message."""
        orch = FakeOrchForHooks()
        hook = _get_pretooluse_hook_for(orch, "SendMessage")

        result = asyncio.run(hook(
            {"tool_name": "SendMessage", "tool_input": {"to": "x", "body": "hi"}},
            tool_use_id="toolu_abc",
            context=None,
        ))

        hs = result.get("hookSpecificOutput", {})
        assert hs.get("permissionDecision") == "deny"
        reason = hs.get("permissionDecisionReason", "")
        assert "mcp__beidou__send_message" in reason
        assert "docs/tool-surface.md#send_message" in reason

    def test_todowrite_deny_reason_redirects_to_primitive(self) -> None:
        """TodoWrite call gets denied with a reason pointing at declare_plan/report_status."""
        orch = FakeOrchForHooks()
        hook = _get_pretooluse_hook_for(orch, "TodoWrite")

        result = asyncio.run(hook(
            {"tool_name": "TodoWrite", "tool_input": {"todos": [{"content": "x"}]}},
            tool_use_id="toolu_abc",
            context=None,
        ))

        hs = result.get("hookSpecificOutput", {})
        assert hs.get("permissionDecision") == "deny"
        reason = hs.get("permissionDecisionReason", "")
        assert "mcp__beidou__declare_plan" in reason
        assert "mcp__beidou__report_status" in reason
        assert "docs/tool-surface.md#declare_plan" in reason

    def test_emits_disallowed_alias_denied_event(self) -> None:
        """The hook emits a single disallowed_alias.denied event per call."""
        orch = FakeOrchForHooks()
        hook = _get_pretooluse_hook_for(orch, "SendMessage")

        asyncio.run(hook(
            {"tool_name": "SendMessage", "tool_input": {}},
            tool_use_id=None,
            context=None,
        ))

        denied = [p for name, p in orch.events if name == "disallowed_alias.denied"]
        assert len(denied) == 1
        assert denied[0]["caller_id"] == "test_agent"
        assert denied[0]["tool_name"] == "SendMessage"

    def test_unknown_tool_returns_empty(self) -> None:
        """If the matcher mis-routes a different tool to the hook, return empty (defensive)."""
        orch = FakeOrchForHooks()
        # Reach into the SendMessage matcher and call it with a wrong tool name.
        hook = _get_pretooluse_hook_for(orch, "SendMessage")
        result = asyncio.run(hook(
            {"tool_name": "NotARealTool", "tool_input": {}},
            tool_use_id=None,
            context=None,
        ))
        assert result == {}

    def test_no_synthetic_tool_pair(self) -> None:
        """Hook must not synthesise tool_called/tool_result events (same bug class as 82d5290)."""
        orch = FakeOrchForHooks()
        hook = _get_pretooluse_hook_for(orch, "TodoWrite")
        asyncio.run(hook(
            {"tool_name": "TodoWrite", "tool_input": {}},
            tool_use_id="toolu_zzz",
            context=None,
        ))
        names = [n for n, _ in orch.events]
        assert "tool_called" not in names
        assert "tool_result" not in names
