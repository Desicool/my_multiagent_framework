"""Tests for the spawn_agent orchestrator method.

Coverage: task_not_ready (unmet_deps), task_not_pending after spawn,
team_cap_exceeded at 9th in-flight, terminate_child unblocks dependents,
force-terminate marks failed and propagates blocking, full DAG runthrough
(5 tasks, 3 levels). Also: team_created:true only on the first spawn.

The SDK launch is stubbed out via monkeypatching _run_agent_with_policy
so tests don't connect to the Anthropic API. load_skill and provision_skills
are also stubbed so no real SKILL.md files are required.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from beidou.orchestrator import AgentRecord, Orchestrator
from beidou.primitives.core import PrimitiveError


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_orchestrator(tmp_path: Path) -> Orchestrator:
    skill_root = tmp_path / "skills"
    skill_root.mkdir(exist_ok=True)
    return Orchestrator(task_id="tsk_test", skill_root=skill_root)


def run(coro) -> object:
    return asyncio.run(coro)


def _patch_home(tmp_path: Path):
    return patch.object(Path, "home", staticmethod(lambda: tmp_path))


@contextlib.contextmanager
def _spawn_stubs(o: Orchestrator):
    """Combined context: stub skill loader, provision_skills, and _run_agent_with_policy."""
    fake_skill = MagicMock()
    fake_skill.system_prompt = "stub"
    fake_skill.allowed_tools = []
    with (
        patch("beidou.skills.loader.load_skill", return_value=fake_skill),
        patch("beidou.skills.loader.provision_skills", return_value=None),
        patch.object(o, "_run_agent_with_policy", _noop_run),
    ):
        yield


async def _noop_run(rec, spec):
    """Stub for _run_agent_with_policy — does nothing (no SDK calls)."""
    pass


def _seed_leader(o: Orchestrator) -> str:
    """Register a root-level leader agent that can call spawn_agent."""
    leader_id = "ag_leader"
    rec = AgentRecord(
        agent_id=leader_id,
        task_id=o.task_id,
        team_id=None,
        role="leader",
        skill_name="orchestrator",
        model=None,
        inbox=asyncio.Queue(),
        create_team_lock=asyncio.Lock(),
    )
    o._agents[leader_id] = rec
    o._root_id = leader_id
    return leader_id


def _simple_tasks(n: int = 1) -> list[dict]:
    return [
        {"id": f"t{i}", "role": f"role{i}", "skill": "worker", "task": f"task text {i}"}
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# Error: no_active_plan.
# ---------------------------------------------------------------------------


def test_spawn_agent_no_active_plan(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError) as ei:
                await o.spawn_for_task(caller_id="ag_leader", task_id="t1")
            assert ei.value.code == "no_active_plan"

        run(body())


# ---------------------------------------------------------------------------
# Error: task_not_ready (blocked task returns unmet_deps).
# ---------------------------------------------------------------------------


def test_spawn_agent_task_not_ready_blocked(tmp_path: Path) -> None:
    specs = [
        {"id": "t1", "role": "r1", "skill": "worker", "task": "a"},
        {"id": "t2", "role": "r2", "skill": "worker", "task": "b", "depends_on": ["t1"]},
    ]
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=specs)
            with pytest.raises(PrimitiveError) as ei:
                await o.spawn_for_task(caller_id="ag_leader", task_id="t2")
            assert ei.value.code == "task_not_ready"
            assert "t1" in ei.value.details.get("unmet_deps", [])

        run(body())


# ---------------------------------------------------------------------------
# Error: task_not_pending (already in_flight).
# ---------------------------------------------------------------------------


def test_spawn_agent_task_not_pending_in_flight(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            await o.spawn_for_task(caller_id="ag_leader", task_id="t1")

            with pytest.raises(PrimitiveError) as ei:
                await o.spawn_for_task(caller_id="ag_leader", task_id="t1")
            assert ei.value.code == "task_not_pending"

        run(body())


# ---------------------------------------------------------------------------
# First spawn: team_created=True. Subsequent spawns: team_created=False.
# ---------------------------------------------------------------------------


def test_spawn_agent_team_created_true_on_first_call(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(2))
            result = await o.spawn_for_task(caller_id="ag_leader", task_id="t1")
            assert result["team_created"] is True

        run(body())


def test_spawn_agent_team_created_false_on_second_call(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(2))
            await o.spawn_for_task(caller_id="ag_leader", task_id="t1")
            result2 = await o.spawn_for_task(caller_id="ag_leader", task_id="t2")
            assert result2["team_created"] is False

        run(body())


def test_spawn_agent_reuses_same_team_on_second_call(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(2))
            r1 = await o.spawn_for_task(caller_id="ag_leader", task_id="t1")
            r2 = await o.spawn_for_task(caller_id="ag_leader", task_id="t2")
            assert r1["team_id"] == r2["team_id"]

        run(body())


# ---------------------------------------------------------------------------
# Team cap: 8 in-flight members; 9th spawn raises team_cap_exceeded.
# ---------------------------------------------------------------------------


def test_spawn_agent_team_cap_exceeded(tmp_path: Path) -> None:
    n = 9
    specs = [
        {"id": f"t{i}", "role": f"role{i}", "skill": "worker", "task": f"task {i}"}
        for i in range(1, n + 1)
    ]
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=specs)
            for i in range(1, 9):
                await o.spawn_for_task(caller_id="ag_leader", task_id=f"t{i}")

            with pytest.raises(PrimitiveError) as ei:
                await o.spawn_for_task(caller_id="ag_leader", task_id="t9")
            assert ei.value.code == "team_cap_exceeded"
            assert ei.value.details.get("live_count") == 8

        run(body())


# ---------------------------------------------------------------------------
# terminate_child unblocks dependents via mark_task_done.
# ---------------------------------------------------------------------------


def test_spawn_agent_terminate_child_unblocks_dependents(tmp_path: Path) -> None:
    specs = [
        {"id": "t1", "role": "r1", "skill": "worker", "task": "a"},
        {"id": "t2", "role": "r2", "skill": "worker", "task": "b", "depends_on": ["t1"]},
    ]
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=specs)
            result = await o.spawn_for_task(caller_id="ag_leader", task_id="t1")

            plan_id = o._active_plan_by_agent["ag_leader"]
            plan = o._plans[plan_id]
            assert plan.tasks["t2"].status == "blocked"

            await o.mark_task_done(task_id="t1")

            assert plan.tasks["t2"].status == "ready"
            result2 = await o.spawn_for_task(caller_id="ag_leader", task_id="t2")
            assert "agent_id" in result2

        run(body())


# ---------------------------------------------------------------------------
# Force-terminate marks task failed; dependents stay blocked.
# ---------------------------------------------------------------------------


def test_spawn_agent_force_terminate_marks_failed_and_blocks_dependents(tmp_path: Path) -> None:
    specs = [
        {"id": "t1", "role": "r1", "skill": "worker", "task": "a"},
        {"id": "t2", "role": "r2", "skill": "worker", "task": "b", "depends_on": ["t1"]},
    ]
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=specs)
            await o.spawn_for_task(caller_id="ag_leader", task_id="t1")

            await o.mark_task_failed(task_id="t1")

            plan_id = o._active_plan_by_agent["ag_leader"]
            plan = o._plans[plan_id]
            assert plan.tasks["t1"].status == "failed"
            assert plan.tasks["t2"].status == "blocked"

        run(body())


# ---------------------------------------------------------------------------
# Full DAG runthrough: 5 tasks, 3 levels (t1 → t2,t3; t2,t3 → t4; t4 → t5).
# ---------------------------------------------------------------------------


def test_spawn_agent_full_dag_runthrough(tmp_path: Path) -> None:
    specs = [
        {"id": "t1", "role": "r1", "skill": "worker", "task": "level0"},
        {"id": "t2", "role": "r2", "skill": "worker", "task": "level1a", "depends_on": ["t1"]},
        {"id": "t3", "role": "r3", "skill": "worker", "task": "level1b", "depends_on": ["t1"]},
        {"id": "t4", "role": "r4", "skill": "worker", "task": "level2", "depends_on": ["t2", "t3"]},
        {"id": "t5", "role": "r5", "skill": "worker", "task": "level3", "depends_on": ["t4"]},
    ]
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            r = await o.register_plan(caller_id="ag_leader", specs=specs)
            statuses = {t["id"]: t["status"] for t in r["tasks"]}
            assert statuses["t1"] == "ready"
            for blocked_id in ("t2", "t3", "t4", "t5"):
                assert statuses[blocked_id] == "blocked"

            s1 = await o.spawn_for_task(caller_id="ag_leader", task_id="t1")
            assert s1["team_created"] is True

            await o.mark_task_done(task_id="t1")

            plan = o._plans[o._active_plan_by_agent["ag_leader"]]
            assert plan.tasks["t2"].status == "ready"
            assert plan.tasks["t3"].status == "ready"

            s2 = await o.spawn_for_task(caller_id="ag_leader", task_id="t2")
            s3 = await o.spawn_for_task(caller_id="ag_leader", task_id="t3")
            assert s2["team_created"] is False
            assert s3["team_created"] is False

            await o.mark_task_done(task_id="t2")
            assert plan.tasks["t4"].status == "blocked"

            await o.mark_task_done(task_id="t3")
            assert plan.tasks["t4"].status == "ready"

            await o.spawn_for_task(caller_id="ag_leader", task_id="t4")
            await o.mark_task_done(task_id="t4")

            assert plan.tasks["t5"].status == "ready"
            await o.spawn_for_task(caller_id="ag_leader", task_id="t5")
            await o.mark_task_done(task_id="t5")

            for tid in ("t1", "t2", "t3", "t4", "t5"):
                assert plan.tasks[tid].status == "done"

        run(body())


# ---------------------------------------------------------------------------
# Task status is in_flight immediately after spawn.
# ---------------------------------------------------------------------------


def test_spawn_agent_task_in_flight_after_spawn(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            await o.spawn_for_task(caller_id="ag_leader", task_id="t1")

            plan = o._plans[o._active_plan_by_agent["ag_leader"]]
            assert plan.tasks["t1"].status == "in_flight"

        run(body())


# ---------------------------------------------------------------------------
# Return shape: agent_id, team_id, task_id, remaining_ready.
# ---------------------------------------------------------------------------


def test_spawn_agent_return_shape(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    _seed_leader(o)

    with _patch_home(tmp_path), _spawn_stubs(o):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(2))
            result = await o.spawn_for_task(caller_id="ag_leader", task_id="t1")

            assert "agent_id" in result
            assert "team_id" in result
            assert result["task_id"] == "t1"
            assert "remaining_ready" in result
            assert isinstance(result["remaining_ready"], list)
            assert "team_created" in result

        run(body())
