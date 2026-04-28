"""Tests for beidou/plans.py: JSON shape, atomic write semantics, and load_plans_for_run."""

from __future__ import annotations

import json
import pathlib
import time

import pytest

from beidou.plans import (
    Plan,
    Task,
    create_plan,
    load_plan,
    load_plans_for_run,
    mark_status,
    remove_plan_file,
    save_plan,
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _minimal_specs(n: int = 2) -> list[dict]:
    return [
        {"id": f"t{i}", "role": f"role{i}", "skill": "worker", "task": f"do task {i}"}
        for i in range(1, n + 1)
    ]


def _linear_specs() -> list[dict]:
    return [
        {"id": "t1", "role": "first", "skill": "worker", "task": "step 1"},
        {"id": "t2", "role": "second", "skill": "worker", "task": "step 2", "depends_on": ["t1"]},
    ]


# ---------------------------------------------------------------------------
# JSON shape: no forward_deps, no removed_at, fields match spec exactly.
# ---------------------------------------------------------------------------


def test_plan_json_shape_no_forward_deps(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_leader", "run_1", _minimal_specs(), root=tmp_path)

    raw = json.loads(pathlib.Path(plan.file_path).read_text())

    assert "forward_deps" not in raw, "forward_deps must not be stored on disk"
    assert "removed_at" not in raw, "removed_at must not be stored on disk"
    assert "file_path" not in raw, "file_path must not be stored on disk"

    assert raw["plan_id"] == plan.plan_id
    assert raw["owner_agent_id"] == "ag_leader"
    assert raw["run_task_id"] == "run_1"
    assert isinstance(raw["created_at"], float)
    assert isinstance(raw["tasks"], dict)

    for tid, td in raw["tasks"].items():
        assert "id" in td
        assert "role" in td
        assert "skill" in td
        assert "task" in td
        assert "description" in td
        assert "model" in td
        assert "depends_on" in td
        assert "status" in td
        assert "agent_id" in td
        assert "spawned_at" in td
        assert "completed_at" in td
        assert "forward_deps" not in td


def test_plan_json_initial_statuses(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _linear_specs(), root=tmp_path)
    raw = json.loads(pathlib.Path(plan.file_path).read_text())

    assert raw["tasks"]["t1"]["status"] == "ready"
    assert raw["tasks"]["t2"]["status"] == "blocked"


def test_plan_json_task_field_mapping(tmp_path: pathlib.Path) -> None:
    specs = [{"id": "t1", "role": "r", "skill": "s", "task": "the task text"}]
    plan = create_plan("ag_l", "run_1", specs, root=tmp_path)
    raw = json.loads(pathlib.Path(plan.file_path).read_text())

    assert raw["tasks"]["t1"]["task"] == "the task text"


# ---------------------------------------------------------------------------
# Atomic write: .tmp file is absent after replace.
# ---------------------------------------------------------------------------


def test_atomic_write_tmp_file_gone_after_save(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _minimal_specs(), root=tmp_path)

    target = pathlib.Path(plan.file_path)
    tmp = pathlib.Path(str(target) + ".tmp")

    assert target.exists(), "plan file must exist after create_plan"
    assert not tmp.exists(), ".tmp file must be gone after atomic replace"


def test_atomic_write_tmp_file_gone_after_status_transition(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _minimal_specs(), root=tmp_path)

    target = pathlib.Path(plan.file_path)
    tmp = pathlib.Path(str(target) + ".tmp")

    mark_status(plan, "t1", "in_flight", agent_id="ag_x")

    assert target.exists()
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# load_plan round-trips correctly.
# ---------------------------------------------------------------------------


def test_load_plan_round_trips(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _linear_specs(), root=tmp_path)

    loaded = load_plan(plan.file_path)

    assert loaded.plan_id == plan.plan_id
    assert loaded.owner_agent_id == plan.owner_agent_id
    assert loaded.run_task_id == plan.run_task_id
    assert set(loaded.tasks.keys()) == {"t1", "t2"}
    assert loaded.tasks["t1"].status == "ready"
    assert loaded.tasks["t2"].status == "blocked"


def test_load_plan_rebuilds_forward_deps(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _linear_specs(), root=tmp_path)
    loaded = load_plan(plan.file_path)

    assert "t2" in loaded.forward_deps["t1"]


# ---------------------------------------------------------------------------
# load_plans_for_run: skips .removed.json, loads active plans.
# ---------------------------------------------------------------------------


def test_load_plans_for_run_skips_removed(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _minimal_specs(), root=tmp_path)

    remove_plan_file(plan)

    result = load_plans_for_run("run_1", tmp_path)
    assert result == {}, "removed plan must not be loaded"


def test_load_plans_for_run_returns_active_plans(tmp_path: pathlib.Path) -> None:
    p1 = create_plan("ag_l1", "run_1", _minimal_specs(), root=tmp_path)
    p2 = create_plan("ag_l2", "run_1", _minimal_specs(), root=tmp_path)

    result = load_plans_for_run("run_1", tmp_path)

    assert p1.plan_id in result
    assert p2.plan_id in result


def test_load_plans_for_run_empty_dir(tmp_path: pathlib.Path) -> None:
    result = load_plans_for_run("run_nonexistent", tmp_path)
    assert result == {}


def test_load_plans_for_run_rebuilds_in_memory_state(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _linear_specs(), root=tmp_path)

    mark_status(plan, "t1", "done")

    result = load_plans_for_run("run_1", tmp_path)
    loaded = result[plan.plan_id]

    assert loaded.tasks["t1"].status == "done"
    assert loaded.tasks["t2"].status == "ready"


# ---------------------------------------------------------------------------
# remove_plan_file preserves renamed artifact.
# ---------------------------------------------------------------------------


def test_remove_plan_file_renames_to_removed_json(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _minimal_specs(), root=tmp_path)
    original = pathlib.Path(plan.file_path)

    remove_plan_file(plan)

    removed = original.parent / f"{plan.plan_id}.removed.json"
    assert not original.exists(), "original plan file must be gone"
    assert removed.exists(), ".removed.json artifact must be present"


def test_remove_plan_file_content_intact(tmp_path: pathlib.Path) -> None:
    plan = create_plan("ag_l", "run_1", _minimal_specs(), root=tmp_path)
    original_content = pathlib.Path(plan.file_path).read_text()

    remove_plan_file(plan)

    removed = (pathlib.Path(plan.file_path).parent / f"{plan.plan_id}.removed.json")
    assert json.loads(removed.read_text())["plan_id"] == plan.plan_id
