"""Tests for the teamless-root model (bd issue my_simple_agent-vhx).

The root agent is now a plain agent with team_id=None. No synthetic tm_root
team is created. These tests cover:

  (a) Root completes solo — agent_started.team_id is None, no team_created fires.
  (b) Root spawns one team — first team_created has parent_team_id=None,
      leader_id==root_agent_id, depth==1.
  (c) Recursion: root spawns team A, member of A spawns team B — A.depth==1,
      B.depth==2, B.parent_team_id==A.team_id.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import pytest

from beidou import orchestrator as orch_module
from beidou.orchestrator import (
    USER_SENTINEL,
    AgentRecord,
    Orchestrator,
    TeamRecord,
)
from beidou.sdk_agent import RunResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEmitter:
    """Records emitter calls without touching ~/.beidou or SQLite."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Optional[str], dict]] = []

    async def emit(self, event: str, agent_id: str, team_id=None, **kwargs) -> None:
        self.calls.append((event, agent_id, team_id, kwargs))

    def events_named(self, name: str) -> list[tuple[str, Optional[str], dict]]:
        return [(a, t, kw) for ev, a, t, kw in self.calls if ev == name]


def _make_orchestrator(tmp_path: Path) -> tuple[Orchestrator, _FakeEmitter]:
    emitter = _FakeEmitter()
    skill_root = tmp_path / "skills"
    skill_root.mkdir(exist_ok=True)
    o = Orchestrator(
        task_id="tsk_teamless_root",
        emitter=emitter,  # type: ignore[arg-type]
        skill_root=skill_root,
        project_workspace=tmp_path,
    )
    return o, emitter


def _make_result(*, terminated: bool) -> RunResult:
    return RunResult(
        final_text="done",
        total_cost_usd=0.0,
        total_usage={},
        num_turns=1,
        duration_ms=0,
        stop_reason="end_turn",
        session_id=None,
        terminated=terminated,
        contract_violation=not terminated,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test (a): root completes solo, no create_team call
# ---------------------------------------------------------------------------


def test_root_solo_team_id_is_none(tmp_path: Path, monkeypatch) -> None:
    """Root agent is teamless: team_id=None, no team_created event fires."""
    o, emitter = _make_orchestrator(tmp_path)

    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())
    monkeypatch.setattr(loader_mod, "provision_skills", lambda *a, **kw: None)

    async def fake_run(orch, spec):
        # Root just returns immediately — no team creation.
        rec = orch._agents.get(spec.caller_id)
        if rec is not None:
            rec.terminate_consumed = True
        return _make_result(terminated=True)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", fake_run)

    async def body():
        result = await o.run_root("fake", "solve the world")

        # 1. Root agent record has team_id=None.
        root_rec = o._agents[o._root_id]
        assert root_rec.team_id is None, (
            f"Expected root agent to have team_id=None, got: {root_rec.team_id!r}"
        )

        # 2. No team_created event was emitted.
        team_created_events = emitter.events_named("team_created")
        assert len(team_created_events) == 0, (
            f"Expected no team_created events, got: {team_created_events}"
        )

        # 3. Root does not appear in _teams dict.
        assert o._root_id not in o._teams, (
            "Root agent_id should not be a key in _teams"
        )
        assert not any(
            o._root_id in (t.member_ids or []) for t in o._teams.values()
        ), "Root agent should not appear as a member in any TeamRecord"

    run(body())


# ---------------------------------------------------------------------------
# Test (b): root spawns one team
# ---------------------------------------------------------------------------


