"""Unit tests for the product_manager_v2 skill-module gate.

Verifies that the validate_tool_call gate enforces the "at least 2 ask_user
rounds before report_status(state='done')" contract from SKILL.md §1 Core
DOs. Also exercises the HookRegistry end-to-end on the bundled skill
directory to prove the loader + parser + handler-resolution path works.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from beidou.agent.context import Block, Pass, ToolCallContext


# Resolve the gate module directly so unit tests don't depend on the
# HookRegistry import path. Each test gets a fresh import to wipe the
# per-spawn state dict (mirroring the registry's per-spawn import scoping).
_GATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "beidou"
    / "skills"
    / "coding_v2"
    / "product_manager"
    / "gate.py"
)
assert _GATE_PATH.is_file(), f"gate.py missing at {_GATE_PATH}"


def _fresh_gate_module():
    """Import gate.py fresh so module-level state doesn't leak between tests."""
    spec = importlib.util.spec_from_file_location(
        f"pm_v2_gate_test_{id(object())}", _GATE_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(tool_name: str, tool_input: dict, agent_id: str = "pm-1", emitted=None):
    if emitted is None:
        emitted = []
    return ToolCallContext(
        agent_id=agent_id,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id="tool_use_xyz",
        emit=lambda n, p: emitted.append((n, p)),
    )


class TestPmV2Gate:
    def test_module_toml_present(self) -> None:
        """The pilot expects module.toml to ship alongside SKILL.md."""
        toml_path = _GATE_PATH.parent / "module.toml"
        assert toml_path.is_file(), f"module.toml missing at {toml_path}"

    def test_pass_through_for_unrelated_tools(self) -> None:
        gate = _fresh_gate_module()
        result = asyncio.run(
            gate.require_two_ask_user_rounds(
                _ctx("mcp__beidou__send_message", {"to": "x", "content": "hi"})
            )
        )
        assert isinstance(result, Pass)

    def test_ask_user_calls_pass_and_increment_counter(self) -> None:
        gate = _fresh_gate_module()
        for _ in range(3):
            r = asyncio.run(
                gate.require_two_ask_user_rounds(
                    _ctx("mcp__beidou__ask_user", {"questions": []})
                )
            )
            assert isinstance(r, Pass)
        assert gate._ASK_USER_COUNT["pm-1"] == 3

    def test_report_status_non_done_passes_regardless(self) -> None:
        gate = _fresh_gate_module()
        for state in ("working", "idle", "blocked"):
            r = asyncio.run(
                gate.require_two_ask_user_rounds(
                    _ctx("mcp__beidou__report_status", {"state": state, "detail": "x"})
                )
            )
            assert isinstance(r, Pass), f"state={state!r} should pass without ask_user count"

    def test_report_status_done_blocked_with_zero_rounds(self) -> None:
        gate = _fresh_gate_module()
        emitted: list = []
        result = asyncio.run(
            gate.require_two_ask_user_rounds(
                _ctx(
                    "mcp__beidou__report_status",
                    {"state": "done", "detail": "[REVIEW REQUIRED] ..."},
                    emitted=emitted,
                )
            )
        )
        assert isinstance(result, Block)
        assert "AT LEAST 2 ask_user rounds" in result.reason
        assert "observed 0" in result.reason
        assert "SKILL.md" in result.reason

        # Telemetry event recorded.
        assert any(name == "pm_v2_gate.blocked_report_done" for name, _ in emitted)
        payload = next(p for n, p in emitted if n == "pm_v2_gate.blocked_report_done")
        assert payload["observed_rounds"] == 0
        assert payload["required_rounds"] == 2

    def test_report_status_done_blocked_with_one_round(self) -> None:
        gate = _fresh_gate_module()
        asyncio.run(
            gate.require_two_ask_user_rounds(
                _ctx("mcp__beidou__ask_user", {"questions": []})
            )
        )
        result = asyncio.run(
            gate.require_two_ask_user_rounds(
                _ctx("mcp__beidou__report_status", {"state": "done", "detail": "x"})
            )
        )
        assert isinstance(result, Block)
        assert "observed 1" in result.reason

    def test_report_status_done_passes_with_two_rounds(self) -> None:
        gate = _fresh_gate_module()
        for _ in range(2):
            asyncio.run(
                gate.require_two_ask_user_rounds(
                    _ctx("mcp__beidou__ask_user", {"questions": []})
                )
            )
        result = asyncio.run(
            gate.require_two_ask_user_rounds(
                _ctx("mcp__beidou__report_status", {"state": "done", "detail": "x"})
            )
        )
        assert isinstance(result, Pass)

    def test_state_isolated_per_agent_id(self) -> None:
        """Two PM instances with different agent_ids each maintain their own count."""
        gate = _fresh_gate_module()
        # PM-1 runs round 1.
        asyncio.run(
            gate.require_two_ask_user_rounds(
                _ctx("mcp__beidou__ask_user", {"questions": []}, agent_id="pm-1")
            )
        )
        # PM-2 has done nothing yet.
        result = asyncio.run(
            gate.require_two_ask_user_rounds(
                _ctx(
                    "mcp__beidou__report_status",
                    {"state": "done", "detail": "x"},
                    agent_id="pm-2",
                )
            )
        )
        assert isinstance(result, Block)
        assert "observed 0" in result.reason
        # PM-1 still observed 1, not 0.
        assert gate._ASK_USER_COUNT["pm-1"] == 1


class TestPmV2HookRegistryEndToEnd:
    """Exercises HookRegistry on the actual bundled skill to prove the loader works."""

    def test_registry_loads_module_toml(self) -> None:
        from beidou.agent.hooks import HookRegistry

        skill_dir = _GATE_PATH.parent

        class DummyOrch:
            def emit_event(self, name, payload):
                pass

        registry = HookRegistry(
            skill_dir=skill_dir,
            orch=DummyOrch(),
            caller_id="pm-test",
            leader_id="leader-1",
        )

        # validate_tool_call should be wired up.
        assert "validate_tool_call" in registry._handlers
        assert registry._handlers["validate_tool_call"]["mode"] == "all_must_pass"
        assert len(registry._handlers["validate_tool_call"]["handlers"]) == 1

    def test_registry_blocks_report_done_without_rounds(self) -> None:
        from beidou.agent.hooks import HookRegistry

        skill_dir = _GATE_PATH.parent

        events: list = []

        class CapturingOrch:
            def emit_event(self, name, payload):
                events.append((name, payload))

        registry = HookRegistry(
            skill_dir=skill_dir,
            orch=CapturingOrch(),
            caller_id="pm-e2e",
            leader_id="leader-1",
        )

        ctx = ToolCallContext(
            agent_id="pm-e2e",
            tool_name="mcp__beidou__report_status",
            tool_input={"state": "done", "detail": "[REVIEW REQUIRED] ..."},
            tool_use_id="t1",
            emit=lambda n, p: events.append((n, p)),
        )

        result = asyncio.run(registry.run_gate_hook("validate_tool_call", ctx))
        assert isinstance(result, Block)
        assert "ask_user rounds" in result.reason
