"""Deterministic integration tests for the review-gate fix: escalate_question /
answer_question must be allowed while a sibling child has completion_pending=True.

bd issue my_simple_agent-7pbj

These tests exercise the multi-child topology described in the plan:
  root leader L
    ├── PM-stub  (asks a question → qid arrives in L's inbox)
    └── QA-stub  (completion_pending=True → would previously block L)

Specifically verifies that the review gate does NOT block
mcp__beidou__escalate_question or mcp__beidou__answer_question even when a
sibling child has completion_pending=True.

Harness mirrors tests/test_review_gate.py (same helpers, no real Anthropic API).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from beidou.orchestrator import (
    USER_SENTINEL,
    AgentRecord,
    Orchestrator,
    TeamRecord,
)
from beidou.sdk_agent import build_hooks

# Reuse the helpers from test_review_gate directly by reconstructing them
# (that file is a plain module, not a conftest, so we replicate the small
# set we need here rather than introduce a coupling import).

_TM_TOP = "tm_top"


class _FakeEmitter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def emit(self, event: str, agent_id: str, team_id=None, **kwargs) -> None:
        self.calls.append((event, agent_id, team_id, kwargs))


def _make_orchestrator(tmp_path: Path) -> tuple[Orchestrator, _FakeEmitter]:
    emitter = _FakeEmitter()
    skill_root = tmp_path / "skills"
    skill_root.mkdir(exist_ok=True)
    o = Orchestrator(
        task_id="tsk_test_7pbj",
        emitter=emitter,  # type: ignore[arg-type]
        skill_root=skill_root,
    )
    return o, emitter


def _seed_team(
    o: Orchestrator,
    team_id: str,
    leader_id: str,
    *,
    depth: int = 0,
    parent=None,
) -> TeamRecord:
    t = TeamRecord(
        team_id=team_id,
        name=team_id,
        task="",
        leader_id=leader_id,
        depth=depth,
        member_ids=[],
        rules=[],
        parent_team_id=parent,
    )
    o._teams[team_id] = t
    return t


def _seed_agent(
    o: Orchestrator,
    agent_id: str,
    team_id,
    *,
    role: str = "member",
    skill: str = "fake_skill",
) -> AgentRecord:
    rec = AgentRecord(
        agent_id=agent_id,
        task_id=o.task_id,
        team_id=team_id,
        role=role,
        skill_name=skill,
        model=None,
        inbox=asyncio.Queue(),
        create_team_lock=asyncio.Lock(),
    )
    o._agents[agent_id] = rec
    if team_id is not None and team_id in o._teams and agent_id not in o._teams[team_id].member_ids:
        o._teams[team_id].member_ids.append(agent_id)
    return rec


def _get_review_gate_hook(orch: Orchestrator, caller_id: str, leader_id: str):
    """Extract the on_review_gate hook (second match-all PreToolUse matcher)."""
    hooks = build_hooks(orch, caller_id=caller_id, leader_id=leader_id)  # type: ignore[arg-type]
    pre_matchers = hooks.get("PreToolUse", [])
    match_all_matchers = [m for m in pre_matchers if m.matcher is None]
    if len(match_all_matchers) >= 2:
        return match_all_matchers[1].hooks[0]
    raise AssertionError(
        f"Expected at least 2 match-all PreToolUse matchers, found {len(match_all_matchers)}"
    )


def _input_data(tool_name: str, tool_input=None) -> dict:
    return {"tool_name": tool_name, "tool_input": tool_input or {}}


def run(coro) -> object:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Sync-event capture (same pattern as test_review_gate.py)
# ---------------------------------------------------------------------------

_SYNC_EVENT_REGISTRY: dict[int, list[tuple]] = {}


def _capture_sync_events(o: Orchestrator) -> list[tuple]:
    return _SYNC_EVENT_REGISTRY.get(id(o), [])


@pytest.fixture(autouse=True)
def patch_emit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Orchestrator.emit_event

    def _recording(self: Orchestrator, event_name: str, payload: dict) -> None:
        oid = id(self)
        if oid not in _SYNC_EVENT_REGISTRY:
            _SYNC_EVENT_REGISTRY[oid] = []
        _SYNC_EVENT_REGISTRY[oid].append((event_name, payload))
        original(self, event_name, payload)

    monkeypatch.setattr(Orchestrator, "emit_event", _recording)
    yield
    _SYNC_EVENT_REGISTRY.clear()


def _check_no_gate_denied_event(o: Orchestrator) -> None:
    _captured = _capture_sync_events(o)
    denied = [e for e in _captured if e[0] == "review_gate.denied"]
    assert not denied, f"Unexpected review_gate.denied events: {denied}"


# ---------------------------------------------------------------------------
# Integration test: multi-child topology (QA pending + PM asks question)
# ---------------------------------------------------------------------------


def test_review_gate_allows_escalate_question_with_sibling_pending(tmp_path: Path) -> None:
    """Root L leads two children: QA-stub (completion_pending=True) and PM-stub.

    Topology:
      root L (leader)
        ├── PM-stub  (live, not pending — has sent an [INBOX QUESTION])
        └── QA-stub  (completion_pending=True — awaiting review)

    Root must be able to call mcp__beidou__escalate_question for PM's question
    even though QA-stub is pending-review. This is the exact failure topology
    logged at t+123/t+124 in tsk_c04d273f.
    """
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")

    # PM-stub: live, not pending; represents an agent that sent an inbox question.
    _seed_agent(o, "pm_stub", "tm_child", skill="product_manager_v2")

    # QA-stub: completion_pending=True — this previously blocked the gate.
    qa = _seed_agent(o, "qa_stub", "tm_child", skill="qa")
    qa.completion_pending = True
    qa.completion_pending_ts = time.time()

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)

    # Root calls escalate_question for PM's inbox question.
    result = run(hook(
        _input_data("mcp__beidou__escalate_question", {
            "qid": "q_aef69762",
            "reason": "human_needed",
        }),
        tool_use_id="tsk_c04d273f_repro",
        context=None,
    ))

    # Must be allowed (empty dict = pass-through).
    assert result == {}, (
        f"escalate_question must be allowed while sibling has completion_pending=True. "
        f"Got: {result!r}"
    )
    # No review_gate.denied event must have been emitted.
    _check_no_gate_denied_event(o)


def test_review_gate_allows_answer_question_with_sibling_pending(tmp_path: Path) -> None:
    """Root L leads two children: QA-stub (completion_pending=True) and PM-stub.

    Same topology as above, but root calls mcp__beidou__answer_question instead
    of escalate_question (v1 orchestrators may answer directly rather than escalate).
    """
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")

    _seed_agent(o, "pm_stub", "tm_child", skill="product_manager_v2")

    qa = _seed_agent(o, "qa_stub", "tm_child", skill="qa")
    qa.completion_pending = True
    qa.completion_pending_ts = time.time()

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)

    result = run(hook(
        _input_data("mcp__beidou__answer_question", {
            "qid": "q_aef69762",
            "answers": [{"selected_labels": ["PostgreSQL"], "text": "Use PostgreSQL."}],
        }),
        tool_use_id="tsk_c04d273f_ans_repro",
        context=None,
    ))

    assert result == {}, (
        f"answer_question must be allowed while sibling has completion_pending=True. "
        f"Got: {result!r}"
    )
    _check_no_gate_denied_event(o)


def test_review_gate_still_denies_create_team_with_sibling_pending(tmp_path: Path) -> None:
    """Regression guard: mcp__beidou__create_team must STILL be denied when QA-stub is pending.

    Ensures the allowlist extension did not over-broaden the gate: planning-advance
    tools must remain blocked.
    """
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    _seed_agent(o, "pm_stub", "tm_child", skill="product_manager_v2")

    qa = _seed_agent(o, "qa_stub", "tm_child", skill="qa")
    qa.completion_pending = True
    qa.completion_pending_ts = time.time()

    hook = _get_review_gate_hook(o, caller_id="L", leader_id=USER_SENTINEL)

    result = run(hook(
        _input_data("mcp__beidou__create_team", {"task": "new phase"}),
        tool_use_id="regression_create_team",
        context=None,
    ))

    # Must be denied (non-empty dict).
    assert result != {}, (
        f"create_team must still be denied while sibling has completion_pending=True. "
        f"Got: {result!r}"
    )
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    # review_gate.denied event must have been emitted.
    _captured = _capture_sync_events(o)
    denied = [e for e in _captured if e[0] == "review_gate.denied"]
    assert len(denied) == 1, f"Expected 1 review_gate.denied event, got: {denied}"
    _, payload = denied[0]
    assert "qa_stub" in payload.get("pending_children", []), (
        f"Expected qa_stub in pending_children: {payload}"
    )
