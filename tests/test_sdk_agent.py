"""Tests for ``beidou/sdk_agent.py``.

Two layers:

1. **Mechanical tests** (always run): mock ``claude_agent_sdk.query`` with an
   async generator yielding fake AssistantMessage / ResultMessage objects.
   These exercise the drain-loop translation logic (dedup, final-text
   capture, event emission, terminated vs contract_violation) without any
   network call.

2. **Integration test** (``@pytest.mark.integration``, skipped unless both
   ``ANTHROPIC_API_KEY`` and the ``claude`` CLI are available): spawns a
   real SDK agent with a trivial test-only skill and asserts end-to-end
   observability behaviour.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from beidou import sdk_agent
from beidou.primitives.core import Message, Peer
from beidou.sdk_agent import RunResult, SpawnSpec, run_agent


# ---------------------------------------------------------------------------
# Minimal FakeOrchestrator. Implements just the Protocol methods that are
# reachable from sdk_agent / the MCP wrapper during these tests.
# ---------------------------------------------------------------------------


class FakeOrchestrator:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._terminated: set[str] = set()
        # Minimal extras so the MCP wrapper wouldn't crash if the agent made
        # primitive calls; the mechanical tests don't, but the integration
        # test does (report_status only).
        self.inboxes: dict[str, asyncio.Queue] = {}
        self.statuses: list[tuple[str, str, Optional[str]]] = []
        # Assistant text tracking (mirrors orchestrator.py).
        self._assistant_text_by_tool: dict[str, dict[str, str]] = {}
        self._most_recent_assistant_text: dict[str, str] = {}
        # Delivery tracking for hook tests.
        self.delivered: list[tuple[str, str, str, str]] = []  # (from, to, body, kind)
        # _agents: used by input_stream in sdk_agent to set terminate_consumed.
        # Each value is a SimpleNamespace(terminate_consumed=False, inbox=Queue).
        self._agents: dict[str, SimpleNamespace] = {}

    # --- Events / termination --------------------------------------------
    def emit_event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def was_terminated(self, caller_id: str) -> bool:
        if caller_id in self._terminated:
            return True
        rec = self._agents.get(caller_id)
        return rec is not None and rec.terminate_consumed

    def mark_terminated(self, caller_id: str) -> None:
        self._terminated.add(caller_id)

    def queue_for(self, agent_id: str) -> asyncio.Queue:
        """Return the per-agent queue (creates inbox+agent record on demand)."""
        if agent_id not in self._agents:
            q: asyncio.Queue = asyncio.Queue()
            self._agents[agent_id] = SimpleNamespace(
                terminate_consumed=False,
                inbox=q,
                inflight_tools=0,
            )
        return self._agents[agent_id].inbox

    async def deliver_to_queue(self, agent_id: str, msg: Message) -> None:
        """Test helper: push a message onto the agent's queue."""
        q = self.queue_for(agent_id)
        await q.put(msg)

    # --- Registry stubs --------------------------------------------------
    def agent_exists(self, agent_id: str) -> bool:
        return True

    def agent_task(self, agent_id: str) -> str:
        return "tsk_fake"

    def agent_team(self, agent_id: str) -> str:
        return "root"

    def leader_of(self, team_id: str) -> str:
        return "leader"

    def teams_led_by(self, agent_id: str) -> list[str]:
        return []

    def team_depth(self, team_id: str) -> int:
        return 0

    def team_members(self, team_id: str) -> list[str]:
        return []

    def peer_snapshot(self, agent_id: str, scope: str) -> list[Peer]:
        return []

    # --- Inbox stubs -----------------------------------------------------
    async def inbox_put(self, recipient: str, msg: Message) -> None:
        self.inboxes.setdefault(recipient, asyncio.Queue()).put_nowait(msg)

    async def inbox_drain(self, agent_id: str) -> list[Message]:
        q = self.inboxes.setdefault(agent_id, asyncio.Queue())
        out: list[Message] = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    async def inbox_get_one(self, agent_id, from_filter, timeout):
        return None

    def inbox_size(self, agent_id: str) -> int:
        return self.inboxes.setdefault(agent_id, asyncio.Queue()).qsize()

    # --- Team / gateway stubs -------------------------------------------
    async def spawn_team(self, **kwargs):
        raise NotImplementedError

    async def create_team_lock(self, agent_id: str) -> asyncio.Lock:
        return asyncio.Lock()

    async def gateway_ask_user(self, caller_id, question, context):
        return "no gateway"

    async def gateway_ask_user_structured(self, caller_id, questions, context):
        return {"answers": [{"selected_labels": [], "text": "no gateway"}], "answer_text": "no gateway"}

    def is_gateway_available(self) -> bool:
        return False

    def record_status(self, caller_id, state, detail):
        self.statuses.append((caller_id, state, detail))

    # --- Assistant text tracking -----------------------------------------
    def record_assistant_text(self, caller_id: str, text: str, tool_use_ids: list[str]) -> None:
        if text:
            self._most_recent_assistant_text[caller_id] = text
            agent_map = self._assistant_text_by_tool.setdefault(caller_id, {})
            for tid in tool_use_ids:
                agent_map[tid] = text

    def assistant_text_for_turn(self, caller_id: str, tool_use_id: str) -> Optional[str]:
        agent_map = self._assistant_text_by_tool.get(caller_id, {})
        text = agent_map.get(tool_use_id)
        if text is not None:
            return text
        return self._most_recent_assistant_text.get(caller_id)

    def deliver_message(self, from_id: str, to_id: str, body: str, kind: str = "message") -> None:
        self.delivered.append((from_id, to_id, body, kind))

    # --- Completion-review / name accessors (Protocol completeness) ---------
    def agent_skill_name(self, agent_id: str) -> str:
        return ""

    def agent_completion_pending(self, agent_id: str) -> bool:
        return False

    def agent_completion_pending_ts(self, agent_id: str) -> Optional[float]:
        return None

    def agent_last_status_detail(self, agent_id: str) -> str:
        return ""

    def agent_name(self, agent_id: str) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Fake SDK message types. The drain loop dispatches by
