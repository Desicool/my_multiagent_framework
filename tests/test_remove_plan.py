"""Tests for the remove_plan orchestrator method.

Coverage: no_active_plan, plan_in_use (returns in_flight task ids),
successful removal preserves .removed.json, replan-after-remove works.
"""

from __future__ import annotations

import asyncio
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
    return Orchestrator(task_id="tsk_test", skill_root=skill_root)


def run(coro) -> object:
    return asyncio.run(coro)


def _patch_home(tmp_path: Path):
    return patch.object(Path, "home", staticmethod(lambda: tmp_path))


def _simple_tasks(n: int = 2) -> list[dict]:
    return [
        {"id": f"t{i}", "role": f"role{i}", "skill": "worker", "task": f"task {i}"}
        for i in range(1, n + 1)
    ]


def _plans_dir(tmp_path: Path) -> Path:
    return tmp_path / ".beidou" / "runs" / "tsk_test" / "plans"


# ---------------------------------------------------------------------------
# Error cases.
# ---------------------------------------------------------------------------


def test_remove_plan_no_active_plan(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            with pytest.raises(PrimitiveError) as ei:
                await o.remove_active_plan(caller_id="ag_leader")
            assert ei.value.code == "no_active_plan"

        run(body())


def test_remove_plan_in_use_returns_in_flight_ids(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(2))
            plan_id = result["plan_id"]
            plan = o._plans[plan_id]

            from beidou.plans import mark_status
            mark_status(plan, "t1", "in_flight", agent_id="ag_x")

            with pytest.raises(PrimitiveError) as ei:
                await o.remove_active_plan(caller_id="ag_leader")
            assert ei.value.code == "plan_in_use"
            assert "t1" in ei.value.details.get("in_flight_task_ids", [])

        run(body())


def test_remove_plan_in_use_does_not_delete_plan_file(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            plan = o._plans[result["plan_id"]]

            from beidou.plans import mark_status
            mark_status(plan, "t1", "in_flight", agent_id="ag_y")

            with pytest.raises(PrimitiveError):
                await o.remove_active_plan(caller_id="ag_leader")

            assert pathlib.Path(plan.file_path).exists(), "plan file must still exist"

        run(body())


def test_remove_plan_done_tasks_do_not_block_removal(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(2))
            plan = o._plans[result["plan_id"]]

            from beidou.plans import mark_status
            mark_status(plan, "t1", "done")
            mark_status(plan, "t2", "done")

            out = await o.remove_active_plan(caller_id="ag_leader")
            assert "removed_plan_id" in out

        run(body())


# ---------------------------------------------------------------------------
# Successful removal.
# ---------------------------------------------------------------------------


def test_remove_plan_success_returns_removed_plan_id(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            plan_id = result["plan_id"]

            out = await o.remove_active_plan(caller_id="ag_leader")
            assert out["removed_plan_id"] == plan_id

        run(body())


def test_remove_plan_preserves_removed_json_file(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            plan_id = result["plan_id"]

            await o.remove_active_plan(caller_id="ag_leader")

            plans_dir = _plans_dir(tmp_path)
            removed_file = plans_dir / f"{plan_id}.removed.json"
            assert removed_file.exists(), ".removed.json audit artifact must be preserved"

        run(body())


def test_remove_plan_original_json_gone(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            result = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            plan_id = result["plan_id"]
            plan_file = pathlib.Path(result["plan_path"])

            await o.remove_active_plan(caller_id="ag_leader")

            assert not plan_file.exists(), "original plan file must not exist after removal"

        run(body())


def test_remove_plan_clears_active_plan_slot(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            await o.remove_active_plan(caller_id="ag_leader")

            assert "ag_leader" not in o._active_plan_by_agent

        run(body())


# ---------------------------------------------------------------------------
# Replan after remove.
# ---------------------------------------------------------------------------


def test_replan_after_remove_succeeds(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            await o.remove_active_plan(caller_id="ag_leader")

            new_specs = [
                {"id": "new_t1", "role": "new_role", "skill": "worker", "task": "new task"},
                {"id": "new_t2", "role": "new_role2", "skill": "worker", "task": "another task",
                 "depends_on": ["new_t1"]},
            ]
            result2 = await o.register_plan(caller_id="ag_leader", specs=new_specs)
            assert "plan_id" in result2
            statuses = {t["id"]: t["status"] for t in result2["tasks"]}
            assert statuses["new_t1"] == "ready"
            assert statuses["new_t2"] == "blocked"

        run(body())


def test_replan_after_remove_both_plan_files_on_disk(tmp_path: Path) -> None:
    o = _make_orchestrator(tmp_path)
    with _patch_home(tmp_path):
        async def body():
            r1 = await o.register_plan(caller_id="ag_leader", specs=_simple_tasks(1))
            plan_id_1 = r1["plan_id"]
            await o.remove_active_plan(caller_id="ag_leader")

            r2 = await o.register_plan(
                caller_id="ag_leader",
                specs=[{"id": "ta", "role": "r", "skill": "s", "task": "t"}],
            )
            plan_id_2 = r2["plan_id"]

            plans_dir = _plans_dir(tmp_path)
            assert (plans_dir / f"{plan_id_1}.removed.json").exists()
            assert (plans_dir / f"{plan_id_2}.json").exists()

        run(body())
