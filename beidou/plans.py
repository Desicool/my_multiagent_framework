"""Plan lifecycle: dataclasses, validation, persistence, and status transitions.

The orchestrator imports this module and delegates all plan operations to it.
Primitive bodies in core.py call orchestrator methods that in turn delegate here.

Plans are persisted under ~/.beidou/runs/{run_task_id}/plans/{plan_id}.json.
Each status transition rewrites the file atomically (write-tmp → fsync → replace
→ fsync-parent-dir). The file is an audit artifact and hot-reload cache, NOT a
crash-recovery mechanism — if the orchestrator process dies, the run dies.
"""
from __future__ import annotations

import collections
import json
import os
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class PlanValidationError(Exception):
    """Base for all plan validation errors."""


class EmptyPlanError(PlanValidationError):
    """Raised when a plan is declared with no tasks."""


class DuplicateTaskIdError(PlanValidationError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"duplicate task id: {task_id!r}")
        self.task_id = task_id


class SelfDepError(PlanValidationError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"task {task_id!r} depends on itself")
        self.task_id = task_id


class UnknownDepError(PlanValidationError):
    def __init__(self, task_id: str, bad_dep: str) -> None:
        super().__init__(
            f"task {task_id!r} references unknown dependency {bad_dep!r}"
        )
        self.task_id = task_id
        self.bad_dep = bad_dep


class CycleDetectedError(PlanValidationError):
    def __init__(self, chain: list[str]) -> None:
        super().__init__(f"cycle detected: {' -> '.join(chain)}")
        self.chain = chain


# ---------------------------------------------------------------------------
# Dataclasses.
# ---------------------------------------------------------------------------

TaskStatus = Literal["blocked", "ready", "in_flight", "done", "failed"]


@dataclass
class Task:
    id: str
    role: str
    skill: str
    task_text: str          # stored as "task" in JSON
    description: str
    model: Optional[str]
    depends_on: list[str]
    status: TaskStatus
    agent_id: Optional[str] = None
    spawned_at: Optional[float] = None
    completed_at: Optional[float] = None

    def to_json_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "skill": self.skill,
            "task": self.task_text,          # field renamed in wire format
            "description": self.description,
            "model": self.model,
            "depends_on": self.depends_on,
            "status": self.status,
            "agent_id": self.agent_id,
            "spawned_at": self.spawned_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_json_dict(cls, d: dict) -> "Task":
        return cls(
            id=d["id"],
            role=d["role"],
            skill=d["skill"],
            task_text=d["task"],             # wire field "task" → task_text
            description=d.get("description", ""),
            model=d.get("model"),
            depends_on=d.get("depends_on", []),
            status=d["status"],
            agent_id=d.get("agent_id"),
            spawned_at=d.get("spawned_at"),
            completed_at=d.get("completed_at"),
        )


@dataclass
class Plan:
    plan_id: str
    owner_agent_id: str
    run_task_id: str
    created_at: float
    tasks: dict[str, Task]
    file_path: str
    # Derived from tasks[*].depends_on at construction time; NOT persisted.
    forward_deps: dict[str, set[str]] = field(init=False)

    def __post_init__(self) -> None:
        fwd: dict[str, set[str]] = {tid: set() for tid in self.tasks}
        for tid, task in self.tasks.items():
            for dep in task.depends_on:
                if dep in fwd:
                    fwd[dep].add(tid)
        self.forward_deps = fwd

    def to_json_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "owner_agent_id": self.owner_agent_id,
            "run_task_id": self.run_task_id,
            "created_at": self.created_at,
            "tasks": {tid: t.to_json_dict() for tid, t in self.tasks.items()},
            # forward_deps is NOT persisted — derived at load time.
            # file_path is NOT persisted — injected from the fs path at load time.
        }

    @classmethod
    def from_json_dict(cls, d: dict, *, file_path: str = "") -> "Plan":
        """Deserialise from a JSON dict.

        ``file_path`` must be supplied by the caller (typically ``load_plan``)
        because the path is NOT stored in the file itself — it is derived from
        the filesystem location.
        """
        tasks = {
            tid: Task.from_json_dict(td)
            for tid, td in d["tasks"].items()
        }
        return cls(
            plan_id=d["plan_id"],
            owner_agent_id=d["owner_agent_id"],
            run_task_id=d["run_task_id"],
            created_at=d["created_at"],
            tasks=tasks,
            file_path=file_path,
        )


