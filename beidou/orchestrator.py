"""Concrete Orchestrator implementing ``beidou.primitives.core.Orchestrator``.

Owns the agent registry, team graph, per-agent inboxes, per-agent spawn
locks, emission of observability events, and the resume-not-terminate
recovery policy for contract violations.

Every rule enforced here is load-bearing; see:

* ``docs/orchestration.md`` — team graph, self-lead invariant, termination
  cascade (leaf-first, root-last).
* ``docs/agent-runtime.md`` — persistent-agent invariant; resume-not-terminate
  on contract violations; N=3 escalation to leader (gateway for root).
* ``docs/limits.md`` — every numeric boundary this file references.
* ``docs/observability.md`` — event catalogue.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import json

from beidou import db as _db
from beidou import plans as _plans
from beidou import sdk_agent
from beidou.events import EventEmitter
from beidou.primitives.core import (
    CONTRACT_STRIKES,
    CRASH_STRIKES,
    INBOX_CAP,
    Message,
    Peer,
    PrimitiveError,
)
from beidou.questions import QuestionRegistry, render_prompt_text
from beidou.sdk_agent import RunResult, SpawnSpec
from beidou.workspace import agent_workspace, team_workspace

if TYPE_CHECKING:  # pragma: no cover
    from beidou.gateways.base import BaseGateway


# Sentinel leader-id for the root agent.  The root agent has no team and no
# leader; USER_SENTINEL is stored as ``leader_id`` in any context object that
# needs a "who leads the root?" answer (e.g. spawn context template_vars).
USER_SENTINEL = "__user__"

# Watchdog tunables — implementation constants, NOT in docs/limits.md.
WATCHDOG_INTERVAL_S = 30.0
REVIEW_PING_INTERVAL_S = 60.0
IDLE_NUDGE_S = 120.0
MAX_PINGS_BEFORE_ESCALATION = 3
TERMINATE_GRACE_S = 60.0


# ---------------------------------------------------------------------------
# Name helpers.
# ---------------------------------------------------------------------------


def _slug_role(role: str) -> str:
    """Convert a role string into a clean slug component.

    Steps: lowercase → replace runs of [^a-z0-9] with '-' → strip
    leading/trailing '-' → collapse repeated '-' → truncate to 24 chars →
    fall back to 'agent' if the result is empty.
    """
    s = role.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = re.sub(r"-{2,}", "-", s)
    s = s[:24]
    return s or "agent"


# ---------------------------------------------------------------------------
# Records.
# ---------------------------------------------------------------------------


@dataclass
class AgentRecord:
    agent_id: str
    task_id: str
    team_id: str | None                # team this agent is a MEMBER of; None for teamless root
    role: str
    skill_name: str
    model: Optional[str]
    inbox: asyncio.Queue
    # Field name is `create_team_lock` for backward-compat with test fixtures;
    # access via the `spawn_lock` property (limits.md #5 rename).
    create_team_lock: asyncio.Lock
    name: str = ""                    # human-readable display name, e.g. "frontend-engineer-a3b2"
    last_status: str = "working"
    last_status_detail: str = ""
    contract_strikes: int = 0
    run_task: Optional[asyncio.Task] = None
    terminate_consumed: bool = False
    terminate_grace_deadline: Optional[float] = None
    total_tokens: int = 0
    # Completion-review state (Phase 2 foundation — bd issue 8z3).
    completion_pending: bool = False
    completion_pending_ts: Optional[float] = None
    last_progress_ts: float = field(default_factory=time.time)
    last_drain_ts: Optional[float] = None
    review_ping_count: int = 0
    # Watchdog fields (bd issue qj2).
    inflight_tools: int = 0
    idle_nudge_count: int = 0
    # Plan-task association. Set when this agent was spawned via spawn_for_task;
    # stays None for the root agent and for legacy create_team-spawned members.
    plan_task_id: Optional[str] = None
    plan_id: Optional[str] = None
    # Crash recovery (agent-runtime.md §5.1).
    crash_strikes: int = 0
    last_session_id: Optional[str] = None
    last_crash_stderr: str = ""

    @property
    def spawn_lock(self) -> asyncio.Lock:
        """Alias for create_team_lock (limits.md #5 rename; field kept for test compat)."""
        return self.create_team_lock


@dataclass
class TeamRecord:
    team_id: str
    name: str
    leader_id: str                    # MUST equal caller_id of create_team / spawn_for_task
    depth: int                        # 0 for root
    member_ids: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    parent_team_id: Optional[str] = None
    # `task` is dead metadata — only written by legacy create_team callers,
    # never read by the orchestrator. Kept as optional with a default so
    # existing test fixtures still pass; will be removed in a follow-up commit.
    task: str = ""


# ---------------------------------------------------------------------------
# Orchestrator.
# ---------------------------------------------------------------------------


class Orchestrator:
    """Concrete orchestrator driving the multi-agent system.

    Instances are single-event-loop. No thread-safety guarantees beyond what
    asyncio provides; every public method either runs synchronously on the
    loop or is an ``async`` coroutine.
    """

    def __init__(
        self,
        task_id: str,
        emitter: Optional[EventEmitter] = None,
        skill_root: Optional[Path] = None,
        gateway: "BaseGateway | None" = None,
        *,
        default_model: Optional[str] = None,
        project_workspace: Optional[Path] = None,
    ) -> None:
        self.task_id = task_id
        self.emitter = emitter if emitter is not None else EventEmitter(task_id)
        self.skill_root = Path(skill_root) if skill_root is not None else Path.cwd()
        self.gateway = gateway
        self._default_model = default_model
        self.project_workspace = project_workspace if project_workspace is not None else Path.cwd()

        self._agents: dict[str, AgentRecord] = {}
        self._teams: dict[str, TeamRecord] = {}
        self._root_id: Optional[str] = None
        self._root_terminated: bool = False

        # Originating user task — captured at run_root time and propagated to
        # every spawned agent as their first user-role message. Distinct from
        # the role's per-spawn `description` (which is the role-specific scope
        # rendered into the system prompt via {role_description}).
        self._user_task: str = ""

        # Background emitter tasks — keep refs so they're not GC'd mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()

        # Watchdog (bd issue qj2).
        self._watchdog_task: Optional[asyncio.Task] = None
        self._shutting_down: bool = False

        # Lazily-created per-agent spawn locks (limits.md #5: one concurrent
        # team-spawn per agent; renamed from create_team_lock).
        self._locks: dict[str, asyncio.Lock] = {}

        # Question registry — replaces QuestionBroker.
        self._questions: QuestionRegistry = QuestionRegistry()

        # Plan registry.
        self._plans: dict[str, _plans.Plan] = {}          # plan_id → Plan
        self._active_plan_by_agent: dict[str, str] = {}   # agent_id → plan_id
        # Per-agent plan locks: serialise plan mutations independently of spawn.
        self._plan_locks: dict[str, asyncio.Lock] = {}

        # Populate from disk (hot-reload: picks up plans declared in this OS
        # lifetime before a dev-server restart).
        self._load_plans_for_run()

        # Per-agent assistant text tracking for PostToolUse hook.
        # Outer key: caller_id. Inner key: tool_use_id -> assistant text from that turn.
        # Also maintains most_recent_text per agent as a fallback.
        self._assistant_text_by_tool: dict[str, dict[str, str]] = {}
        self._most_recent_assistant_text: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Internal: name generation
    # ------------------------------------------------------------------

    def _make_unique_name(self, role: str) -> str:
        """Generate a display name ``<slug>-<4hex>`` unique among in-flight agents.

        On collision (max 3 retries), falls back to an 8-hex suffix so correctness
        is always preserved — ``agent_id`` remains the stable join key.
        """
        slug = _slug_role(role)
        existing_names = {rec.name for rec in self._agents.values()}
        for _ in range(3):
            candidate = f"{slug}-{uuid.uuid4().hex[:4]}"
            if candidate not in existing_names:
                return candidate
        # Fallback: 8-hex suffix virtually eliminates collision.
        return f"{slug}-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    # Protocol: registry lookups
    # ------------------------------------------------------------------

    def agent_exists(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def agent_task(self, agent_id: str) -> str:
        return self._agents[agent_id].task_id

    def agent_team(self, agent_id: str) -> str | None:
        """Return the team_id for agent_id, or None for teamless agents (e.g. root)."""
        return self._agents[agent_id].team_id

    def leader_of(self, team_id: str) -> str:
        return self._teams[team_id].leader_id

    def teams_led_by(self, agent_id: str) -> list[str]:
        return [tid for tid, t in self._teams.items() if t.leader_id == agent_id]

    def team_depth(self, team_id: str | None) -> int:
        """Return the depth of team_id, or 0 for None (teamless = depth 0)."""
        if team_id is None:
            return 0
        return self._teams[team_id].depth

    def team_members(self, team_id: str | None) -> list[str]:
        """Return member ids for team_id, or empty list for None (teamless agent)."""
        if team_id is None:
            return []
        return list(self._teams[team_id].member_ids)

    # --- Completion-review accessors (used by list_pending_reviews) --------

    def agent_skill_name(self, agent_id: str) -> str:
        return self._agents[agent_id].skill_name

    def agent_completion_pending(self, agent_id: str) -> bool:
        return self._agents[agent_id].completion_pending

    def agent_completion_pending_ts(self, agent_id: str) -> Optional[float]:
        return self._agents[agent_id].completion_pending_ts

    def agent_last_status_detail(self, agent_id: str) -> str:
        return self._agents[agent_id].last_status_detail

    def agent_name(self, agent_id: str) -> str | None:
        rec = self._agents.get(agent_id)
        return rec.name if rec is not None else None

    def peer_snapshot(self, agent_id: str, scope: str) -> list[Peer]:
        if agent_id not in self._agents:
            return []
        me = self._agents[agent_id]
        out: list[Peer] = []

        def _peer(aid: str) -> Peer:
            a = self._agents[aid]
            return Peer(
                agent_id=aid,
                role=a.role,
                team_id=a.team_id,
                status=a.last_status,
                is_leader_of=self.teams_led_by(aid),
                name=a.name or None,
            )

        if scope == "team":
            if me.team_id is None:
                # Teamless agent (root) has no team-mates.
                return out
            for mid in self.team_members(me.team_id):
                if mid == agent_id or mid not in self._agents:
                    continue
                out.append(_peer(mid))
            # Include the team leader (leader is not in member_ids in
            # production -- spawn_team keeps them separate).
            team = self._teams.get(me.team_id)
            if team is not None:
                lid = team.leader_id
                already_in = {p.agent_id for p in out}
                if (lid is not None
                        and lid != agent_id          # self-exclusion
                        and lid != USER_SENTINEL     # skip sentinel placeholder
                        and lid in self._agents      # leader must exist
                        and lid not in already_in):  # defensive dedup
                    la = self._agents[lid]
                    out.append(Peer(
                        agent_id=lid,
                        role="leader",
                        team_id=la.team_id,
                        status=la.last_status,
                        is_leader_of=self.teams_led_by(lid),
                        name=la.name or None,
                    ))
        elif scope == "children":
            seen: set[str] = set()
            for tid in self.teams_led_by(agent_id):
                for mid in self.team_members(tid):
                    if mid in seen or mid not in self._agents:
                        continue
                    seen.add(mid)
                    out.append(_peer(mid))
        elif scope == "all":
            for aid, a in self._agents.items():
                if aid == agent_id or a.task_id != me.task_id:
                    continue
                out.append(_peer(aid))
        return out

    # ------------------------------------------------------------------
    # Protocol: inbox
    # ------------------------------------------------------------------

    async def inbox_put(self, recipient: str, msg: Message) -> None:
        """Enqueue ``msg`` onto ``recipient``'s inbox.

        Auto-bypasses the cap when the sender is Beidou itself (``from_id ==
        "beidou"``), which covers terminate sentinels — those must not be
        lost to a full inbox. All other senders get ``inbox_full`` when at
        the cap.

        When ``msg`` is a terminate sentinel and ``recipient`` leads
        sub-teams, the sentinel is also posted to every member of those
        teams. This makes the cascade a runtime guarantee — the prior
        prose-driven cascade no longer fires because sdk_agent's outer
        loop intercepts terminate before the agent sees it.
        """
        if recipient not in self._agents:
            raise PrimitiveError("unknown_recipient", f"no such agent: {recipient}", to=recipient)
        rec = self._agents[recipient]
        if msg.from_id != "beidou" and rec.inbox.qsize() >= INBOX_CAP:
            raise PrimitiveError(
                "inbox_full",
                f"{recipient}'s inbox is at cap ({INBOX_CAP})",
                to=recipient,
                cap=INBOX_CAP,
            )
        rec.inbox.put_nowait(msg)

        # Update progress timestamp on every inbox arrival (agent is about to
        # wake and act).
        rec.last_progress_ts = time.time()

        # Terminate sentinel: possibly emit completion.approved if pending, then
        # cascade to children.
        if msg.kind == "terminate":
            # Stamp grace deadline idempotently — re-posting terminate does NOT
            # extend an already-running countdown.
            if rec.terminate_grace_deadline is None:
                rec.terminate_grace_deadline = time.time() + TERMINATE_GRACE_S

            if rec.completion_pending:
                duration_ms: Optional[float] = (
                    (time.time() - rec.completion_pending_ts) * 1000
                    if rec.completion_pending_ts is not None
                    else None
                )
                leader_id_for_event = (
                    USER_SENTINEL if rec.team_id is None
                    else self.leader_of(rec.team_id)
                )
                self.emit_event(
                    "completion.approved",
                    {
                        "agent_id": rec.agent_id,
                        "leader_id": leader_id_for_event,
                        "completion_pending_duration_ms": duration_ms,
                        "ts": time.time(),
                    },
                )
                # Clear the pending state AFTER emitting (so duration_ms is
                # computed against the pre-cleared ts).  Without this, the
                # AgentRecord persists with completion_pending=True after the
                # child is terminated, and the leader's on_review_gate hook
                # keeps denying every subsequent tool call (tsk_e92d672b repro).
                rec.completion_pending = False
                rec.completion_pending_ts = None
                rec.review_ping_count = 0
                rec.idle_nudge_count = 0

            for tid in self.teams_led_by(recipient):
                for mid in list(self._teams[tid].member_ids):
                    if mid == recipient or mid not in self._agents:
                        continue
                    cascade = Message(
                        from_id="beidou",
                        content="__terminate__",
                        ts=time.time(),
                        message_id=str(uuid.uuid4()),
                        kind="terminate",
                    )
                    await self.inbox_put(mid, cascade)
            return

        # Non-terminate message: clear completion_pending when the sender is
        # the agent's direct leader or the human user gateway ("user").
        # Exclude system messages (from_id == "beidou") explicitly.
        if msg.from_id != "beidou" and rec.completion_pending:
            leader_id_for_msg = (
                USER_SENTINEL if rec.team_id is None
                else self.leader_of(rec.team_id)
            )
            is_from_leader = msg.from_id == leader_id_for_msg
            is_from_user = msg.from_id == "user"
            if is_from_leader or is_from_user:
                rec.completion_pending = False
                rec.completion_pending_ts = None
                rec.review_ping_count = 0
                content_preview = (msg.content or "")[:200]
                self.emit_event(
                    "completion.rework",
                    {
                        "agent_id": rec.agent_id,
                        "leader_id": leader_id_for_msg,
                        "content_preview": content_preview,
                        "ts": time.time(),
                    },
                )

    def inbox_size(self, agent_id: str) -> int:
        return self._agents[agent_id].inbox.qsize()

    def queue_for(self, agent_id: str) -> asyncio.Queue:
        """Return the per-agent asyncio.Queue for direct use by sdk_agent's outer loop.

        The returned queue is the same object that ``inbox_put`` / ``deliver_message``
        push onto. sdk_agent's ``input_stream`` parks on ``await queue.get()`` between
        turns; terminate sentinels end the stream by returning from the generator.
        """
        return self._agents[agent_id].inbox

    # ------------------------------------------------------------------
    # Protocol: team spawn helpers
    # ------------------------------------------------------------------

    def _provision_team_workspace(self, team_id: str) -> Path:
        """Allocate workspace dir and provision bundled skills into it."""
        workspace_path = team_workspace(self.project_workspace, self.task_id, team_id)
        from beidou.skills.loader import provision_skills
        provision_skills(workspace_path, skill_root=self.skill_root)
        return workspace_path

    def _create_team_record(
        self,
        team_id: str,
        name: str,
        leader_id: str,
        depth: int,
        parent_team_id: Optional[str],
        member_ids: list[str],
        rules: list[str],
    ) -> "TeamRecord":
        """Build and register a TeamRecord. Returns it for convenience."""
        rec = TeamRecord(
            team_id=team_id,
            name=name,
            leader_id=leader_id,
            depth=depth,
            member_ids=member_ids,
            rules=list(rules),
            parent_team_id=parent_team_id,
        )
        self._teams[team_id] = rec
        return rec

    def _register_member_and_launch(
        self,
        team_id: str,
        role_dict: dict,
        per_member_task: str,
        leader_id: str,
        workspace_path: Path,
        team_name: str,
        *,
        plan_task_id: Optional[str] = None,
        plan_id: Optional[str] = None,
    ) -> tuple[str, "AgentRecord", "SpawnSpec"]:
        """Create an AgentRecord for one role and build its SpawnSpec.

        Does NOT launch the asyncio task — caller does that after emitting
        team_created (preserving the team_created-before-agent_spawned ordering
        invariant). Returns (agent_id, rec, spec).
        """
        agent_id = f"ag_{uuid.uuid4().hex[:8]}"
        role_name = role_dict.get("role", "member")
        role_desc = role_dict.get("description", "")
        model = role_dict.get("model") or self._default_model
        skill_name = role_dict.get("skill") or role_dict.get("template", "")
        agent_name = self._make_unique_name(role_name)
        run_task_id = self.agent_task(leader_id)

        rec = AgentRecord(
            agent_id=agent_id,
            task_id=run_task_id,
            team_id=team_id,
            role=role_name,
            skill_name=skill_name,
            model=model,
            inbox=asyncio.Queue(),
            create_team_lock=asyncio.Lock(),
            name=agent_name,
            plan_task_id=plan_task_id,
            plan_id=plan_id,
        )
        self._register_agent_record(rec)

        if plan_task_id:
            _artifacts = str(Path(self.project_workspace) / "artifacts" / plan_task_id)
            _header = (
                "[TASK ASSIGNMENT]\n"
                f"plan_task_id: {plan_task_id}\n"
                f"artifacts_path: {_artifacts}\n"
                "[/TASK ASSIGNMENT]\n\n"
            )
            per_member_task = _header + per_member_task

        spec = SpawnSpec(
            caller_id=agent_id,
            skill_name=skill_name,
            skill_root=self.skill_root,
            task=per_member_task,
            model=model,
            template_vars={
                "role": role_name,
                "role_description": role_desc,
                "team_name": team_name,
                "workspace_path": str(workspace_path),
                "project_workspace_path": str(self.project_workspace),
                "leader_id": leader_id,
            },
            cwd=str(workspace_path),
        )
        return agent_id, rec, spec

    async def spawn_team(
        self,
        leader_id: str,
        name: str,
        task: str,
        roles: list[dict],
        rules: list[str],
    ) -> dict:
        """Create a sub-team and launch SDK runs for each member.

        Called from ``create_team`` primitive after fan-out / depth / lock
        checks pass. Self-lead invariant: ``leader_id`` comes in as the
        caller's id from the primitive; we record it verbatim.

        Preserves the team_created-before-agent_spawned event ordering invariant:
        team_created is emitted synchronously before any run tasks are launched.
        """
        if leader_id not in self._agents:
            raise PrimitiveError("unknown_agent", f"no such leader: {leader_id}")

        parent_team_id = self.agent_team(leader_id)
        new_depth = self.team_depth(parent_team_id) + 1

        team_id = f"tm_{uuid.uuid4().hex[:8]}"
        members_out: list[dict] = []
        new_records: list[tuple[AgentRecord, SpawnSpec]] = []

        # Pre-validate all skills before allocating ids; surface
        # unknown_skill without half-spawning.
        for r in roles:
            skill = r.get("skill") or r.get("template")  # accept deprecated "template" key
            if not skill:
                raise PrimitiveError("unknown_skill", "role missing 'skill'", role=r)
            try:
                from beidou.skills.loader import load_skill, SkillError
                load_skill(self.skill_root, skill)
            except SkillError as exc:
                raise PrimitiveError(
                    "unknown_skill",
                    f"cannot resolve skill {skill!r}: {exc}",
                    skill=skill,
                )
            except Exception:
                raise

        workspace_path = self._provision_team_workspace(team_id)

        for r in roles:
            agent_id, rec, spec = self._register_member_and_launch(
                team_id, r,
                per_member_task=self._user_task or task,
                leader_id=leader_id,
                workspace_path=workspace_path,
                team_name=name,
            )
            members_out.append({"agent_id": agent_id, "role": rec.role, "name": rec.name})
            new_records.append((rec, spec))

        # Register TeamRecord and emit team_created BEFORE launching runs
        # so any early LLM call from a member sees the right topology and
        # event consumers see team_created before any agent_spawned events.
        self._create_team_record(
            team_id, name, leader_id, new_depth, parent_team_id,
            [m["agent_id"] for m in members_out], rules,
        )

        self.emit_event(
            "team_created",
            {
                "new_team_id": team_id,
                "team_name": name,
                "leader_agent_id": leader_id,
                "parent_team_id": parent_team_id,
                "depth": new_depth,
                "members": members_out,
                "ts": time.time(),
            },
        )

        for rec, spec in new_records:
            rec.run_task = asyncio.create_task(
                self._run_agent_with_policy(rec, spec),
                name=f"agent-{rec.agent_id}",
            )

        return {"team_id": team_id, "members": members_out}

    async def spawn_lock(self, agent_id: str) -> asyncio.Lock:
        """Return the per-agent spawn lock (limits.md #5: one concurrent team-spawn per agent).

        The error code ``concurrent_create_team`` is preserved for the existing
        create_team path; it will be renamed to ``concurrent_spawn`` once
        primitives are migrated.
        """
        lock = self._locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_id] = lock
        return lock

    # Back-compat alias so primitives/core.py (which calls orch.create_team_lock)
    # continues to work until its replacement commit lands.
    async def create_team_lock(self, agent_id: str) -> asyncio.Lock:
        return await self.spawn_lock(agent_id)

    # ------------------------------------------------------------------
    # Protocol: observability / gateway
    # ------------------------------------------------------------------

    def emit_event(self, name: str, payload: dict) -> None:
        """Schedule an async emit to the underlying EventEmitter.

        Also intercepts ``turn.usage`` to accumulate per-agent tokens and
        trigger the limits.md #8 ceiling recommendation.
        """
        if name == "turn.usage":
            self._observe_turn_usage(payload)

        # Pull out the standard keys the EventEmitter expects positionally.
        kwargs = dict(payload)
        agent_id = (
            kwargs.pop("agent_id", None)
            or kwargs.pop("caller_id", None)
            or ""
        )
        kwargs.setdefault("caller_id", agent_id)
        team_id = kwargs.pop("team_id", None)
        if not team_id and agent_id in self._agents:
            team_id = self._agents[agent_id].team_id

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop — best-effort synchronous write via the JSONL path.
            # Happens in unit tests that call emit_event without a loop.
            self._emit_sync_fallback(name, agent_id, team_id, kwargs)
            return

        task = loop.create_task(self.emitter.emit(name, agent_id, team_id, **kwargs))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _emit_sync_fallback(
        self,
        name: str,
        agent_id: str,
        team_id: Optional[str],
        kwargs: dict,
    ) -> None:
        import json
        try:
            payload = {
                "ts": time.time(),
                "event": name,
                "task_id": self.task_id,
                "agent_id": agent_id,
                "team_id": team_id,
                **kwargs,
            }
            with self.emitter._path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        except Exception:
            pass

    def _observe_turn_usage(self, payload: dict) -> None:
        caller_id = payload.get("caller_id") or payload.get("agent_id")
        if not caller_id or caller_id not in self._agents:
            return
        rec = self._agents[caller_id]
        in_tok = int(payload.get("input_tokens") or 0)
        out_tok = int(payload.get("output_tokens") or 0)
        cache_c = int(payload.get("cache_creation_input_tokens") or 0)
        cache_r = int(payload.get("cache_read_input_tokens") or 0)
        rec.total_tokens += in_tok + out_tok + cache_c + cache_r

    def _schedule_emit(
        self,
        name: str,
        agent_id: str,
        team_id: Optional[str],
        kwargs: dict,
    ) -> None:
        """Emit without re-entering ``emit_event`` (avoids recursion)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._emit_sync_fallback(name, agent_id, team_id, kwargs)
            return
        task = loop.create_task(self.emitter.emit(name, agent_id, team_id, **kwargs))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def gateway_ask_user(
        self,
        caller_id: str,
        question: str,
        context: Optional[str],
    ) -> str:
        """Legacy string-only path used by sdk_agent.py's AskUserQuestion hook.

        Wraps the question as a single free-text sub-question and delegates to
        gateway_ask_user_structured, then returns the answer_text string.
        """
        questions = [{"question": question, "header": "", "multiSelect": False, "options": []}]
        result = await self.gateway_ask_user_structured(caller_id, questions, context)
        if isinstance(result, dict):
            return result.get("answer_text", "")
        return str(result)

    async def gateway_ask_user_structured(
        self,
        caller_id: str,
        questions: list[dict],
        context: Optional[str],
    ) -> dict:
        """Direct-to-user structured path. Bypasses the leader chain — used by
        watchdog escalations, root completion review, and any caller that
        explicitly wants the user to see the question.

        Routes straight to the user gateway (parent_id=None).
        Returns ``{"answers": [...], "answer_text": "..."}`` once resolved.
        """
        if self.gateway is None:
            raise PrimitiveError("gateway_unavailable", "no human gateway registered")
        qid, future = await self.post_question(
            asker_id=caller_id,
            parent_id=None,
            questions=questions,
            context_hint=context,
        )
        self.emit_event(
            "question_asked",
            {
                "agent_id": caller_id,
                "qid": qid,
                "asker": caller_id,
                "holder": None,
                "prompt": render_prompt_text(questions)[:200],
                "questions": questions,
            },
        )
        try:
            return await future
        finally:
            self._questions.pop(qid)

    async def gateway_ask_via_chain(
        self,
        caller_id: str,
        questions: list[dict],
        context: Optional[str],
    ) -> dict:
        """Leader-chain structured path used by agent-originated ``ask_user``.

        Routes the question to the caller's team leader's inbox first; the
        leader can ``answer_question`` or ``escalate_question``. Falls back to
        direct-to-user when the caller has no leader (root) or its leader is
        the user sentinel.

        Returns ``{"answers": [...], "answer_text": "..."}`` once the question
        is resolved (by a leader, an upstream leader after escalation, or the
        user gateway terminal).
        """
        if self.gateway is None:
            raise PrimitiveError("gateway_unavailable", "no human gateway registered")
        parent_id = self.parent_for_chain(caller_id)
        qid, future = await self.post_question(
            asker_id=caller_id,
            parent_id=parent_id,
            questions=questions,
            context_hint=context,
        )
        self.emit_event(
            "question_asked",
            {
                "agent_id": caller_id,
                "qid": qid,
                "asker": caller_id,
                "holder": parent_id,
                "prompt": render_prompt_text(questions)[:200],
                "questions": questions,
            },
        )
        try:
            return await future
        finally:
            self._questions.pop(qid)

    def is_gateway_available(self) -> bool:
        return self.gateway is not None and hasattr(self.gateway, "ask")

    # ------------------------------------------------------------------
    # Question routing — replaces QuestionBroker
    # ------------------------------------------------------------------

    def parent_for_chain(self, caller_id: str) -> Optional[str]:
        """Return caller's next-up reviewer for question-chain routing, or None.

        Returns None when the caller is root (no team), or when the team's
        leader is the user sentinel (meaning the next hop is the user).
        """
        rec = self._agents.get(caller_id)
        if rec is None or rec.team_id is None:
            return None
        team = self._teams.get(rec.team_id)
        if team is None:
            return None
        leader = getattr(team, "leader_id", None)
        if leader is None or leader == USER_SENTINEL:
            return None
        return leader

    async def post_question(
        self,
        *,
        asker_id: str,
        parent_id: Optional[str],
        questions: list[dict],
        context_hint: Optional[str],
    ) -> tuple[str, asyncio.Future]:
        """First post (from ask_user primitive). Registers, dispatches, returns (qid, future).

        The caller awaits the returned future and handles cleanup in a finally block.
        DB insert is non-blocking (best-effort executor).
        """
        qid, future = self._questions.register(asker_id, questions, context_hint, parent_id)
        pq = self._questions.get(qid)
        # Non-blocking DB insert — mirrors QuestionBroker.ask (inbox.py:104-118).
        try:
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _db.insert_question(
                    qid=qid,
                    task_id=self.task_id,
                    asker_agent_id=asker_id,
                    prompt=render_prompt_text(questions),
                    context_hint=context_hint,
                    chain_json=json.dumps(pq.chain),
                    created_at=pq.created_at,
                ),
            )
        except Exception:
            pass
        await self._deliver_question(
            qid, target=parent_id, sender=asker_id,
            questions=questions, context_hint=context_hint,
            chain=pq.chain, escalation=False,
        )
        return qid, future

    async def forward_question(
        self,
        *,
        qid: str,
        by_id: str,
        new_target_id: Optional[str],
        reason: str,
    ) -> dict:
        """Re-post (from escalate_question primitive). Adds to chain, dispatches, returns immediately.

        NO future is awaited here — bubble model: the escalator forwards and returns.
        """
        pq = self._questions.get(qid)
        if pq is None or pq.future.done():
            return {"ok": False, "reason": "stale"}
        self._questions.add_chain_hop(qid, new_target_id if new_target_id is not None else "USER")
        await self._deliver_question(
            qid, target=new_target_id, sender=by_id,
            questions=pq.questions, context_hint=None,
            chain=pq.chain, escalation=True, reason=reason,
        )
        return {"ok": True, "qid": qid, "new_holder": new_target_id}

    async def _deliver_question(
        self,
        qid: str,
        *,
        target: Optional[str],
        sender: str,
        questions: list[dict],
        context_hint: Optional[str],
        chain: list[str],
        escalation: bool,
        reason: Optional[str] = None,
    ) -> None:
        """Single dispatch: if target is None → user gateway; else → agent inbox."""
        body = self._render_question_body(
            qid, sender, questions, context_hint, chain, escalation, reason,
        )
        if target is None:
            # User target — surface to the gateway.
            if self.gateway is not None:
                surface = getattr(self.gateway, "surface_question", None)
                if surface is not None:
                    asyncio.create_task(surface(qid, body, questions))
        else:
            # Agent target — normal inbox message.
            msg = Message(
                from_id="beidou",
                content=body,
                ts=time.time(),
                message_id=f"qmsg-{qid}",
                kind="system",
            )
            await self.inbox_put(target, msg)

    def _render_question_body(
        self,
        qid: str,
        sender: str,
        questions: list[dict],
        context_hint: Optional[str],
        chain: list[str],
        escalation: bool,
        reason: Optional[str] = None,
    ) -> str:
        """Render the inbox body for an agent-targeted question delivery.

        Format ported from QuestionBroker._notify_holder (inbox.py:310-328).
        Wording kept identical so receiving agents see the same message format.
        Adds an '(escalated by X)' line when escalation=True.
        """
        prompt_preview = render_prompt_text(questions)
        asker_agent_id = chain[0] if chain else sender
        body = (
            f"[INBOX QUESTION] qid={qid} from {asker_agent_id}\n"
            f"chain: {' → '.join(chain)}\n"
            f"\n{prompt_preview}\n"
        )
        if escalation and reason:
            body += f"\n(escalated by {sender}: {reason})\n"
        if context_hint:
            body += f"\ncontext: {context_hint}\n"
        body += (
            f"\nResolve this BEFORE doing anything else. Two options:\n"
            f"  1. Answer it directly via mcp__beidou__answer_question("
            f"qid=\"{qid}\", answers=[{{\"selected_labels\": [...], \"text\": \"...\"}}, ...])"
            f" if you can answer from what you already know.\n"
            f"  2. Escalate via mcp__beidou__escalate_question("
            f"qid=\"{qid}\", reason=\"...\") if only the next-up reviewer "
            f"or the user can answer.\n"
        )
        return body

    def resolve_question(self, qid: str, answers: list[dict], *, answerer: str | None = None, reason: str | None = None) -> dict:
        """Public entry for the gateway and answer_question primitive.

        Owns the side-effects (DB update, event emit) that the registry
        deliberately avoids. The registry just sets the future.
        """
        pq = self._questions.get(qid)
        if pq is None:
            return {"ok": False, "reason": "unknown_qid"}
        if pq.future.done():
            return {"ok": False, "reason": "already_answered"}

        out = self._questions.resolve(qid, answers)
        if not out["ok"]:
            return out

        answer_text = out["answer_text"]

        # Non-blocking DB update.
        try:
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _db.update_question_answered(
                    qid=qid, answer=json.dumps(answers), answered_at=time.time(),
                ),
            )
        except Exception:
            pass

        # Event emit — orchestrator-owned; single source of truth.
        self.emit_event(
            "question_answered",
            {
                "agent_id": pq.asker_agent_id,
                "qid": qid,
                "asker": pq.asker_agent_id,
                "answerer": answerer,
                "reason": reason[:200] if reason else None,
                "chain_len": len(pq.chain),
                "answers": answers,
                "answer_text": answer_text,
            },
        )

        return {"ok": True}

    def record_status(
        self,
        caller_id: str,
        state: str,
        detail: Optional[str],
    ) -> None:
        rec = self._agents.get(caller_id)
        if rec is not None:
            rec.last_status = state
            rec.last_status_detail = detail or ""
        # Liveness signal as a data-only event; leader observes via
        # list_peers. See orchestration.md "Liveness checks".
        if state == "done" and rec is not None:
            team = self._teams.get(rec.team_id)
            all_done = bool(
                team
                and all(
                    self._agents[mid].last_status == "done"
                    for mid in team.member_ids
                    if mid in self._agents
                )
            )
            self._schedule_emit(
                "liveness_check",
                caller_id,
                rec.team_id,
                {"all_done": all_done, "ts": time.time()},
            )

    def was_terminated(self, caller_id: str) -> bool:
        rec = self._agents.get(caller_id)
        if rec is None:
            return False
        return rec.terminate_consumed

    # ------------------------------------------------------------------
    # Assistant text recording / retrieval for PostToolUse hook.
    # ------------------------------------------------------------------

    def record_assistant_text(
        self,
        caller_id: str,
        text: str,
        tool_use_ids: list[str],
    ) -> None:
        """Record the assistant text from one turn, binding it to all tool_use_ids in that turn.

        Called by the drain loop in sdk_agent.py after processing one AssistantMessage.
        Both the per-tool_use_id map and the most-recent-per-agent fallback are updated.

        If text is empty, only the fallback is updated when no prior text exists;
        we deliberately avoid overwriting good text with empty text.
        """
        if text:
            self._most_recent_assistant_text[caller_id] = text
            agent_map = self._assistant_text_by_tool.setdefault(caller_id, {})
            for tid in tool_use_ids:
                agent_map[tid] = text

    def assistant_text_for_turn(self, caller_id: str, tool_use_id: str) -> str | None:
        """Return the assistant text from the same turn as tool_use_id.

        Exact binding: looks up the per-tool_use_id map populated by the drain loop.
        Fallback: if the tool_use_id is not found (e.g. model emitted text in a
        prior message before the report_status call), returns the most recent
        assistant text for the agent.

        Returns None if no text has been recorded for this agent at all.
        """
        agent_map = self._assistant_text_by_tool.get(caller_id, {})
        text = agent_map.get(tool_use_id)
        if text is not None:
            return text
        # Fallback: most recent assistant text for this agent (may be from a prior turn).
        return self._most_recent_assistant_text.get(caller_id)

    # ------------------------------------------------------------------
    # deliver_message: orchestrator-side A2A delivery (used by hooks).
    # ------------------------------------------------------------------

    def deliver_message(
        self,
        from_id: str,
        to_id: str,
        body: str,
        kind: str = "message",
    ) -> None:
        """Deliver a message to ``to_id``'s inbox from the orchestrator runtime.

        Unlike the agent-facing ``send_message`` primitive (which enforces the
        inbox cap for non-Beidou senders), this method uses ``from_id=from_id``
        so the recipient sees the real sender.  If ``to_id`` is the user-sentinel
        (no agent registered), the call is silently ignored.

        Inbox overflow: if the recipient's inbox is full, we emit a
        ``completion.empty`` event with reason ``inbox_full`` rather than
        crashing the hook callback.
        """
        if to_id not in self._agents:
            # Destination is the user sentinel or an unknown id — drop silently.
            return

        message_id = str(uuid.uuid4())
        msg = Message(
            from_id=from_id,
            content=body,
            ts=time.time(),
            message_id=message_id,
            kind=kind,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — deliver inline (tests without a loop).
            rec = self._agents[to_id]
            if rec.inbox.qsize() < INBOX_CAP:
                rec.inbox.put_nowait(msg)
            else:
                self.emit_event(
                    "completion.empty",
                    {
                        "agent_id": from_id,
                        "leader_id": to_id,
                        "reason": "inbox_full",
                    },
                )
            return

        async def _deliver() -> None:
            try:
                await self.inbox_put(to_id, msg)
            except PrimitiveError as exc:
                if exc.code == "inbox_full":
                    self.emit_event(
                        "completion.empty",
                        {
                            "agent_id": from_id,
                            "leader_id": to_id,
                            "reason": "inbox_full",
                        },
                    )
                # Unknown recipient errors are already logged; ignore.
                return
            # Emit the message event only on successful delivery.
            self.emit_event(
                "message",
                {
                    "agent_id": from_id,
                    "to": to_id,
                    "message_id": message_id,
                    "kind": kind,
                    "ts": time.time(),
                },
            )

        task = loop.create_task(_deliver())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------
    # Agent registration helper.
    # ------------------------------------------------------------------

    def _register_agent_record(self, rec: AgentRecord) -> None:
        """Register a new AgentRecord and lazily start the watchdog task.

        Must be called from within a running event loop (i.e. from async
        context) to allow asyncio.create_task. Guarded defensively so tests
        that construct AgentRecord outside a loop aren't broken.
        """
        self._agents[rec.agent_id] = rec
        if self._watchdog_task is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # No loop — watchdog will start on first async call.
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(),
                name="beidou-watchdog",
            )

    # ------------------------------------------------------------------
    # Watchdog — background liveness / review-escalation task.
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Background coroutine: tick every WATCHDOG_INTERVAL_S seconds.

        Two passes per tick:
        - Pass A: escalate agents with completion_pending=True that their
          leader hasn't reviewed in time.
        - Pass B: nudge idle agents that have made no progress.
        """
        while not self._shutting_down:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            if self._shutting_down:
                break
            try:
                await self._watchdog_tick()
            except Exception as exc:  # noqa: BLE001
                import traceback
                tb = traceback.format_exc()[:500]
                self._schedule_emit(
                    "watchdog.exception",
                    "",
                    None,
                    {"exception": type(exc).__name__, "msg": str(exc), "tb": tb, "ts": time.time()},
                )

    async def _watchdog_ask_and_deliver_liveness_answer(
        self,
        agent_id: str,
        questions: list[dict],
    ) -> None:
        """Watchdog-local helper: ask the user gateway, then deliver the answer to the parked agent.

        The parked agent's prior SDK turn has already drained, so it is waiting on
        inbox.get() — not awaiting the question future.  resolve_question sets the
        future (which no one is awaiting here), so we must explicitly forward the
        answer into the agent's inbox via deliver_message.

        No try/except needed: deliver_message already no-ops silently if the recipient
        is gone, and an unexpected exception in a background task is acceptable (the
        _bg_tasks.discard callback handles cleanup).
        """
        result = await self.gateway_ask_user_structured(agent_id, questions, None)
        prompt_text = render_prompt_text(questions)
        answer_text = result["answer_text"]
        self.deliver_message(
            from_id="beidou",
            to_id=agent_id,
            body=(
                "[LIVENESS ANSWER from user]\n"
                f"{prompt_text}\n\n"
                f"User answer: {answer_text}"
            ),
            kind="liveness_answer",
        )

    def _agent_freshness_ts(self, agent_id: str) -> float:
        """Return the Pass B freshness timestamp for an agent, or 0.0 if the agent is still active or legitimately waiting.

        Returns 0.0 when:
          - a tool call is in-flight (inflight_tools > 0),
          - this agent is waiting for its leader to review its completion (completion_pending),
          - the agent forwarded a question and is waiting for it to be resolved upstream, or
          - last_drain_ts is None or last_progress_ts > last_drain_ts (inbox/tool activity is
            newer than the most recent natural drain, so the current turn is still in-flight or
            the agent was freshly awakened since the last drain).

        Returns rec.last_drain_ts otherwise: the timestamp of the most-recent natural SDK turn
        drain that Pass B can compare against now to decide whether the agent has been idle.
        """
        rec = self._agents[agent_id]
        if rec.inflight_tools > 0 or rec.completion_pending:
            return 0.0
        if self._questions.has_pending_through(agent_id):
            return 0.0
        if rec.last_drain_ts is None or rec.last_progress_ts > rec.last_drain_ts:
            return 0.0
        return rec.last_drain_ts

    async def _watchdog_tick(self) -> None:
        """One tick of the watchdog: Pass A (review escalation) + Pass B (liveness nudge).

        Snapshot the agent list to avoid issues if _agents is mutated during iteration.
        """
        import traceback as _traceback

        now = time.time()
        agents = list(self._agents.values())

        # ------------------------------------------------------------------
        # Pass A — review-pending escalation.
        # ------------------------------------------------------------------
        for rec in agents:
            if not rec.completion_pending:
                continue
            if rec.terminate_consumed:
                continue
            if rec.completion_pending_ts is None:
                continue
            delta = now - rec.completion_pending_ts
            if delta < REVIEW_PING_INTERVAL_S:
                continue
            # Determine the leader of this child.
            team = self._teams.get(rec.team_id)
            if team is None:
                continue
            leader = team.leader_id
            if leader == USER_SENTINEL:
                # Root agent; no leader to ping.
                continue

            child_id = rec.agent_id
            delta_s = int(delta)

            if rec.review_ping_count == 0:
                body = (
                    f"[REVIEW REQUIRED — STILL PENDING {delta_s}s]"
                    f" child={child_id} awaiting your decision."
                    f" Call mcp__beidou__terminate_child(agent_id=\"{child_id}\") (approve)"
                    f" OR mcp__beidou__send_message(to=\"{child_id}\", content=\"rework: ...\")."
                    f" Do not end your turn before deciding."
                )
                self.deliver_message(
                    from_id="beidou",
                    to_id=leader,
                    body=body,
                    kind="ping",
                )
                rec.review_ping_count += 1
                rec.completion_pending_ts = now
                self.emit_event(
                    "completion.reping",
                    {"agent_id": child_id, "team_id": rec.team_id, "leader_id": leader, "ping_count": rec.review_ping_count, "delta_s": delta_s, "ts": now},
                )

            elif rec.review_ping_count == 1:
                body = (
                    f"[REVIEW REQUIRED — STILL PENDING {delta_s}s]"
                    f" child={child_id} awaiting your decision."
                    f" Call mcp__beidou__terminate_child(agent_id=\"{child_id}\") (approve)"
                    f" OR mcp__beidou__send_message(to=\"{child_id}\", content=\"rework: ...\")."
                    f" Do not end your turn before deciding."
                    f"\n\nIf you do not act on this ping, this will escalate to the user gateway in ~60s."
                )
                self.deliver_message(
                    from_id="beidou",
                    to_id=leader,
                    body=body,
                    kind="ping",
                )
                rec.review_ping_count += 1
                rec.completion_pending_ts = now
                self.emit_event(
                    "completion.reping",
                    {"agent_id": child_id, "team_id": rec.team_id, "leader_id": leader, "ping_count": rec.review_ping_count, "delta_s": delta_s, "ts": now},
                )

            elif rec.review_ping_count == 2:
                # Escalate to user gateway (fire-and-forget — the watchdog
                # does not await the user's answer; the question surfaces
                # asynchronously via the gateway).
                self.emit_event(
                    "review.escalated_to_user",
                    {"agent_id": child_id, "team_id": rec.team_id, "leader_id": leader, "ts": now},
                )
                if self.is_gateway_available():
                    _q = [
                        {
                            "question": (
                                f"Root agent's leader {leader} has not decided on completion"
                                f" review for {child_id} after 3 pings."
                                f" Approve (terminate_child), rework, or abort?"
                            ),
                            "header": "",
                            "multiSelect": False,
                            "options": [],
                        }
                    ]
                    _t = asyncio.create_task(self.gateway_ask_user_structured(leader, _q, None))
                    self._bg_tasks.add(_t)
                    _t.add_done_callback(self._bg_tasks.discard)
                rec.review_ping_count += 1

            # review_ping_count >= 3: user owns it; do nothing.

        # ------------------------------------------------------------------
        # Pass B — general liveness nudge.
        # ------------------------------------------------------------------
        for rec in agents:
            agent_id = rec.agent_id
            # Skip sentinels and terminated agents.
            if agent_id == USER_SENTINEL:
                continue
            if rec.terminate_consumed:
                continue
            # Skip if already escalated.
            if rec.idle_nudge_count >= MAX_PINGS_BEFORE_ESCALATION:
                continue
            freshness = self._agent_freshness_ts(agent_id)
            if freshness == 0:
                continue
            if now - freshness < IDLE_NUDGE_S:
                continue

            delta_s = int(now - freshness)

            if rec.idle_nudge_count < 2:
                body = (
                    f"[BEIDOU LIVENESS CHECK] Your last SDK turn drained {delta_s}s ago and the runtime has not seen fresh work since.\n"
                    f"Choose one and act on this turn:\n"
                    f"  (a) call report_status(state=\"done\", detail=\"...\") if your work is complete,\n"
                    f"  (b) take the needed coordination step (for example send_message or terminate_child) if you are waiting on another agent,\n"
                    f"  (c) call ask_user if you are blocked on missing user input,\n"
                    f"  (d) if your work is not finished and you still have concrete next steps, keep working now — emit your next plan or tool call on this turn.\n"
                    f"Do not answer only with \"still working\" or \"waiting\"."
                )
                if rec.idle_nudge_count == 1:
                    body += "\n\nIf you do not act, this will escalate to the user gateway in ~120s."
                self.deliver_message(
                    from_id="beidou",
                    to_id=agent_id,
                    body=body,
                    kind="ping",
                )
            elif rec.idle_nudge_count == 2:
                # Escalate to user gateway (fire-and-forget — the watchdog
                # does not await the user's answer).
                self.emit_event(
                    "liveness.escalated_to_user",
                    {"agent_id": agent_id, "team_id": rec.team_id, "delta_s": delta_s, "ts": now},
                )
                if self.is_gateway_available():
                    _q = [
                        {
                            "question": (
                                f"Agent {agent_id} has been idle {delta_s}s with no progress."
                                f" Approve continuation, redirect, or abort?"
                            ),
                            "header": "",
                            "multiSelect": False,
                            "options": [],
                        }
                    ]
                    _t = asyncio.create_task(
                        self._watchdog_ask_and_deliver_liveness_answer(agent_id, _q)
                    )
                    self._bg_tasks.add(_t)
                    _t.add_done_callback(self._bg_tasks.discard)

            rec.idle_nudge_count += 1
            rec.last_progress_ts = now
            self.emit_event(
                "liveness.nudge",
                {"agent_id": agent_id, "team_id": rec.team_id, "nudge_count": rec.idle_nudge_count, "delta_s": delta_s, "ts": now},
            )

        # ------------------------------------------------------------------
        # Pass C — terminate-grace cancel backstop.
        # ------------------------------------------------------------------
        for rec in agents:
            if rec.agent_id == USER_SENTINEL:
                continue
            if rec.terminate_grace_deadline is None:
                continue
            if now < rec.terminate_grace_deadline:
                continue
            if rec.terminate_consumed:
                continue
            if rec.run_task is None or rec.run_task.done():
                continue
            # Grace period expired and agent has not yet consumed its terminate
            # sentinel — cancel the run_task to unstick it.
            rec.run_task.cancel()
            self.emit_event(
                "terminate.forced",
                {
                    "caller_id": "watchdog",
                    "agent_id": rec.agent_id,
                    "team_id": rec.team_id,
                    "reason": "watchdog_grace",
                    "ts": now,
                },
            )
            # Clear the deadline to prevent re-firing on subsequent ticks.
            rec.terminate_grace_deadline = None

    async def stop_watchdog(self) -> None:
        """Signal the watchdog to stop and await its cancellation.

        Safe to call multiple times and when no watchdog was started.
        """
        self._shutting_down = True
        task = self._watchdog_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Resume-not-terminate policy.
    # ------------------------------------------------------------------

    async def _run_agent_with_policy(
        self,
        rec: AgentRecord,
        spec: SpawnSpec,
    ) -> RunResult:
        """Drive one agent's SDK loop through ``sdk_agent.run_agent``.

        On contract violation (run ends without terminate), increments
        ``contract_strikes`` and resumes with a nudge. After ``CONTRACT_STRIKES``
        consecutive violations, escalates to the agent's parent leader via
        ``send_message`` (for root: emits ``root_contract_escalation`` and, if
        available, asks the user gateway). Resets the strike counter only on
        a clean terminated exit.

        On crash (subprocess exit code 1, etc.), applies the hybrid retry
        strategy from agent-runtime.md §5.1:
          Strike 1 — resume via ``resume=session_id`` with recovery prompt.
          Strike 2 — fresh restart with recovery prompt pointing at workspace.
          Strike 3 — escalate to leader (members) or user gateway (root).
        Resets ``crash_strikes`` to 0 on any successful ``run_agent()`` return.
        ``asyncio.CancelledError`` is re-raised, not treated as a crash.
        """
        current_spec = spec
        last_result: Optional[RunResult] = None
        while True:
            # Looked up lazily on every iteration so tests can monkeypatch
            # ``beidou.sdk_agent.run_agent``.
            try:
                result = await sdk_agent.run_agent(self, current_spec)
            except asyncio.CancelledError:
                # Cancellation is a watchdog signal, not a crash — re-raise
                # so the asyncio task canceller can process it.
                raise
            except Exception as exc:
                rec.crash_strikes += 1
                rec.inflight_tools = 0       # stale from pre-crash drain
                rec.last_progress_ts = time.time()

                # Capture stderr from the sdk_agent drain record if available.
                stderr_text = rec.last_crash_stderr

                self.emit_event(
                    "agent_crashed",
                    {
                        "caller_id": rec.agent_id,
                        "exception": type(exc).__name__,
                        "msg": str(exc),
                        "strike_count": rec.crash_strikes,
                        "stderr": stderr_text,
                        "ts": time.time(),
                    },
                )

                if rec.crash_strikes >= CRASH_STRIKES:
                    await self._escalate_crash(rec)
                    self.emit_event(
                        "agent_exited",
                        {
                            "caller_id": rec.agent_id,
                            "strike_count": rec.contract_strikes,
                            "terminated": False,
                            "crash_strikes": rec.crash_strikes,
                            "ts": time.time(),
                        },
                    )
                    self._remove_orphan_plan(rec.agent_id)
                    return RunResult(
                        final_text="",
                        total_cost_usd=0.0,
                        total_usage={},
                        num_turns=0,
                        duration_ms=0,
                        stop_reason="crash_escalated",
                        session_id=None,
                        terminated=False,
                        contract_violation=False,
                    )

                if rec.crash_strikes == 1 and rec.last_session_id:
                    # STRIKE 1: Resume with session history.
                    # Wrap in try — if resume fails (mid-tool-call 400),
                    # promote to strike 2 without burning another crash slot.
                    # fork_session=False — we want continuity, not branching.
                    current_spec = replace(
                        current_spec,
                        task="[beidou] Your previous session crashed. Resume your work.",
                        resume_session_id=rec.last_session_id,
                    )
                    try:
                        result = await sdk_agent.run_agent(self, current_spec)
                    except asyncio.CancelledError:
                        raise
                    except Exception as resume_exc:
                        # Resume failed — promote to strike 2 without extra count.
                        rec.crash_strikes = 2
                        stderr_text = rec.last_crash_stderr
                        self.emit_event(
                            "agent_crashed",
                            {
                                "caller_id": rec.agent_id,
                                "exception": type(resume_exc).__name__,
                                "msg": str(resume_exc),
                                "strike_count": rec.crash_strikes,
                                "stderr": stderr_text,
                                "ts": time.time(),
                            },
                        )
                        current_spec = replace(
                            current_spec,
                            task=(
                                "[beidou] Your session crashed and could not be "
                                "resumed. Start fresh — check your workspace for "
                                "prior artifacts."
                            ),
                            resume_session_id=None,
                        )
                        continue
                    # Resume succeeded — proceed to normal policy handling below.
                else:
                    # STRIKE 2+: Fresh restart
                    current_spec = replace(
                        current_spec,
                        task=(
                            "[beidou] Your session crashed and could not be "
                            "resumed. Start fresh — check your workspace for "
                            "prior artifacts."
                        ),
                        resume_session_id=None,
                    )
                    continue

                # If we reach here, the resume succeeded. Fall through
                # to the normal policy handling with `result` set.
                rec.crash_strikes = 0
            else:
                # run_agent() returned normally (no exception).
                rec.crash_strikes = 0

            last_result = result

            if result.terminated:
                # Per agent-runtime.md #4, strikes count "consecutive"
                # violations; a clean terminated exit breaks the chain, but
                # since this is the terminal state of the run we simply
                # leave the counter alone -- no downstream reader cares.
                self.emit_event(
                    "agent_exited",
                    {
                        "caller_id": rec.agent_id,
                        "strike_count": rec.contract_strikes,
                        "terminated": True,
                        "ts": time.time(),
                    },
                )
                # Clean up any orphan plan this agent owns (leader declared a
                # plan but never spawned, or all tasks already finished).
                self._remove_orphan_plan(rec.agent_id)
                return result

            rec.contract_strikes += 1
            self.emit_event(
                "contract_violation",
                {
                    "caller_id": rec.agent_id,
                    "strike_count": rec.contract_strikes,
                    "stop_reason": result.stop_reason,
                    "action": (
                        "escalated_to_user"
                        if rec.agent_id == self._root_id and rec.contract_strikes >= CONTRACT_STRIKES
                        else "escalated_to_leader"
                        if rec.contract_strikes >= CONTRACT_STRIKES
                        else "resumed"
                    ),
                    "ts": time.time(),
                },
            )

            if rec.contract_strikes >= CONTRACT_STRIKES:
                await self._escalate_contract_violation(rec)
                self.emit_event(
                    "agent_exited",
                    {
                        "caller_id": rec.agent_id,
                        "strike_count": rec.contract_strikes,
                        "terminated": False,
                        "ts": time.time(),
                    },
                )
                # TODO: orphan plan cleanup — contract-escalation path also
                # removes the agent, but in_flight tasks (children) may still
                # be running; _remove_orphan_plan handles this safely by
                # checking in_flight count before removing.
                self._remove_orphan_plan(rec.agent_id)
                return result

            # Resume with a nudge; keep skill_name / model / etc. intact.
            current_spec = replace(
                current_spec,
                task=(
                    "[beidou] You ended your turn without a tool call but "
                    "have not been terminated. You must not self-exit. "
                    "Continue working or call report_status(state='done') "
                    "when your task is complete."
                ),
            )

    async def _escalate_contract_violation(self, rec: AgentRecord) -> None:
        """Post a recommendation to the agent's team leader.

        For the root agent, whose "leader" is the user sentinel, escalate
        via the human gateway if available; otherwise emit a dedicated
        event the CLI can observe.
        """
        if rec.agent_id == self._root_id:
            self._schedule_emit(
                "root_contract_escalation",
                rec.agent_id,
                rec.team_id,
                {"strike_count": rec.contract_strikes, "ts": time.time()},
            )
            if self.is_gateway_available():
                try:
                    await self.gateway_ask_user_structured(
                        rec.agent_id,
                        [
                            {
                                "question": (
                                    f"Root agent {rec.agent_id} has violated the "
                                    f"no-self-exit contract {rec.contract_strikes} times. "
                                    f"Continue, or terminate the root?"
                                ),
                                "header": "",
                                "multiSelect": False,
                                "options": [],
                            }
                        ],
                        None,
                    )
                except Exception:
                    # Best-effort; escalation event already logged.
                    pass
            return

        leader_id = self.leader_of(rec.team_id)
        msg = Message(
            from_id="beidou",
            content=(
                f"agent {rec.agent_id} has violated the no-self-exit "
                f"contract {rec.contract_strikes} times. Consider "
                f"terminate_child({rec.agent_id})."
            ),
            ts=time.time(),
            message_id=str(uuid.uuid4()),
            kind="user",
        )
        try:
            await self.inbox_put(leader_id, msg)
        except PrimitiveError:
            # Leader's inbox full or unknown — best effort.
            pass

    async def _escalate_crash(self, rec: AgentRecord) -> None:
        """Escalate after ``CRASH_STRIKES`` consecutive subprocess crashes.

        Mirrors ``_escalate_contract_violation`` but for crash recovery.
        Members: posts a message to the team leader recommending action.
        Root: emits ``root_crash_escalation`` and asks the human gateway.
        """
        if rec.agent_id == self._root_id:
            self._schedule_emit(
                "root_crash_escalation",
                rec.agent_id,
                rec.team_id,
                {
                    "crash_strikes": rec.crash_strikes,
                    "stderr": rec.last_crash_stderr,
                    "ts": time.time(),
                },
            )
            if self.is_gateway_available():
                try:
                    await self.gateway_ask_user_structured(
                        rec.agent_id,
                        [
                            {
                                "question": (
                                    f"Root agent {rec.agent_id} has crashed "
                                    f"{rec.crash_strikes} times. "
                                    f"Last stderr: {rec.last_crash_stderr[:200]}. "
                                    f"Continue, or terminate the root?"
                                ),
                                "header": "",
                                "multiSelect": False,
                                "options": [],
                            }
                        ],
                        None,
                    )
                except Exception:
                    # Best-effort; escalation event already logged.
                    pass
            return

        leader_id = self.leader_of(rec.team_id)
        msg = Message(
            from_id="beidou",
            content=(
                f"agent {rec.agent_id} has crashed {rec.crash_strikes} times. "
                f"Consider terminate_child({rec.agent_id}) or re-spawning."
            ),
            ts=time.time(),
            message_id=str(uuid.uuid4()),
            kind="user",
        )
        try:
            await self.inbox_put(leader_id, msg)
        except PrimitiveError:
            # Leader's inbox full or unknown — best effort.
            pass

    # ------------------------------------------------------------------
    # Plan lifecycle — internal helpers.
    # ------------------------------------------------------------------

    def _get_plan_lock(self, agent_id: str) -> asyncio.Lock:
        """Return (lazily creating) the per-agent plan mutation lock.

        Distinct from the spawn_lock: plan operations (declare, remove, status
        transitions) are serialised by this lock; spawning is serialised by the
        spawn_lock. Keeping them separate avoids deadlocks between the two paths.
        """
        lock = self._plan_locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._plan_locks[agent_id] = lock
        return lock

    def _load_plans_for_run(self) -> None:
        """Populate _plans and _active_plan_by_agent from disk on startup.

        Hot-reload helper: if the orchestrator process was restarted mid-run
        (dev-server restart), this re-hydrates in-memory plan state from the
        persistent plan files. Silently no-ops when the plans directory is absent.
        """
        plans_root = Path.home() / ".beidou" / "runs"
        try:
            loaded = _plans.load_plans_for_run(self.task_id, plans_root)
        except Exception:
            return
        for plan in loaded.values():
            self._plans[plan.plan_id] = plan
            self._active_plan_by_agent[plan.owner_agent_id] = plan.plan_id

    def _agent_owning_task(self, task_id: str) -> Optional["_plans.Plan"]:
        """Return the Plan that contains task_id, or None."""
        for plan in self._plans.values():
            if task_id in plan.tasks:
                return plan
        return None

    def _is_leader_of_task(self, caller_id: str, task_id: str) -> bool:
        """True iff caller_id owns the plan that contains task_id."""
        plan = self._agent_owning_task(task_id)
        return plan is not None and plan.owner_agent_id == caller_id

    def _remove_orphan_plan(self, agent_id: str) -> None:
        """Remove an agent's active plan when it is no longer needed.

        Called from agent-teardown paths. If the plan has in_flight tasks, we
        leave it — the cascade termination will eventually clear them, triggering
        mark_task_failed, which calls this again once the count reaches zero.
        """
        plan_id = self._active_plan_by_agent.get(agent_id)
        if plan_id is None:
            return
        plan = self._plans.get(plan_id)
        if plan is None:
            return
        in_flight = [t for t in plan.tasks.values() if t.status == "in_flight"]
        if in_flight:
            # Leave plan in place; cascade will clean up when tasks exit.
            return
        try:
            _plans.remove_plan_file(plan)
        except Exception:
            pass
        self._plans.pop(plan_id, None)
        self._active_plan_by_agent.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Plan lifecycle — public API (called by primitive wrappers).
    # ------------------------------------------------------------------

    async def register_plan(
        self,
        *,
        caller_id: str,
        specs: list[dict],
    ) -> dict:
        """Validate a task DAG, persist it, and register it as caller's active plan.

        Returns the wire shape documented in the plan spec:
        ``{plan_id, plan_path, tasks: [{id, role, skill, depends_on, status}, ...]}``.

        Raises ``PrimitiveError`` on validation failures (see plan spec for codes)
        or if the caller already has an active plan (use remove_active_plan first).
        """
        async with self._get_plan_lock(caller_id):
            if caller_id in self._active_plan_by_agent:
                raise PrimitiveError(
                    "plan_already_declared",
                    f"{caller_id} already has an active plan; call remove_plan first",
                    caller_id=caller_id,
                    active_plan_id=self._active_plan_by_agent[caller_id],
                )
            try:
                plan = _plans.create_plan(
                    owner_agent_id=caller_id,
                    run_task_id=self.task_id,
                    specs=specs,
                )
            except _plans.EmptyPlanError as exc:
                raise PrimitiveError("empty_plan", str(exc)) from exc
            except _plans.DuplicateTaskIdError as exc:
                raise PrimitiveError("duplicate_task_id", str(exc), task_id=exc.task_id) from exc
            except _plans.SelfDepError as exc:
                raise PrimitiveError("self_dep", str(exc), task_id=exc.task_id) from exc
            except _plans.UnknownDepError as exc:
                raise PrimitiveError("unknown_dep", str(exc), task_id=exc.task_id, bad_dep=exc.bad_dep) from exc
            except _plans.CycleDetectedError as exc:
                raise PrimitiveError("cycle_detected", str(exc), chain=exc.chain) from exc
            except _plans.PlanValidationError as exc:
                raise PrimitiveError("plan_invalid", str(exc)) from exc

            self._plans[plan.plan_id] = plan
            self._active_plan_by_agent[caller_id] = plan.plan_id

        self.emit_event(
            "plan_declared",
            {
                "agent_id": caller_id,
                "plan_id": plan.plan_id,
                "task_count": len(plan.tasks),
                "ts": time.time(),
            },
        )
        return {
            "plan_id": plan.plan_id,
            "plan_path": plan.file_path,
            "tasks": [
                {
                    "id": t.id,
                    "role": t.role,
                    "skill": t.skill,
                    "depends_on": t.depends_on,
                    "status": t.status,
                }
                for t in plan.tasks.values()
            ],
        }

    async def remove_active_plan(self, *, caller_id: str) -> dict:
        """Remove caller's active plan so a fresh one can be declared.

        Raises ``plan_in_use`` if any task is in_flight (the leader must
        approve or force-terminate them first). Done/failed tasks do not block.
        """
        async with self._get_plan_lock(caller_id):
            plan_id = self._active_plan_by_agent.get(caller_id)
            if plan_id is None:
                raise PrimitiveError("no_active_plan", f"{caller_id} has no active plan")
            plan = self._plans[plan_id]

            in_flight = [
                t for t in plan.tasks.values() if t.status == "in_flight"
            ]
            if in_flight:
                raise PrimitiveError(
                    "plan_in_use",
                    f"plan {plan_id} has {len(in_flight)} in-flight task(s); "
                    "approve or force-terminate them before replanning",
                    plan_id=plan_id,
                    in_flight_task_ids=[t.id for t in in_flight],
                    in_flight_agent_ids=[t.agent_id for t in in_flight if t.agent_id],
                )

            try:
                _plans.remove_plan_file(plan)
            except Exception:
                pass
            self._plans.pop(plan_id, None)
            self._active_plan_by_agent.pop(caller_id, None)

        self.emit_event(
            "plan_removed",
            {
                "agent_id": caller_id,
                "plan_id": plan_id,
                "ts": time.time(),
            },
        )
        return {"removed_plan_id": plan_id}

    async def spawn_for_task(self, *, caller_id: str, task_id: str) -> dict:
        """Spawn an agent for a plan task, lazily creating the team on first call.

        Gates: caller has an active plan; task_id exists; status==ready; team
        has fewer than 8 in-flight members. On first call, depth is checked and
        the team is materialised (team_created event emitted before agent launch).

        Returns ``{agent_id, team_id, task_id, remaining_ready, team_created}``.
        """
        # Acquire spawn_lock first (limits.md #5: one concurrent spawn per agent).
        lock = await self.spawn_lock(caller_id)
        async with lock:
            plan_id = self._active_plan_by_agent.get(caller_id)
            if plan_id is None:
                raise PrimitiveError("no_active_plan", f"{caller_id} has no active plan")
            plan = self._plans[plan_id]

            if task_id not in plan.tasks:
                raise PrimitiveError(
                    "unknown_task",
                    f"task {task_id!r} is not in the active plan",
                    task_id=task_id,
                    plan_id=plan_id,
                )

            task = plan.tasks[task_id]
            if task.status != "ready":
                if task.status == "blocked":
                    unmet = [d for d in task.depends_on if plan.tasks[d].status != "done"]
                    raise PrimitiveError(
                        "task_not_ready",
                        f"task {task_id!r} is blocked on unmet dependencies",
                        task_id=task_id,
                        unmet_deps=unmet,
                    )
                raise PrimitiveError(
                    "task_not_pending",
                    f"task {task_id!r} has status {task.status!r}",
                    task_id=task_id,
                    current_status=task.status,
                )

            # Determine whether we need to create the team.
            existing_teams = self.teams_led_by(caller_id)
            team_was_created = False

            if not existing_teams:
                # First spawn: depth check + lazy team materialisation.
                parent_team_id = self.agent_team(caller_id)
                new_depth = self.team_depth(parent_team_id) + 1
                # Depth cap is in limits.md #2 (value imported from primitives).
                from beidou.primitives.core import MAX_DEPTH
                if new_depth > MAX_DEPTH:
                    raise PrimitiveError(
                        "depth_exceeded",
                        f"spawning would exceed max recursion depth {MAX_DEPTH}",
                        current_depth=new_depth - 1,
                        max_depth=MAX_DEPTH,
                    )

                new_team_id = f"tm_{uuid.uuid4().hex[:8]}"
                workspace_path = self._provision_team_workspace(new_team_id)

                # Validate skill before allocating agent id.
                try:
                    from beidou.skills.loader import load_skill, SkillError
                    load_skill(self.skill_root, task.skill)
                except SkillError as exc:
                    raise PrimitiveError(
                        "unknown_skill",
                        f"cannot load skill {task.skill!r}: {exc}",
                        skill=task.skill,
                    ) from exc

                agent_id, rec, spec = self._register_member_and_launch(
                    new_team_id,
                    {"role": task.role, "skill": task.skill, "description": task.description, "model": task.model},
                    per_member_task=task.task_text,
                    leader_id=caller_id,
                    workspace_path=workspace_path,
                    team_name=f"{caller_id}-team",
                    plan_task_id=task_id,
                    plan_id=plan_id,
                )

                # Register team BEFORE emitting team_created (event ordering invariant).
                self._create_team_record(
                    new_team_id,
                    f"{caller_id}-team",
                    caller_id,
                    new_depth,
                    parent_team_id,
                    [agent_id],
                    [],
                )

                self.emit_event(
                    "team_created",
                    {
                        "new_team_id": new_team_id,
                        "team_name": f"{caller_id}-team",
                        "leader_agent_id": caller_id,
                        "parent_team_id": parent_team_id,
                        "depth": new_depth,
                        "members": [{"agent_id": agent_id, "role": task.role, "name": rec.name}],
                        "ts": time.time(),
                    },
                )
                team_was_created = True

            else:
                # Subsequent spawn: append to the existing team.
                new_team_id = existing_teams[0]
                team_rec = self._teams[new_team_id]

                # Cap: at most 8 in-flight (live) members per team (limits.md #1).
                live_members = [
                    mid for mid in team_rec.member_ids
                    if mid in self._agents and not self._agents[mid].terminate_consumed
                ]
                if len(live_members) >= 8:
                    raise PrimitiveError(
                        "team_cap_exceeded",
                        f"team {new_team_id} already has {len(live_members)} live members (cap: 8)",
                        live_count=len(live_members),
                        live_agent_ids=live_members,
                    )

                try:
                    from beidou.skills.loader import load_skill, SkillError
                    load_skill(self.skill_root, task.skill)
                except SkillError as exc:
                    raise PrimitiveError(
                        "unknown_skill",
                        f"cannot load skill {task.skill!r}: {exc}",
                        skill=task.skill,
                    ) from exc

                workspace_path = team_workspace(self.project_workspace, self.task_id, new_team_id)
                agent_id, rec, spec = self._register_member_and_launch(
                    new_team_id,
                    {"role": task.role, "skill": task.skill, "description": task.description, "model": task.model},
                    per_member_task=task.task_text,
                    leader_id=caller_id,
                    workspace_path=workspace_path,
                    team_name=team_rec.name,
                    plan_task_id=task_id,
                    plan_id=plan_id,
                )
                team_rec.member_ids.append(agent_id)

            # Transition task to in_flight and persist.
            async with self._get_plan_lock(caller_id):
                _plans.mark_status(plan, task_id, "in_flight", agent_id=agent_id)

            # Launch the agent run task.
            rec.run_task = asyncio.create_task(
                self._run_agent_with_policy(rec, spec),
                name=f"agent-{agent_id}",
            )

        self.emit_event(
            "task_spawned",
            {
                "agent_id": caller_id,
                "plan_id": plan_id,
                "task_id": task_id,
                "spawned_agent_id": agent_id,
                "team_id": new_team_id,
                "ts": time.time(),
            },
        )

        remaining_ready = [
            t.id for t in plan.tasks.values() if t.status == "ready" and t.id != task_id
        ]
        return {
            "agent_id": agent_id,
            "team_id": new_team_id,
            "task_id": task_id,
            "remaining_ready": remaining_ready,
            "team_created": team_was_created,
        }

    def list_ready_tasks(self, *, caller_id: str) -> dict:
        """Return the ready task ids in caller's active plan.

        Read-only; no lock required.
        """
        plan_id = self._active_plan_by_agent.get(caller_id)
        if plan_id is None:
            raise PrimitiveError("no_active_plan", f"{caller_id} has no active plan")
        plan = self._plans[plan_id]
        ready = [t.id for t in plan.tasks.values() if t.status == "ready"]
        return {"ready": ready}

    async def mark_task_done(self, *, task_id: str) -> None:
        """Transition a plan task to done and unblock dependents.

        Called from terminate_child's success path (non-forced) in primitives/core.py
        when agent.plan_task_id is not None. Acquires the plan-owner's plan lock.

        NOTE TO NEXT SUBAGENT: The call site in primitives/core.py:terminate_child
        (around line 752, after inbox_put + emit_event in the success path) still
        needs to be wired up: ``if target_rec.plan_task_id is not None: await
        orch.mark_task_done(task_id=target_rec.plan_task_id)``.
        """
        plan = self._agent_owning_task(task_id)
        if plan is None:
            return
        async with self._get_plan_lock(plan.owner_agent_id):
            newly_ready = _plans.mark_status(plan, task_id, "done")
        self.emit_event(
            "task_done",
            {"plan_id": plan.plan_id, "task_id": task_id, "ts": time.time()},
        )
        for ready_id in newly_ready:
            self.emit_event(
                "task_ready",
                {"plan_id": plan.plan_id, "task_id": ready_id, "ts": time.time()},
            )

    async def mark_task_failed(self, *, task_id: str) -> None:
        """Transition a plan task to failed (force-terminate path).

        Dependents that listed this task remain permanently blocked.
        Called from terminate_child's force=True path in primitives/core.py.

        NOTE TO NEXT SUBAGENT: Wire in primitives/core.py:terminate_child's
        force path: ``if target_rec.plan_task_id is not None: await
        orch.mark_task_failed(task_id=target_rec.plan_task_id)``.
        """
        plan = self._agent_owning_task(task_id)
        if plan is None:
            return
        async with self._get_plan_lock(plan.owner_agent_id):
            _plans.mark_status(plan, task_id, "failed")
        self.emit_event(
            "task_failed",
            {"plan_id": plan.plan_id, "task_id": task_id, "ts": time.time()},
        )

    # ------------------------------------------------------------------
    # High-level entry points.
    # ------------------------------------------------------------------

    async def run_root(
        self,
        root_skill: str,
        root_task: str,
        *,
        model: Optional[str] = None,
    ) -> RunResult:
        """Spawn the root agent and block until it exits.

        The root agent can only be terminated by Beidou itself (on user
        signal) via :meth:`terminate_root`.
        """
        if self._root_id is not None:
            raise RuntimeError("run_root already invoked on this orchestrator")

        # Capture the originating user task once. Every member spawned later
        # (via spawn_team) reads this as their first user-role message so the
        # user's actual request is never lost in role-meta-descriptions.
        self._user_task = root_task

        effective_model = model or self._default_model

        root_agent_id = f"ag_{uuid.uuid4().hex[:8]}"
        root_agent_name = self._make_unique_name("root")

        # Emit collision warnings for any user-owned skill files that provision_skills
        # would overwrite in the project workspace.
        from beidou.skills.loader import provision_skills, _parse_skill_text
        skills_dst_dir = self.project_workspace / ".claude" / "skills"
        if skills_dst_dir.exists():
            for skill_md in sorted(self.skill_root.rglob("SKILL.md")):
                try:
                    bundled_bytes = skill_md.read_bytes()
                    loaded = _parse_skill_text(bundled_bytes.decode("utf-8"), skill_md)
                except Exception:
                    continue
                user_skill_path = skills_dst_dir / loaded.name / "SKILL.md"
                if user_skill_path.exists():
                    existing_bytes = user_skill_path.read_bytes()
                    if existing_bytes != bundled_bytes:
                        # If the destination starts with the Beidou bundled-origin
                        # marker, it's a stale copy from a previous run — NOT a
                        # user edit.  Overwrite silently.
                        if existing_bytes.startswith(b"<!-- beidou-bundled:"):
                            pass
                        else:
                            self.emit_event(
                                "config_warning",
                                {
                                    "agent_id": root_agent_id,
                                    "warning": "user_skill_overwritten",
                                    "skill": loaded.name,
                                    "ts": time.time(),
                                },
                            )

        # Provision all bundled skills into the project workspace so the SDK's
        # setting_sources=["project"] discovery can find them via .claude/skills/
        # (root agent cwd = project workspace).
        provision_skills(self.project_workspace, skill_root=self.skill_root)

        # Root agent is teamless: no TeamRecord is created, no team_created event
        # is emitted.  Per-agent scratch dir for artifacts / inbox files.
        root_workspace = agent_workspace(self.project_workspace, self.task_id, root_agent_id)

        rec = AgentRecord(
            agent_id=root_agent_id,
            task_id=self.task_id,
            team_id=None,
            role="root",
            skill_name=root_skill,
            model=effective_model,
            inbox=asyncio.Queue(),
            create_team_lock=asyncio.Lock(),
            name=root_agent_name,
        )
        self._register_agent_record(rec)
        self._root_id = root_agent_id

        spec = SpawnSpec(
            caller_id=root_agent_id,
            skill_name=root_skill,
            skill_root=self.skill_root,
            task=root_task,
            model=effective_model,
            template_vars={
                "role": "root",
                # Root has no role-specific scope; its scope IS the user task.
                # Keep this slot clean so {role_description} substitutes to
                # empty when the skill body uses the placeholder.
                "role_description": "",
                "team_name": "root",
                "workspace_path": str(root_workspace),
                "project_workspace_path": str(self.project_workspace),
                "leader_id": USER_SENTINEL,
            },
            cwd=str(self.project_workspace),
        )

        rec.run_task = asyncio.create_task(
            self._run_agent_with_policy(rec, spec),
            name=f"agent-{root_agent_id}",
        )

        return await rec.run_task

    async def terminate_root(self) -> None:
        """Post a terminate sentinel to the root agent's inbox.

        The cascade to every descendant is the root agent's responsibility
        (see ``docs/orchestration.md`` "Termination cascade"). We do NOT
        await the root task here — :meth:`shutdown` does.
        """
        if self._root_id is None:
            return
        if self._root_terminated:
            return
        self._root_terminated = True
        sentinel = Message(
            from_id="beidou",
            content="__terminate__",
            ts=time.time(),
            message_id=str(uuid.uuid4()),
            kind="terminate",
        )
        # bypass_cap semantics: from_id=="beidou" is the convention.
        await self.inbox_put(self._root_id, sentinel)
        self.emit_event(
            "terminate_posted",
            {
                "caller_id": "beidou",
                "agent_id": self._root_id,
                "message_id": sentinel.message_id,
                "ts": sentinel.ts,
            },
        )

    async def shutdown(self, *, grace_seconds: float = 30.0) -> None:
        """Graceful teardown.

        Posts a terminate sentinel to the root (if not already done), waits
        up to ``grace_seconds`` for the cascade to unwind, then cancels any
        stragglers. Finally drains the background emitter tasks.
        """
        await self.terminate_root()

        if self._root_id is not None:
            rec = self._agents.get(self._root_id)
            if rec is not None and rec.run_task is not None:
                try:
                    await asyncio.wait_for(rec.run_task, timeout=grace_seconds)
                except asyncio.TimeoutError:
                    rec.run_task.cancel()
                    try:
                        await rec.run_task
                    except (asyncio.CancelledError, Exception):
                        pass

        # Cancel any other run tasks still in flight (e.g. leaf agents whose
        # leaders never ack'd them). Best-effort.
        pending = [
            r.run_task for r in self._agents.values()
            if r.run_task is not None and not r.run_task.done()
        ]
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        # Drain the background emitter tasks.
        if self._bg_tasks:
            try:
                await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
            except Exception:
                pass


__all__ = [
    "Orchestrator",
    "AgentRecord",
    "TeamRecord",
    "USER_SENTINEL",
]