def test_root_spawns_team_depth_1_parent_none(tmp_path: Path, monkeypatch) -> None:
    """Root spawns one team: depth=1, parent_team_id=None, leader=root agent."""
    o, emitter = _make_orchestrator(tmp_path)

    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())
    monkeypatch.setattr(loader_mod, "provision_skills", lambda *a, **kw: None)

    spawned_team_id: list[str] = []
    members_ready = asyncio.Event()

    async def root_behaviour(orch, spec):
        # Root spawns a team of one member.
        out = await orch.spawn_team(
            leader_id=spec.caller_id,
            name="alpha",
            task="sub-task",
            roles=[{"role": "worker", "skill": "fake", "description": "do work"}],
            rules=[],
        )
        spawned_team_id.append(out["team_id"])
        members_ready.set()

        # Root waits for members to finish, then terminates cleanly.
        for m in out["members"]:
            member_rec = orch._agents[m["agent_id"]]
            while member_rec.run_task is None or not member_rec.run_task.done():
                await asyncio.sleep(0.01)
        root_rec = orch._agents.get(spec.caller_id)
        if root_rec is not None:
            root_rec.terminate_consumed = True
        return _make_result(terminated=True)

    async def member_behaviour(orch, spec):
        mem_rec = orch._agents.get(spec.caller_id)
        if mem_rec is not None:
            mem_rec.terminate_consumed = True
        return _make_result(terminated=True)

    async def fake_run(orch, spec):
        tvars = spec.template_vars or {}
        if tvars.get("role") == "root":
            return await root_behaviour(orch, spec)
        return await member_behaviour(orch, spec)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", fake_run)

    async def body():
        await o.run_root("fake", "delegate-task")
        await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)

        # Root agent is teamless.
        root_rec = o._agents[o._root_id]
        assert root_rec.team_id is None, (
            f"Root team_id must be None, got {root_rec.team_id!r}"
        )

        # Exactly one team was created.
        assert len(spawned_team_id) == 1
        tid = spawned_team_id[0]
        team_rec = o._teams[tid]

        # Depth 1 (root is teamless = depth 0, first spawn = depth 1).
        assert team_rec.depth == 1, f"Expected depth 1, got {team_rec.depth}"

        # parent_team_id is None (root has no team).
        assert team_rec.parent_team_id is None, (
            f"Expected parent_team_id=None, got {team_rec.parent_team_id!r}"
        )

        # Leader is the root agent.
        assert team_rec.leader_id == o._root_id, (
            f"Expected leader_id={o._root_id!r}, got {team_rec.leader_id!r}"
        )

        # Verify team_created event matches.
        tc_events = emitter.events_named("team_created")
        assert len(tc_events) == 1
        _, _, tc_kw = tc_events[0]
        assert tc_kw["depth"] == 1
        assert tc_kw["parent_team_id"] is None
        assert tc_kw["leader_agent_id"] == o._root_id

    run(body())


# ---------------------------------------------------------------------------
# Test (c): recursion — root → team A → team B
# ---------------------------------------------------------------------------


def test_recursive_spawn_depth_increments(tmp_path: Path, monkeypatch) -> None:
    """root → A (depth 1) → B (depth 2); B.parent_team_id == A.team_id."""
    o, emitter = _make_orchestrator(tmp_path)

    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())
    monkeypatch.setattr(loader_mod, "provision_skills", lambda *a, **kw: None)

    a_team_id: list[str] = []
    b_team_id: list[str] = []

    async def root_behaviour(orch, spec):
        out = await orch.spawn_team(
            leader_id=spec.caller_id,
            name="team-A",
            task="a-task",
            roles=[{"role": "a-leader", "skill": "fake", "description": "lead sub"}],
            rules=[],
        )
        a_team_id.append(out["team_id"])
        # Wait for A members to finish.
        for m in out["members"]:
            mem_rec = orch._agents[m["agent_id"]]
            while mem_rec.run_task is None or not mem_rec.run_task.done():
                await asyncio.sleep(0.01)
        rec = orch._agents.get(spec.caller_id)
        if rec is not None:
            rec.terminate_consumed = True
        return _make_result(terminated=True)

    async def a_leader_behaviour(orch, spec):
        out = await orch.spawn_team(
            leader_id=spec.caller_id,
            name="team-B",
            task="b-task",
            roles=[{"role": "b-worker", "skill": "fake", "description": "do leaf work"}],
            rules=[],
        )
        b_team_id.append(out["team_id"])
        # Wait for B members to finish.
        for m in out["members"]:
            mem_rec = orch._agents[m["agent_id"]]
            while mem_rec.run_task is None or not mem_rec.run_task.done():
                await asyncio.sleep(0.01)
        rec = orch._agents.get(spec.caller_id)
        if rec is not None:
            rec.terminate_consumed = True
        return _make_result(terminated=True)

    async def leaf_behaviour(orch, spec):
        rec = orch._agents.get(spec.caller_id)
        if rec is not None:
            rec.terminate_consumed = True
        return _make_result(terminated=True)

    async def fake_run(orch, spec):
        tvars = spec.template_vars or {}
        role = tvars.get("role", "")
        if role == "root":
            return await root_behaviour(orch, spec)
        if role == "a-leader":
            return await a_leader_behaviour(orch, spec)
        return await leaf_behaviour(orch, spec)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", fake_run)

    async def body():
        await o.run_root("fake", "recursive-test")
        await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)

        # Root is teamless.
        root_rec = o._agents[o._root_id]
        assert root_rec.team_id is None

        # Team A: depth 1, parent None.
        assert len(a_team_id) == 1
        a_rec = o._teams[a_team_id[0]]
        assert a_rec.depth == 1
        assert a_rec.parent_team_id is None

        # Team B: depth 2, parent = A's team_id.
        assert len(b_team_id) == 1
        b_rec = o._teams[b_team_id[0]]
        assert b_rec.depth == 2
        assert b_rec.parent_team_id == a_team_id[0]

    run(body())