# ---------------------------------------------------------------------------
# DAG validation.
# ---------------------------------------------------------------------------


def validate_tasks(specs: list[dict]) -> dict[str, Task]:
    """Validate a list of raw task-spec dicts and return a ready task dict.

    Runs Kahn's topological sort to detect cycles. Initial statuses are
    assigned here: ``ready`` if ``depends_on`` is empty, else ``blocked``.

    Raises specific subclasses of ``PlanValidationError`` on invalid input.
    """
    if not specs:
        raise EmptyPlanError("plan must contain at least one task")

    # --- duplicate-id check ---------------------------------------------------
    seen_ids: set[str] = set()
    for spec in specs:
        tid = spec["id"]
        if tid in seen_ids:
            raise DuplicateTaskIdError(tid)
        seen_ids.add(tid)

    all_ids = seen_ids

    # --- per-task self-dep and unknown-dep checks -----------------------------
    for spec in specs:
        tid = spec["id"]
        for dep in spec.get("depends_on", []):
            if dep == tid:
                raise SelfDepError(tid)
            if dep not in all_ids:
                raise UnknownDepError(tid, dep)

    # --- Kahn's topological sort for cycle detection -------------------------
    in_degree: dict[str, int] = {tid: 0 for tid in all_ids}
    for spec in specs:
        for dep in spec.get("depends_on", []):
            in_degree[spec["id"]] += 1

    queue: collections.deque[str] = collections.deque(
        tid for tid, deg in in_degree.items() if deg == 0
    )
    processed: list[str] = []
    # Build adjacency: dep → set of dependents (forward edges)
    fwd: dict[str, list[str]] = {tid: [] for tid in all_ids}
    for spec in specs:
        for dep in spec.get("depends_on", []):
            fwd[dep].append(spec["id"])

    while queue:
        node = queue.popleft()
        processed.append(node)
        for dependent in fwd[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(processed) != len(all_ids):
        # There's a cycle. Walk one unprocessed node's depends_on to recover
        # the chain.
        remaining = {tid for tid in all_ids if tid not in set(processed)}
        start = next(iter(remaining))
        chain = _trace_cycle(start, {s["id"]: s.get("depends_on", []) for s in specs}, remaining)
        raise CycleDetectedError(chain)

    # --- Assemble Task objects -----------------------------------------------
    tasks: dict[str, Task] = {}
    for spec in specs:
        deps = spec.get("depends_on", [])
        status: TaskStatus = "blocked" if deps else "ready"
        tasks[spec["id"]] = Task(
            id=spec["id"],
            role=spec["role"],
            skill=spec["skill"],
            task_text=spec.get("task", spec.get("task_text", "")),
            description=spec.get("description", ""),
            model=spec.get("model"),
            depends_on=deps,
            status=status,
        )
    return tasks


def _trace_cycle(
    start: str,
    dep_map: dict[str, list[str]],
    candidates: set[str],
) -> list[str]:
    """Walk the dep_map from start to find and return a cycle chain."""
    path: list[str] = [start]
    visited: set[str] = {start}
    current = start
    while True:
        # Pick the first dep that is itself in the cycle candidates
        next_node = None
        for dep in dep_map.get(current, []):
            if dep in candidates:
                next_node = dep
                break
        if next_node is None:
            # Shouldn't happen on a valid cyclic graph; bail out with what we have.
            break
        if next_node in visited:
            # Close the cycle chain
            idx = path.index(next_node)
            return path[idx:] + [next_node]
        path.append(next_node)
        visited.add(next_node)
        current = next_node
    return path


# ---------------------------------------------------------------------------
# Path helpers.
# ---------------------------------------------------------------------------


def default_plan_path(
    run_task_id: str,
    plan_id: str,
    *,
    root: Optional[pathlib.Path] = None,
) -> pathlib.Path:
    """Return the canonical path for a plan file.

    root defaults to ``~/.beidou/runs`` but can be overridden in tests.
    """
    if root is None:
        root = pathlib.Path.home() / ".beidou" / "runs"
    return root / run_task_id / "plans" / f"{plan_id}.json"


# ---------------------------------------------------------------------------
# Atomic persistence.
# ---------------------------------------------------------------------------


def save_plan(plan: Plan) -> None:
    """Atomically write the plan to disk.

    Sequence: write to .tmp → fsync → os.replace → fsync parent dir.
    The parent-dir fsync is POSIX-only and best-effort (some filesystems
    reject it; we catch OSError rather than propagate).
    """
    target = pathlib.Path(plan.file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = str(target) + ".tmp"

    payload = json.dumps(plan.to_json_dict(), indent=2).encode()

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(tmp_path, str(target))

    if os.name == "posix":
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


def load_plan(file_path: str) -> Plan:
    """Load a plan from disk and recompute forward_deps.

    ``file_path`` is injected into the returned Plan; it is NOT stored in
    the file (which exactly matches the documented on-disk schema).
    """
    resolved = str(pathlib.Path(file_path).resolve())
    with open(resolved, encoding="utf-8") as fh:
        data = json.load(fh)
    return Plan.from_json_dict(data, file_path=resolved)


# ---------------------------------------------------------------------------
# Status transitions.
# ---------------------------------------------------------------------------


def mark_status(
    plan: Plan,
    task_id: str,
    new_status: str,
    *,
    agent_id: Optional[str] = None,
) -> list[str]:
    """Update task status in-memory, persist atomically, return newly-ready ids.

    Newly-ready ids are those whose every dependency just reached ``done``
    as a direct result of this transition. Only non-empty when
    ``new_status == "done"``.

    Caller is responsible for holding any necessary concurrency lock; this
    function is synchronous and not thread-safe on its own.
    """
    task = plan.tasks[task_id]
    task.status = new_status  # type: ignore[assignment]

    if agent_id is not None:
        task.agent_id = agent_id

    now = time.time()
    if new_status == "in_flight" and task.spawned_at is None:
        task.spawned_at = now
    elif new_status in ("done", "failed"):
        task.completed_at = now

    newly_ready: list[str] = []
    if new_status == "done":
        for dependent_id in plan.forward_deps.get(task_id, set()):
            dep_task = plan.tasks[dependent_id]
            if dep_task.status != "blocked":
                # Already unblocked or further along; skip.
                continue
            if all(plan.tasks[d].status == "done" for d in dep_task.depends_on):
                dep_task.status = "ready"
                newly_ready.append(dependent_id)

    save_plan(plan)
    return newly_ready


# ---------------------------------------------------------------------------
# Removal.
# ---------------------------------------------------------------------------


def remove_plan_file(plan: Plan) -> None:
    """Rename the plan file to {plan_id}.removed.json (audit history preserved).

    The renamed file stays on disk; ``load_plans_for_run`` skips it.
    """
    src = pathlib.Path(plan.file_path)
    dst = src.parent / f"{plan.plan_id}.removed.json"
    os.replace(str(src), str(dst))


# ---------------------------------------------------------------------------
# Startup rebuild.
# ---------------------------------------------------------------------------


def load_plans_for_run(
    run_task_id: str,
    plans_root: pathlib.Path,
) -> dict[str, Plan]:
    """Walk plans_root/run_task_id/plans/ and load every active plan.

    Active means: filename ends with ``.json`` but NOT ``.removed.json``.
    Returns a dict keyed by plan_id.
    """
    plans_dir = plans_root / run_task_id / "plans"
    result: dict[str, Plan] = {}
    if not plans_dir.is_dir():
        return result
    for entry in plans_dir.iterdir():
        name = entry.name
        if not name.endswith(".json"):
            continue
        if name.endswith(".removed.json"):
            continue
        try:
            plan = load_plan(str(entry))
            result[plan.plan_id] = plan
        except Exception:
            # Corrupt or partial file — skip rather than crash at startup.
            pass
    return result


# ---------------------------------------------------------------------------
# Factory helper (convenience for orchestrator's declare_plan path).
# ---------------------------------------------------------------------------


def create_plan(
    owner_agent_id: str,
    run_task_id: str,
    specs: list[dict],
    *,
    plan_id: Optional[str] = None,
    root: Optional[pathlib.Path] = None,
) -> Plan:
    """Validate specs, build a Plan, and persist it atomically.

    Raises ``PlanValidationError`` subclasses on invalid input — in that
    case nothing is written to disk.
    """
    tasks = validate_tasks(specs)
    pid = plan_id or str(uuid.uuid4())
    file_path = str(default_plan_path(run_task_id, pid, root=root))
    plan = Plan(
        plan_id=pid,
        owner_agent_id=owner_agent_id,
        run_task_id=run_task_id,
        created_at=time.time(),
        tasks=tasks,
        file_path=file_path,
    )
    save_plan(plan)
    return plan