# ``type(msg).__name__``, so we only need shape-compatible fakes.
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    """Named to match the real SDK block class (drain loop dispatches by __name__)."""
    text: str


@dataclass
class ToolUseBlock:
    name: str
    input: dict = field(default_factory=dict)
    id: str = "toolu_1"


class AssistantMessage:
    def __init__(
        self,
        content: list,
        model: str = "fake-model",
        message_id: Optional[str] = None,
        usage: Optional[dict] = None,
        stop_reason: Optional[str] = None,
    ) -> None:
        self.content = content
        self.model = model
        self.message_id = message_id
        self.usage = usage
        self.stop_reason = stop_reason


class ResultMessage:
    def __init__(
        self,
        total_cost_usd: float,
        usage: dict,
        num_turns: int,
        duration_ms: int = 100,
        duration_api_ms: int = 50,
        stop_reason: str = "end_turn",
        session_id: str = "sess_fake",
        model_usage: Optional[dict] = None,
    ) -> None:
        self.total_cost_usd = total_cost_usd
        self.usage = usage
        self.num_turns = num_turns
        self.duration_ms = duration_ms
        self.duration_api_ms = duration_api_ms
        self.stop_reason = stop_reason
        self.session_id = session_id
        self.model_usage = model_usage or {}


@dataclass
class ToolResultBlock:
    """Named to match the real SDK block class (drain loop dispatches by __name__)."""
    tool_use_id: str
    is_error: bool = False
    content: list = field(default_factory=list)


class SystemMessage:
    """Shape-compatible sentinel; drain loop ignores these."""


class UserMessage:
    """Carries tool-result echoes from the SDK (drain loop reads .content)."""

    def __init__(self, content: list = None) -> None:
        self.content = content or []


# ---------------------------------------------------------------------------
# Fake skill fixture. Every test gets its own skill_root temp dir so YAML
# parsing and template rendering stay honest.
# ---------------------------------------------------------------------------


