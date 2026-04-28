"""Tests for the declare_plan orchestrator method and primitive.

Drives the real Orchestrator via register_plan (the orchestrator-level method),
with Path.home patched to tmp_path so nothing lands in ~/.beidou.

Coverage: cycle_detected, unknown_dep, duplicate_task_id, self_dep, empty_plan,
plan_already_declared, atomic-on-failure (no file written on validation error),
happy-path return shape, "pure data" invariant (no team/workspace/agent created).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from pathlib import Path
from unittest.mock import patch

import pytest

from beidou.orchestrator import Orchestrator
from beidou.primitives.core import PrimitiveError


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_orchestrator(tmp_path: Path) -> Orchestrator:
    skill_root = tmp_path / "skills"
    skill_root.mkdir(exist_ok=True)
    return Orchestrator(
        task_id="tsk_test",
        skill_root=skill_root,
    )


def run(coro) -> object:
    return asyncio.run(coro)


def _patch_home(tmp_path: Path):
    """Context manager: redirect Path.home() → tmp_path during plan I/O."""
    return patch.object(Path, "home", staticmethod(lambda: tmp_path))


def _plans_dir(tmp_path: Path) -> Path:
    return tmp_path / ".beidou" / "runs" / "tsk_test" / "plans"


def _any_json_in_plans_dir(tmp_path: Path) -> bool:
    d = _plans_dir(tmp_path)
    if not d.exists():
        return False
    return any(f.suffix == ".json" for f in d.iterdir())


def _simple_tasks(n: int = 1) -> list[dict]:
    return [
        {
            "id": f"t{i}",
            "role": f"role{i}",
            "skill": "worker",
            "task": f"do task {i}",
        }
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# Validation errors — nothing written on failure.
# ---------------------------------------------------------------------------


def test_declare_plan_empty_plan(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError) as ei:
                await o.register_plan(caller_id="ag_leader", specs=[])
            assert ei.value.code == "empty_plan"

        run(body())


def test_declare_plan_empty_plan_no_file_written(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError):
                await o.register_plan(caller_id="ag_leader", specs=[])
            assert not _any_json_in_plans_dir(tmp_path)

        run(body())


def test_declare_plan_duplicate_task_id(tmp_path: Path) -> None:
    specs = [
        {"id": "t1", "role": "r", "skill": "s", "task": "x"},
        {"id": "t1", "role": "r2", "skill": "s", "task": "y"},
    ]
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError) as ei:
                await o.register_plan(caller_id="ag_leader", specs=specs)
            assert ei.value.code == "duplicate_task_id"

        run(body())


def test_declare_plan_duplicate_task_id_no_file_written(tmp_path: Path) -> None:
    specs = [
        {"id": "t1", "role": "r", "skill": "s", "task": "x"},
        {"id": "t1", "role": "r2", "skill": "s", "task": "y"},
    ]
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError):
                await o.register_plan(caller_id="ag_leader", specs=specs)
            assert not _any_json_in_plans_dir(tmp_path)

        run(body())


def test_declare_plan_self_dep(tmp_path: Path) -> None:
    specs = [{"id": "t1", "role": "r", "skill": "s", "task": "x", "depends_on": ["t1"]}]
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError) as ei:
                await o.register_plan(caller_id="ag_leader", specs=specs)
            assert ei.value.code == "self_dep"

        run(body())


def test_declare_plan_self_dep_no_file_written(tmp_path: Path) -> None:
    specs = [{"id": "t1", "role": "r", "skill": "s", "task": "x", "depends_on": ["t1"]}]
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError):
                await o.register_plan(caller_id="ag_leader", specs=specs)
            assert not _any_json_in_plans_dir(tmp_path)

        run(body())


def test_declare_plan_unknown_dep(tmp_path: Path) -> None:
    specs = [{"id": "t1", "role": "r", "skill": "s", "task": "x", "depends_on": ["ghost"]}]
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError) as ei:
                await o.register_plan(caller_id="ag_leader", specs=specs)
            assert ei.value.code == "unknown_dep"

        run(body())


def test_declare_plan_cycle_detected(tmp_path: Path) -> None:
    specs = [
        {"id": "t1", "role": "r1", "skill": "s", "task": "x", "depends_on": ["t2"]},
        {"id": "t2", "role": "r2", "skill": "s", "task": "y", "depends_on": ["t1"]},
    ]
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError) as ei:
                await o.register_plan(caller_id="ag_leader", specs=specs)
            assert ei.value.code == "cycle_detected"
            assert "chain" in ei.value.details

        run(body())


def test_declare_plan_cycle_no_file_written(tmp_path: Path) -> None:
    specs = [
        {"id": "t1", "role": "r1", "skill": "s", "task": "x", "depends_on": ["t2"]},
        {"id": "t2", "role": "r2", "skill": "s", "task": "y", "depends_on": ["t1"]},
    ]
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError):
                await o.register_plan(caller_id="ag_leader", specs=specs)
            assert not _any_json_in_plans_dir(tmp_path)

        run(body())


def test_declare_plan_already_declared(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            with pytest.raises(PrimitiveError) as ei:
                await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            assert ei.value.code == "plan_already_declared"

        run(body())


# ---------------------------------------------------------------------------
# Happy path: return shape and ready-set correctness.
# ---------------------------------------------------------------------------


def test_declare_plan_happy_path_return_shape(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(2))
            assert "plan_id" in result
            assert "plan_path" in result
            assert isinstance(result["plan_path"], str)
            assert "tasks" in result
            assert isinstance(result["tasks"], list)
            assert len(result["tasks"]) == 2
            for t in result["tasks"]:
                assert "id" in t
                assert "role" in t
                assert "skill" in t
                assert "depends_on" in t
                assert "status" in t

        run(body())


def test_declare_plan_all_independent_tasks_are_ready(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(3))
            statuses = {t["id"]: t["status"] for t in result["tasks"]}
            assert all(s == "ready" for s in statuses.values())

        run(body())


def test_declare_plan_fan_in_fan_out_statuses(tmp_path: Path) -> None:
    # t1 → t2, t1 → t3, (t2,t3) → t4
    specs = [
        {"id": "t1", "role": "r1", "skill": "s", "task": "a"},
        {"id": "t2", "role": "r2", "skill": "s", "task": "b", "depends_on": ["t1"]},
        {"id": "t3", "role": "r3", "skill": "s", "task": "c", "depends_on": ["t1"]},
        {"id": "t4", "role": "r4", "skill": "s", "task": "d", "depends_on": ["t2", "t3"]},
    ]
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=specs)
            statuses = {t["id"]: t["status"] for t in result["tasks"]}
            assert statuses["t1"] == "ready"
            assert statuses["t2"] == "blocked"
            assert statuses["t3"] == "blocked"
            assert statuses["t4"] == "blocked"

        run(body())


def test_declare_plan_plan_file_written_on_success(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            plan_file = pathlib.Path(result["plan_path"])
            assert plan_file.exists()
            data = json.loads(plan_file.read_text())
            assert data["plan_id"] == result["plan_id"]

        run(body())


# ---------------------------------------------------------------------------
# Pure-data invariant: declare_plan creates no team, workspace, or agent.
# ---------------------------------------------------------------------------


def test_declare_plan_creates_no_team_or_agent(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            before_agents = len(o._agents)
            before_teams = len(o._teams)

            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(3))

            assert len(o._agents) == before_agents, "declare_plan must not create any agents"
            assert len(o._teams) == before_teams, "declare_plan must not create any teams"

        run(body())


def test_declare_plan_emits_plan_declared_event(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []

    class _RecordingEmitter:
        async def emit(self, event: str, agent_id: str, team_id=None, **kwargs) -> None:
            events.append((event, {"agent_id": agent_id, **kwargs}))

    skill_root = tmp_path / "skills"
    skill_root.mkdir(exist_ok=True)
    o = Orchestrator(
        task_id="tsk_test",
        emitter=_RecordingEmitter(),  # type: ignore[arg-type]
        skill_root=skill_root,
    )

    with _patch_home(tmp_path):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            await asyncio.sleep(0)
            plan_events = [ev for ev in events if ev[0] == "plan_declared"]
            assert len(plan_events) == 1

        run(body())
