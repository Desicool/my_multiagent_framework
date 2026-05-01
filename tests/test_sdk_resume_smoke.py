"""Precondition smoke test for SDK session resume.

Validates that MCP tool continuity works across a resume with a rebuilt
MCP server.  If this test fails, strike-1 (resume) in the crash-recovery
policy is not viable and should be skipped.

This is a REAL INTEGRATION test — it shells out to the Claude Code CLI
and requires both ``ANTHROPIC_API_KEY`` and ``claude`` on PATH.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import textwrap
import time
import types
from pathlib import Path

import pytest

from beidou import sdk_agent
from beidou.primitives.core import Message
from beidou.sdk_agent import RunResult, SpawnSpec

_CLAUDE_CLI = shutil.which("claude")
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
def test_mcp_tool_continuity_across_resume(tmp_path):
    """Spawn -> MCP tool call -> kill -> resume with rebuilt MCP -> verify tools work.

    This is a precondition test for the crash-recovery strike-1 (resume) path.
    If the resumed session cannot make MCP tool calls because of stale
    tool_use_ids from the old MCP server, strike-1 is not viable.
    """
    from dotenv import load_dotenv

    load_dotenv()

    skill_dir = tmp_path / "resume_test_agent"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: resume_test_agent
            version: 1.0.0
            description: Agent for testing MCP tool continuity across resume.
            allowed-tools:
              - report_status
            ---
            You are a test agent. On the first session, call
            mcp__beidou__report_status with state="working" and
            detail="pre-resume". On a resumed session, call
            mcp__beidou__report_status with state="done" and
            detail="post-resume", then produce a one-sentence
            acknowledgment.
            """
        )
    )

    # --- First session: an orchestrator stub for the pre-resume spawn ---
    class ResumeTestOrch:
        """Minimal orchestrator stub for the resume smoke test."""
        def __init__(self):
            self.events: list[tuple[str, dict]] = []

        def emit_event(self, name: str, payload: dict) -> None:
            self.events.append((name, payload))

        def was_terminated(self, caller_id: str) -> bool:
            return False

        def queue_for(self, agent_id: str) -> asyncio.Queue:
            return asyncio.Queue()

        def record_status(self, caller_id, state, detail):
            pass

        def record_assistant_text(self, caller_id, text, tool_use_ids):
            pass

        def assistant_text_for_turn(self, caller_id, tool_use_id):
            return None

        def deliver_message(self, from_id, to_id, body, kind="message"):
            pass

        def agent_skill_name(self, agent_id):
            return "resume_test_agent"

        def agent_completion_pending(self, agent_id):
            return False

        def agent_completion_pending_ts(self, agent_id):
            return None

        def agent_last_status_detail(self, agent_id):
            return ""

        def agent_name(self, agent_id):
            return None

        # _agents dict for drain loop access
        _agents: dict = {}

    orch1 = ResumeTestOrch()
    # Seed an agent record so the drain loop can find it.
    orch1._agents["ag_resume"] = types.SimpleNamespace(
        terminate_consumed=False,
        inbox=asyncio.Queue(),
        inflight_tools=0,
    )

    # --- First run: start a session that calls report_status ---
    spec1 = SpawnSpec(
        caller_id="ag_resume",
        skill_name="resume_test_agent",
        skill_root=tmp_path,
        task="Call mcp__beidou__report_status with state='working' and detail='pre-resume'. Do not call any other tool. End your turn after the tool call.",
        model="claude-haiku-4-5-20251001",
    )

    async def drive_first():
        # Pre-seed a terminate sentinel to end the session after one turn.
        orch1.queue_for("ag_resume")
        await (orch1._agents["ag_resume"].inbox).put(
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="term1",
                kind="terminate",
            )
        )
        result1 = await sdk_agent.run_agent(orch1, spec1)
        return result1

    result1 = asyncio.run(drive_first())

    # The first session should have completed with tool calls.
    names1 = [n for n, _ in orch1.events]
    assert "agent_started" in names1, "first session must start"
    assert "tool_called" in names1, (
        "first session must have made an MCP tool call; "
        "if the model did not call report_status, the agent prompt needs tuning"
    )
    assert "agent_completed" in names1, "first session must complete"

    # Get the session_id from the first run.
    session_id = result1.session_id
    assert session_id is not None, "first session must have a session_id"

    # --- Second run: resume the session with a rebuilt MCP server ---
    orch2 = ResumeTestOrch()
    orch2._agents["ag_resume"] = types.SimpleNamespace(
        terminate_consumed=False,
        inbox=asyncio.Queue(),
        inflight_tools=0,
    )

    spec2 = SpawnSpec(
        caller_id="ag_resume",
        skill_name="resume_test_agent",
        skill_root=tmp_path,
        task="Your previous session was interrupted. Resume your work. Call mcp__beidou__report_status with state='done' and detail='post-resume', then produce a one-sentence acknowledgment.",
        model="claude-haiku-4-5-20251001",
        resume_session_id=session_id,
    )

    async def drive_second():
        orch2.queue_for("ag_resume")
        await (orch2._agents["ag_resume"].inbox).put(
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=time.time(),
                message_id="term2",
                kind="terminate",
            )
        )
        result2 = await sdk_agent.run_agent(orch2, spec2)
        return result2

    result2 = asyncio.run(drive_second())

    names2 = [n for n, _ in orch2.events]
    assert "agent_started" in names2, "resumed session must start"
    assert "agent_completed" in names2, "resumed session must complete"

    # The key assertion: the resumed session must be able to make MCP tool
    # calls despite the MCP server being rebuilt. If tool_called is absent,
    # strike-1 resume is NOT viable (stale tool_use_ids from old MCP).
    tool_calls = [n for n, _ in orch2.events if n == "tool_called"]
    assert len(tool_calls) > 0, (
        "RESUME BROKEN: resumed session could not make MCP tool calls. "
        "MCP rebuild likely causes stale tool_use_ids. Strike-1 resume is NOT viable."
    )

    # Verify the second session made meaningful progress.
    cost_events = [p for n, p in orch2.events if n == "run.cost"]
    if cost_events:
        cost = cost_events[0]
        assert cost.get("num_turns", 0) >= 1, "resumed session must execute at least one turn"