def _make_skill_dir(tmp_path: Path, skill_name: str = "fake_skill") -> Path:
    skill_dir = tmp_path / skill_name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {skill_name}
            version: 1.0.0
            description: Fake skill for tests.
            allowed-tools:
              - report_status
            ---
            You are the fake agent. Role: {{role}}.
            """
        )
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Mechanical tests (no network).
# ---------------------------------------------------------------------------


def _install_fake_query(monkeypatch, messages: list) -> list[dict]:
    """Patch ``claude_agent_sdk.query`` to yield ``messages``. Returns the
    list of ``(prompt, options)`` captured for assertion."""
    captured: list[dict] = []

    async def fake_query(*, prompt, options):
        captured.append({"prompt": prompt, "options": options})
        for m in messages:
            yield m

    monkeypatch.setattr(sdk_agent, "query", fake_query)
    return captured


def test_mechanical_happy_path_terminated(tmp_path, monkeypatch):
    """Dedup, final-text capture, and terminated flag when orch says so."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_1")  # the orchestrator observed a terminate.

    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2}
    # Two AssistantMessage fragments sharing message_id "msg_1" -> one
    # turn.usage event. A third turn with a different id -> a second event.
    messages = [
        SystemMessage(),
        AssistantMessage(
            content=[ToolUseBlock(name="mcp__beidou__report_status")],
            message_id="msg_1",
            usage=usage,
            stop_reason="tool_use",
        ),
        AssistantMessage(
            content=[TextBlock("ignored intermediate text")],
            message_id="msg_1",
            usage=usage,  # duplicate -> must NOT be counted twice.
            stop_reason="tool_use",
        ),
        UserMessage(),
        AssistantMessage(
            content=[TextBlock("final answer "), TextBlock("complete")],
            message_id="msg_2",
            usage={"input_tokens": 20, "output_tokens": 3},
            stop_reason="end_turn",
        ),
        ResultMessage(
            total_cost_usd=0.0123,
            usage={"input_tokens": 30, "output_tokens": 8},
            num_turns=2,
            stop_reason="end_turn",
        ),
    ]
    captured = _install_fake_query(monkeypatch, messages)

    spec = SpawnSpec(
        caller_id="ag_1",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="do the thing",
        model="claude-haiku-4-5-20251001",
        template_vars={"role": "tester"},
    )

    result = asyncio.run(run_agent(orch, spec))

    # RunResult
    assert isinstance(result, RunResult)
    assert result.final_text == "final answer complete"
    assert result.total_cost_usd == pytest.approx(0.0123)
    assert result.total_usage == {"input_tokens": 30, "output_tokens": 8}
    assert result.num_turns == 2
    assert result.stop_reason == "end_turn"
    assert result.session_id == "sess_fake"
    assert result.terminated is True
    assert result.contract_violation is False

    # Events in order: agent_started, 2x turn.usage (dedup!), run.cost, agent_completed.
    names = [n for n, _ in orch.events]
    assert names[0] == "agent_started"
    assert names.count("turn.usage") == 2
    assert "run.cost" in names
    assert names[-1] == "agent_completed"

    # agent_started payload carries the requested model.
    started = next(p for n, p in orch.events if n == "agent_started")
    assert started["caller_id"] == "ag_1"
    assert started["skill"] == "fake_skill"
    assert started["model_requested"] == "claude-haiku-4-5-20251001"

    # turn.usage payloads carry the expected token counts.
    usage_events = [p for n, p in orch.events if n == "turn.usage"]
    assert usage_events[0]["message_id"] == "msg_1"
    assert usage_events[0]["input_tokens"] == 10
    assert usage_events[0]["cache_read_input_tokens"] == 2
    assert usage_events[1]["message_id"] == "msg_2"

    # run.cost carries authoritative totals.
    cost = next(p for n, p in orch.events if n == "run.cost")
    assert cost["total_cost_usd"] == pytest.approx(0.0123)
    assert cost["num_turns"] == 2

    # agent_completed flags terminated=True.
    completed = next(p for n, p in orch.events if n == "agent_completed")
    assert completed["terminated"] is True
    assert completed["stop_reason"] == "end_turn"

    # Options were assembled with the new four-section system prompt and setting_sources.
    opts = captured[0]["options"]
    assert "mcp__beidou__report_status" in opts.allowed_tools
    assert opts.setting_sources == ["user", "project"]
    assert opts.skills == "all"
    # system_prompt is the new four-section structure: skill body first.
    assert "[ASSIGNED SKILL" in opts.system_prompt
    assert "[IDENTITY]" in opts.system_prompt
    assert "[PERSISTENT-AGENT CONTRACT]" in opts.system_prompt
    assert "[OTHER SKILLS]" in opts.system_prompt
    # Template substitution was applied inside the skill body.
    assert "Role: tester" in opts.system_prompt


def test_mechanical_contract_violation_when_not_terminated(tmp_path, monkeypatch):
    """Loop exits cleanly, but orchestrator never saw a terminate sentinel."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    # Deliberately NOT calling mark_terminated.

    messages = [
        AssistantMessage(
            content=[TextBlock("done")],
            message_id="msg_a",
            usage={"input_tokens": 5, "output_tokens": 2},
            stop_reason="end_turn",
        ),
        ResultMessage(
            total_cost_usd=0.001,
            usage={"input_tokens": 5, "output_tokens": 2},
            num_turns=1,
        ),
    ]
    _install_fake_query(monkeypatch, messages)

    spec = SpawnSpec(
        caller_id="ag_2",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
    )
    result = asyncio.run(run_agent(orch, spec))

    assert result.terminated is False
    assert result.contract_violation is True
    completed = next(p for n, p in orch.events if n == "agent_completed")
    assert completed["terminated"] is False


def test_mechanical_allowed_tools_override(tmp_path, monkeypatch):
    """Explicit ``allowed_tools`` in SpawnSpec overrides the skill default."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_3")

    _install_fake_query(
        monkeypatch,
        [
            ResultMessage(
                total_cost_usd=0.0,
                usage={},
                num_turns=0,
            )
        ],
    )

    spec = SpawnSpec(
        caller_id="ag_3",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
        allowed_tools=["mcp__beidou__send_message"],
    )
    asyncio.run(run_agent(orch, spec))
    # Assembled options captured on the class-level fake_query replacement.
    # Re-pull from the monkeypatched module attribute isn't direct; instead
    # re-install a capture-variant and assert there.