# ---------------------------------------------------------------------------
# Test (d): user task propagates to every spawned agent's first user message,
# and {role_description} stays clean (only the role-level description, not
# the user task).
# ---------------------------------------------------------------------------


def test_user_task_propagates_role_description_stays_clean(tmp_path: Path, monkeypatch) -> None:
    """Each spawned agent receives the originating user task as spec.task,
    even when the team-level create_team `task` arg is something different.
    {role_description} carries only the role-level description.
    """
    o, _ = _make_orchestrator(tmp_path)

    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())
    monkeypatch.setattr(loader_mod, "provision_skills", lambda *a, **kw: None)

    USER_TASK = "build a calculator with React"
    TEAM_TASK = "gather requirements (orchestrator's words)"
    ROLE_DESC = "Write requirements.md to the team workspace."

    captured: list[dict] = []

    async def fake_run(orch, spec):
        captured.append({
            "role": (spec.template_vars or {}).get("role"),
            "task": spec.task,
            "role_description": (spec.template_vars or {}).get("role_description"),
        })

        # Root spawns one team, then waits for the member.
        if (spec.template_vars or {}).get("role") == "root":
            out = await orch.spawn_team(
                leader_id=spec.caller_id,
                name="requirements",
                task=TEAM_TASK,
                roles=[{
                    "role": "product-manager",
                    "skill": "fake",
                    "description": ROLE_DESC,
                }],
                rules=[],
            )
            for m in out["members"]:
                member_rec = orch._agents[m["agent_id"]]
                while member_rec.run_task is None or not member_rec.run_task.done():
                    await asyncio.sleep(0.01)

        rec = orch._agents.get(spec.caller_id)
        if rec is not None:
            rec.terminate_consumed = True
        return _make_result(terminated=True)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", fake_run)

    async def body():
        await o.run_root("fake", USER_TASK)
        await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)

        # Two agents: root + product-manager.
        roles = [c["role"] for c in captured]
        assert "root" in roles
        assert "product-manager" in roles

        root = next(c for c in captured if c["role"] == "root")
        pm = next(c for c in captured if c["role"] == "product-manager")

        # 1. Both agents' first user message is the originating user task.
        assert root["task"] == USER_TASK, (
            f"Root spec.task should be the user task, got {root['task']!r}"
        )
        assert pm["task"] == USER_TASK, (
            f"PM spec.task should be the user task (not {TEAM_TASK!r}), got {pm['task']!r}"
        )

        # 2. Root's role_description is empty (root has no role-specific scope).
        assert root["role_description"] == "", (
            f"Root role_description should be empty, got {root['role_description']!r}"
        )

        # 3. PM's role_description is exactly the role-level description, clean.
        assert pm["role_description"] == ROLE_DESC, (
            f"PM role_description should be {ROLE_DESC!r}, got {pm['role_description']!r}"
        )

    run(body())