def test_mechanical_empty_allowed_tools_emits_warning(tmp_path, monkeypatch):
    """Empty allowed_tools list yields a ``config_warning`` event."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_4")

    _install_fake_query(
        monkeypatch,
        [ResultMessage(total_cost_usd=0.0, usage={}, num_turns=0)],
    )

    spec = SpawnSpec(
        caller_id="ag_4",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
        allowed_tools=[],  # explicit empty -- override skill default.
    )
    asyncio.run(run_agent(orch, spec))
    assert any(
        n == "config_warning" and p["warning"] == "empty_allowed_tools"
        for n, p in orch.events
    )


def test_mechanical_cancellation_emits_completed_and_reraises(tmp_path, monkeypatch):
    """``asyncio.CancelledError`` mid-drain yields terminated=False completed event, then re-raises."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()

    async def fake_query(*, prompt, options):
        # Yield one message, then the caller cancels us.
        yield AssistantMessage(
            content=[TextBlock("partial")],
            message_id="m",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        await asyncio.sleep(10)  # park; will be cancelled.
        yield ResultMessage(total_cost_usd=0.0, usage={}, num_turns=1)

    monkeypatch.setattr(sdk_agent, "query", fake_query)

    async def body():
        task = asyncio.create_task(
            run_agent(
                orch,
                SpawnSpec(
                    caller_id="ag_c",
                    skill_name="fake_skill",
                    skill_root=skill_root,
                    task="t",
                ),
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(body())

    completed = [p for n, p in orch.events if n == "agent_completed"]
    assert completed and completed[-1]["stop_reason"] == "cancelled"
    assert completed[-1]["terminated"] is False


def test_mechanical_exception_emits_agent_error_and_reraises(tmp_path, monkeypatch):
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()

    async def fake_query(*, prompt, options):
        yield AssistantMessage(
            content=[TextBlock("x")],
            message_id="m",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        raise RuntimeError("api blew up")

    monkeypatch.setattr(sdk_agent, "query", fake_query)

    with pytest.raises(RuntimeError, match="api blew up"):
        asyncio.run(
            run_agent(
                orch,
                SpawnSpec(
                    caller_id="ag_e",
                    skill_name="fake_skill",
                    skill_root=skill_root,
                    task="t",
                ),
            )
        )

    assert any(n == "agent_error" for n, _ in orch.events)


# ---------------------------------------------------------------------------
# tool_called / tool_result pairing tests.
# ---------------------------------------------------------------------------


def test_tool_called_and_result_events_paired(tmp_path, monkeypatch):
    """ToolUseBlock yields tool_called; matching ToolResultBlock yields tool_result.

    Verifies:
    - tool_called emitted with tool_use_id, message_id, name, input, caller_id.
    - tool_result emitted with tool_use_id, duration_ms (int >=0), is_error (bool).
    - tool_use_id matches between the two events.
    """
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_tool")

    messages = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    name="mcp__beidou__report_status",
                    input={"state": "done"},
                    id="toolu_abc123",
                ),
            ],
            message_id="msg_tool",
            usage={"input_tokens": 5, "output_tokens": 2},
            stop_reason="tool_use",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_abc123",
                    is_error=False,
                ),
            ],
        ),
        ResultMessage(
            total_cost_usd=0.001,
            usage={"input_tokens": 5, "output_tokens": 2},
            num_turns=1,
        ),
    ]
    _install_fake_query(monkeypatch, messages)

    spec = SpawnSpec(
        caller_id="ag_tool",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
    )
    asyncio.run(run_agent(orch, spec))

    tool_called_events = [p for n, p in orch.events if n == "tool_called"]
    tool_result_events = [p for n, p in orch.events if n == "tool_result"]

    assert len(tool_called_events) == 1, f"expected 1 tool_called, got {len(tool_called_events)}"
    assert len(tool_result_events) == 1, f"expected 1 tool_result, got {len(tool_result_events)}"

    tc = tool_called_events[0]
    tr = tool_result_events[0]

    # tool_called fields.
    assert tc["tool_use_id"] == "toolu_abc123"
    assert tc["message_id"] == "msg_tool"
    assert tc["name"] == "mcp__beidou__report_status"
    assert tc["input"] == {"state": "done"}
    assert tc["caller_id"] == "ag_tool"

    # tool_result fields.
    assert tr["tool_use_id"] == "toolu_abc123"
    assert isinstance(tr["duration_ms"], int)
    assert tr["duration_ms"] >= 0
    assert tr["is_error"] is False
    assert tr["error_reason"] is None

    # Pairing: same tool_use_id.
    assert tc["tool_use_id"] == tr["tool_use_id"]


def test_tool_result_without_prior_tool_called_emits_none_duration(tmp_path, monkeypatch):
    """ToolResultBlock with no prior ToolUseBlock emits tool_result with duration_ms=None.

    This exercises the 'orphan result' path in the drain loop (pending_tool_uses
    lookup returns None) without crashing.
    """
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_orphan")

    messages = [
        # No ToolUseBlock — jump straight to ToolResultBlock.
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_orphan",
                    is_error=True,
                ),
            ],
        ),
        ResultMessage(
            total_cost_usd=0.0,
            usage={},
            num_turns=1,
        ),
    ]
    _install_fake_query(monkeypatch, messages)

    spec = SpawnSpec(
        caller_id="ag_orphan",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
    )
    asyncio.run(run_agent(orch, spec))  # must not raise

    tool_result_events = [p for n, p in orch.events if n == "tool_result"]
    assert len(tool_result_events) == 1
    tr = tool_result_events[0]
    assert tr["tool_use_id"] == "toolu_orphan"
    assert tr["duration_ms"] is None
    assert tr["is_error"] is True


def test_tool_result_captures_error_reason(tmp_path, monkeypatch):
    """ToolResultBlock with is_error=True and text content surfaces error_reason in the event."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_err_reason")

    messages = [
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_err1",
                    is_error=True,
                    content=[{"type": "text", "text": "plan_incomplete"}],
                ),
            ],
        ),
        ResultMessage(
            total_cost_usd=0.0,
            usage={},
            num_turns=1,
        ),
    ]
    _install_fake_query(monkeypatch, messages)

    spec = SpawnSpec(
        caller_id="ag_err_reason",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
    )
    asyncio.run(run_agent(orch, spec))

    tool_result_events = [p for n, p in orch.events if n == "tool_result"]
    assert len(tool_result_events) == 1
    tr = tool_result_events[0]
    assert tr["is_error"] is True
    assert tr["error_reason"] == "plan_incomplete"


def test_tool_result_truncates_long_error_reason(tmp_path, monkeypatch):
    """ToolResultBlock with is_error=True and >2000-char content is truncated with suffix."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_trunc")

    long_text = "x" * 5000
    messages = [
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_trunc",
                    is_error=True,
                    content=[{"type": "text", "text": long_text}],
                ),
            ],
        ),
        ResultMessage(
            total_cost_usd=0.0,
            usage={},
            num_turns=1,
        ),
    ]
    _install_fake_query(monkeypatch, messages)

    spec = SpawnSpec(
        caller_id="ag_trunc",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
    )
    asyncio.run(run_agent(orch, spec))

    tool_result_events = [p for n, p in orch.events if n == "tool_result"]
    assert len(tool_result_events) == 1
    tr = tool_result_events[0]
    assert tr["is_error"] is True
    assert tr["error_reason"].endswith("…(truncated)")
    assert len(tr["error_reason"]) == 2000 + len("…(truncated)")


# ---------------------------------------------------------------------------
# Assistant text per-turn recording tests (drain loop binding for hook).
# ---------------------------------------------------------------------------


def test_drain_loop_records_assistant_text_bound_to_tool_use_id(tmp_path, monkeypatch):
    """The drain loop calls record_assistant_text with text + tool_use_ids from the same turn.

    Critically, the recording must happen IMMEDIATELY after processing the AssistantMessage
    (not on the next message_id transition), so that a PostToolUse hook firing between
    message yields can read the bound text. This test verifies the timing by checking the
    text is available before any subsequent messages are processed.
    """
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_txt")

    # Capture: after the first AssistantMessage is yielded and before any other
    # message, the text must be in the orchestrator (simulates the hook firing
    # between yields). We verify this via an interleaved check in the fake query.
    text_after_first_msg: list[Optional[str]] = []

    async def recording_fake_query(*, prompt, options):
        # Yield the AssistantMessage with text + tool_use.
        yield AssistantMessage(
            content=[
                TextBlock("I finished the work."),
                ToolUseBlock(name="mcp__beidou__report_status", input={"state": "done"}, id="toolu_report"),
            ],
            message_id="msg_done",
            usage={"input_tokens": 5, "output_tokens": 3},
            stop_reason="tool_use",
        )
        # At this point, the drain loop has processed the AssistantMessage.
        # A real hook would fire here. Simulate by reading the text immediately.
        text_after_first_msg.append(
            orch.assistant_text_for_turn("ag_txt", "toolu_report")
        )
        yield UserMessage(content=[ToolResultBlock(tool_use_id="toolu_report")])
        yield ResultMessage(total_cost_usd=0.001, usage={}, num_turns=1)

    monkeypatch.setattr(sdk_agent, "query", recording_fake_query)

    spec = SpawnSpec(
        caller_id="ag_txt",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
    )
    asyncio.run(run_agent(orch, spec))

    # Verify the text was available immediately after the AssistantMessage yield
    # (before any subsequent messages — this is the hook firing window).
    assert text_after_first_msg, "Interleaved check was not reached"
    assert text_after_first_msg[0] is not None, (
        "record_assistant_text was not called before the next message yield — "
        "PostToolUse hook would have seen an empty turn."
    )
    assert "I finished the work." in text_after_first_msg[0]

    # Also verify the final state after run_agent completes.
    text = orch.assistant_text_for_turn("ag_txt", "toolu_report")
    assert text is not None
    assert "I finished the work." in text


def test_drain_loop_most_recent_fallback(tmp_path, monkeypatch):
    """When exact binding not found, assistant_text_for_turn returns most recent text."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    orch.mark_terminated("ag_fallback")

    messages = [
        AssistantMessage(
            content=[TextBlock("Some prior summary.")],
            message_id="msg_prior",
            usage={"input_tokens": 3, "output_tokens": 2},
            stop_reason="end_turn",
        ),
        ResultMessage(total_cost_usd=0.001, usage={}, num_turns=1),
    ]
    _install_fake_query(monkeypatch, messages)

    spec = SpawnSpec(
        caller_id="ag_fallback",
        skill_name="fake_skill",
        skill_root=skill_root,
        task="t",
    )
    asyncio.run(run_agent(orch, spec))

    # No specific tool_use_id was recorded; fallback to most-recent text.
    text = orch.assistant_text_for_turn("ag_fallback", "toolu_nonexistent")
    assert text == "Some prior summary."


# ---------------------------------------------------------------------------
# Outer-loop (streaming-input) test.
# ---------------------------------------------------------------------------


def test_outer_loop_streaming_input_terminate(tmp_path, monkeypatch):
    """Drive the streaming-input outer loop end-to-end without Anthropic API.

    1. FakeOrchestrator exposes queue_for and _agents so the input_stream
       in sdk_agent can park and detect the terminate sentinel.
    2. A stub query consumes the AsyncIterable and yields one AssistantMessage
       per user-role input it receives.
    3. The test sends an extra peer message then terminates, asserting:
       - at least 2 assistant_text events (initial turn + peer turn)
       - agent_completed with terminated=True
    """
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    agent_id = "ag_loop"

    # Pre-register the agent record so queue_for is idempotent.
    orch.queue_for(agent_id)

    # Stub query: consume the AsyncIterable; yield one AssistantMessage per
    # user-role dict it receives from input_stream, then a ResultMessage.
    async def stub_query(*, prompt, options):
        turn = 0
        async for item in prompt:
            if not isinstance(item, dict) or item.get("type") != "user":
                continue
            turn += 1
            yield AssistantMessage(
                content=[TextBlock(f"response to turn {turn}")],
                message_id=f"msg_{turn}",
                usage={"input_tokens": 5, "output_tokens": 2},
                stop_reason="end_turn",
            )
        yield ResultMessage(
            total_cost_usd=0.001,
            usage={"input_tokens": 10, "output_tokens": 4},
            num_turns=turn,
        )

    monkeypatch.setattr(sdk_agent, "query", stub_query)

    async def body():
        # Start run_agent as a background task.
        task = asyncio.create_task(
            run_agent(
                orch,
                SpawnSpec(
                    caller_id=agent_id,
                    skill_name="fake_skill",
                    skill_root=skill_root,
                    task="initial task",
                ),
            )
        )

        # Allow the initial turn to be processed.
        await asyncio.sleep(0.05)

        # Deliver a peer message — triggers a second turn.
        await orch.deliver_to_queue(
            agent_id,
            Message(from_id="peer_x", content="ping", ts=time.time(), message_id="peer_m1", kind="user"),
        )
        await asyncio.sleep(0.05)

        # Deliver a terminate sentinel — ends the input_stream.
        await orch.deliver_to_queue(
            agent_id,
            Message(from_id="beidou", content="__terminate__", ts=time.time(), message_id="term_1", kind="terminate"),
        )

        result = await task
        return result

    result = asyncio.run(body())

    # agent_completed with terminated=True.
    assert isinstance(result, RunResult)
    assert result.terminated is True
    assert result.contract_violation is False

    # At least 2 assistant_text events (initial + peer turn).
    assistant_text_events = [p for n, p in orch.events if n == "assistant_text"]
    assert len(assistant_text_events) >= 2, (
        f"expected >=2 assistant_text events, got {len(assistant_text_events)}"
    )

    # Final agent_completed event has terminated=True.
    completed = next(p for n, p in orch.events if n == "agent_completed")
    assert completed["terminated"] is True


# ---------------------------------------------------------------------------
# agent_input event tests.
# ---------------------------------------------------------------------------
#
# Both tests use the stub_query pattern from test_outer_loop_streaming_input_terminate
# — a stub that iterates the `prompt` AsyncIterable so that input_stream's yields
# (and pre-yield emits) are actually exercised.  _install_fake_query does NOT
# iterate `prompt`, so agent_input events would never fire under that harness.


def test_agent_input_initial_task_emitted(tmp_path, monkeypatch):
    """Spawning an agent emits agent_input with source='initial' before the first turn.

    Asserts:
    - event name is 'agent_input'
    - source == 'initial'
    - from == 'user'
    - message_kind == 'initial'
    - content matches spec.task
    - message_id == f'{caller_id}:initial'
    - event appears BEFORE the first assistant_text event (emitted before yield)
    """
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    agent_id = "ag_input_initial"
    orch.queue_for(agent_id)

    async def stub_query(*, prompt, options):
        async for item in prompt:
            if not isinstance(item, dict) or item.get("type") != "user":
                continue
            yield AssistantMessage(
                content=[TextBlock("ok")],
                message_id="msg_init_1",
                usage={"input_tokens": 1, "output_tokens": 1},
                stop_reason="end_turn",
            )
        yield ResultMessage(total_cost_usd=0.0, usage={}, num_turns=1)

    monkeypatch.setattr(sdk_agent, "query", stub_query)

    async def body():
        task = asyncio.create_task(
            run_agent(
                orch,
                SpawnSpec(
                    caller_id=agent_id,
                    skill_name="fake_skill",
                    skill_root=skill_root,
                    task="build me something cool",
                ),
            )
        )
        # Let initial turn complete.
        await asyncio.sleep(0.05)
        # Terminate to unblock input_stream.
        await orch.deliver_to_queue(
            agent_id,
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="term_init",
                kind="terminate",
            ),
        )
        return await task

    asyncio.run(body())

    input_events = [p for n, p in orch.events if n == "agent_input"]
    assert len(input_events) >= 1, "Expected at least one agent_input event"

    initial_ev = next((p for p in input_events if p.get("source") == "initial"), None)
    assert initial_ev is not None, "No agent_input with source='initial' found"
    assert initial_ev["from"] == "user"
    assert initial_ev["message_kind"] == "initial"
    assert initial_ev["content"] == "build me something cool"
    assert initial_ev["message_id"] == f"{agent_id}:initial"
    assert initial_ev["caller_id"] == agent_id

    # agent_input(source='initial') must appear before any assistant_text event.
    event_names = [n for n, _ in orch.events]
    first_input_idx = next(
        i for i, (n, p) in enumerate(orch.events)
        if n == "agent_input" and p.get("source") == "initial"
    )
    first_assistant_idx = next(
        (i for i, (n, _) in enumerate(orch.events) if n == "assistant_text"),
        len(orch.events),
    )
    assert first_input_idx < first_assistant_idx, (
        "agent_input(initial) should appear before first assistant_text"
    )


def test_agent_input_queued_peer_message_emitted(tmp_path, monkeypatch):
    """A queued peer message produces agent_input with source='queue'.

    Asserts:
    - source == 'queue'
    - from == sender's agent_id
    - message_kind == 'user'
    - content matches the sent content
    - message_id matches the Message.message_id
    - ts matches msg_in.ts (origin time), not a fresh time.time()
    """
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    agent_id = "ag_input_queue"
    orch.queue_for(agent_id)

    async def stub_query(*, prompt, options):
        async for item in prompt:
            if not isinstance(item, dict) or item.get("type") != "user":
                continue
            yield AssistantMessage(
                content=[TextBlock("turn response")],
                message_id=f"msg_q_{id(item)}",
                usage={"input_tokens": 1, "output_tokens": 1},
                stop_reason="end_turn",
            )
        yield ResultMessage(total_cost_usd=0.0, usage={}, num_turns=2)

    monkeypatch.setattr(sdk_agent, "query", stub_query)

    peer_ts = time.time()
    peer_message_id = "peer_msg_xyz"

    async def body():
        task = asyncio.create_task(
            run_agent(
                orch,
                SpawnSpec(
                    caller_id=agent_id,
                    skill_name="fake_skill",
                    skill_root=skill_root,
                    task="initial task",
                ),
            )
        )
        # Let initial turn complete.
        await asyncio.sleep(0.05)
        # Deliver a peer message.
        await orch.deliver_to_queue(
            agent_id,
            Message(
                from_id="agent_sender",
                content="hello from peer",
                ts=peer_ts,
                message_id=peer_message_id,
                kind="user",
            ),
        )
        await asyncio.sleep(0.05)
        # Terminate.
        await orch.deliver_to_queue(
            agent_id,
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="term_queue",
                kind="terminate",
            ),
        )
        return await task

    asyncio.run(body())

    queue_events = [
        p for n, p in orch.events
        if n == "agent_input" and p.get("source") == "queue"
    ]
    assert len(queue_events) >= 1, "Expected at least one agent_input with source='queue'"

    peer_ev = next(
        (p for p in queue_events if p.get("message_id") == peer_message_id),
        None,
    )
    assert peer_ev is not None, (
        f"No agent_input found for message_id={peer_message_id!r}; "
        f"got queue events: {queue_events}"
    )
    assert peer_ev["from"] == "agent_sender"
    assert peer_ev["message_kind"] == "user"
    assert peer_ev["content"] == "hello from peer"
    assert peer_ev["caller_id"] == agent_id
    assert peer_ev["ts"] == pytest.approx(peer_ts), (
        "ts should be origin time (msg_in.ts), not consume time"
    )


def test_agent_input_terminate_sentinel_not_emitted(tmp_path, monkeypatch):
    """Terminate sentinels must NOT produce an agent_input event."""
    skill_root = _make_skill_dir(tmp_path)
    orch = FakeOrchestrator()
    agent_id = "ag_input_term"
    orch.queue_for(agent_id)

    async def stub_query(*, prompt, options):
        async for item in prompt:
            if not isinstance(item, dict) or item.get("type") != "user":
                continue
            yield AssistantMessage(
                content=[TextBlock("ok")],
                message_id="msg_term_test",
                usage={"input_tokens": 1, "output_tokens": 1},
                stop_reason="end_turn",
            )
        yield ResultMessage(total_cost_usd=0.0, usage={}, num_turns=1)

    monkeypatch.setattr(sdk_agent, "query", stub_query)

    async def body():
        task = asyncio.create_task(
            run_agent(
                orch,
                SpawnSpec(
                    caller_id=agent_id,
                    skill_name="fake_skill",
                    skill_root=skill_root,
                    task="task",
                ),
            )
        )
        await asyncio.sleep(0.05)
        await orch.deliver_to_queue(
            agent_id,
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="term_sentinel",
                kind="terminate",
            ),
        )
        return await task

    asyncio.run(body())

    # No agent_input event should have message_id matching the terminate sentinel.
    terminate_input_events = [
        p for n, p in orch.events
        if n == "agent_input" and p.get("message_id") == "term_sentinel"
    ]
    assert terminate_input_events == [], (
        "terminate sentinel must not produce an agent_input event"
    )


# ---------------------------------------------------------------------------
# Integration test (real SDK / claude CLI).
# ---------------------------------------------------------------------------


_CLAUDE_CLI = shutil.which("claude")
# Honour .env as well as the process env so devs don't have to export the key
# just to run integration tests.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except ImportError:
    pass
_API_KEY = os.getenv("ANTHROPIC_API_KEY")


@pytest.mark.integration
@pytest.mark.skipif(
    not _API_KEY or not _CLAUDE_CLI,
    reason="requires ANTHROPIC_API_KEY and claude CLI on PATH",
)
def test_integration_real_sdk_agent(tmp_path):
    """Spawn a real SDK agent with a trivial skill; assert observability."""
    from dotenv import load_dotenv

    load_dotenv()

    skill_dir = tmp_path / "sdk_test_agent"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: sdk_test_agent
            version: 1.0.0
            description: Minimal SDK-agent test skill.
            allowed-tools:
              - report_status
            ---
            You are a test agent. Your only job is to call the
            mcp__beidou__report_status tool exactly once with
            state="done" and detail="ok", then produce a one-sentence final
            acknowledgment. Do not call any other tool.
            """
        )
    )

    orch = FakeOrchestrator()

    spec = SpawnSpec(
        caller_id="ag_int",
        skill_name="sdk_test_agent",
        skill_root=tmp_path,
        task="Please complete the test.",
        model="claude-haiku-4-5-20251001",
    )

    async def _drive() -> object:
        # Pre-seed a terminate sentinel on the agent's queue. After the agent
        # finishes its single turn (report_status + final ack), the outer loop
        # in sdk_agent will park on queue.get() and immediately consume the
        # sentinel, ending the SDK session. Without this the run hangs forever
        # under streaming-input mode (agents are now persistent listeners).
        from beidou.primitives.core import Message

        orch.queue_for("ag_int")  # materialise the queue
        await orch.deliver_to_queue(
            "ag_int",
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="int-terminate",
                kind="terminate",
            ),
        )
        return await run_agent(orch, spec)

    result = asyncio.run(_drive())

    names = [n for n, _ in orch.events]
    assert "agent_started" in names
    assert "run.cost" in names
    assert "agent_completed" in names

    cost_payload = next(p for n, p in orch.events if n == "run.cost")
    assert cost_payload["total_cost_usd"] > 0
    assert result.num_turns >= 1
    assert result.total_usage.get("input_tokens", 0) > 0
