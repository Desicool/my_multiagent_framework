"""Concrete Orchestrator implementing ``beidou.primitives.core.Orchestrator``.

Owns the agent registry, team graph, per-agent inboxes, per-agent create_team
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

from beidou import sdk_agent
from beidou.events import EventEmitter
from beidou.primitives.core import (
    CONTRACT_STRIKES,
    INBOX_CAP,
    Message,
    Peer,
    PrimitiveError,
)
from beidou.sdk_agent import RunResult, SpawnSpec
from beidou.workspace import agent_workspace, team_workspace

if TYPE_CHECKING:  # pragma: no cover
    from beidou.gateways.base import BaseGateway


# Sentinel leader-id for the root agent.  The root agent has no team and no
# leader; USER_SENTINEL is stored as ``leader_id`` in any context object that
# needs a "who leads the root?" answer (e.g. spawn context template_vars).
USER_SENTINEL = "__user__"

# Limits.md #8: per-agent token ceiling.
TOKEN_CEILING = 1_000_000

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
    review_ping_count: int = 0
    # Watchdog fields (bd issue qj2).
    inflight_tools: int = 0
    idle_nudge_count: int = 0


@dataclass
class TeamRecord:
    team_id: str
    name: str
    task: str
    leader_id: str                    # MUST equal caller_id of create_team
    depth: int                        # 0 for root
    member_ids: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    parent_team_id: Optional[str] = None


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

        # Lazily-created per-agent create_team locks.
        self._locks: dict[str, asyncio.Lock] = {}

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
    # Protocol: team spawn
    # ------------------------------------------------------------------

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
                # Loader itself blew up for some non-SkillError reason — let
                # it propagate; it's not a primitive-visible condition.
                raise

        # Workspace for the new team.
        workspace_path = team_workspace(self.project_workspace, self.task_id, team_id)

        # Provision all bundled skills into the team workspace so the SDK's
        # setting_sources=["project"] discovery can find them via .claude/skills/.
        from beidou.skills.loader import provision_skills
        provision_skills(workspace_path, skill_root=self.skill_root)

        task_id = self.agent_task(leader_id)
        for r in roles:
            agent_id = f"ag_{uuid.uuid4().hex[:8]}"
            role_name = r.get("role", "member")
            role_desc = r.get("description", "")
            model = r.get("model") or self._default_model
            skill_name = r.get("skill") or r.get("template", "")  # accept deprecated "template" key
            agent_name = self._make_unique_name(role_name)

            rec = AgentRecord(
                agent_id=agent_id,
                task_id=task_id,
                team_id=team_id,
                role=role_name,
                skill_name=skill_name,
                model=model,
                inbox=asyncio.Queue(),
                create_team_lock=asyncio.Lock(),
                name=agent_name,
            )
            self._register_agent_record(rec)
            members_out.append({"agent_id": agent_id, "role": role_name, "name": agent_name})

            # First user message = originating user task (propagated from
            # run_root). The team-level `task` arg from create_team is kept on
            # TeamRecord for orchestrator-internal coordination but is no
            # longer the agent's first user message — that role belongs to
            # the actual user request.
            spec = SpawnSpec(
                caller_id=agent_id,
                skill_name=skill_name,
                skill_root=self.skill_root,
                task=self._user_task or task,
                model=model,
                template_vars={
                    "role": role_name,
                    "role_description": role_desc,
                    "team_name": name,
                    "workspace_path": str(workspace_path),
                    "project_workspace_path": str(self.project_workspace),
                    "leader_id": leader_id,
                },
                cwd=str(workspace_path),
            )
            new_records.append((rec, spec))

        self._teams[team_id] = TeamRecord(
            team_id=team_id,
            name=name,
            task=task,
            leader_id=leader_id,
            depth=new_depth,
            member_ids=[m["agent_id"] for m in members_out],
            rules=list(rules),
            parent_team_id=parent_team_id,
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

        # Launch runs AFTER team registry is populated so any early LLM call
        # from a member sees the right topology.
        for rec, spec in new_records:
            rec.run_task = asyncio.create_task(
                self._run_agent_with_policy(rec, spec),
                name=f"agent-{rec.agent_id}",
            )

        return {"team_id": team_id, "members": members_out}

    async def create_team_lock(self, agent_id: str) -> asyncio.Lock:
        lock = self._locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_id] = lock
        return lock

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

        if rec.total_tokens < TOKEN_CEILING:
            return

        # Root agent has no leader to recommend to; escalate via gateway /
        # dedicated event for the CLI to pick up.
        if rec.agent_id == self._root_id:
            # Fire exactly once per run: strip the flag via a marker.
            if not getattr(rec, "_token_ceiling_fired", False):
                rec._token_ceiling_fired = True  # type: ignore[attr-defined]
                # Emit only — don't recurse via emit_event (we're called
                # from inside it).
                self._schedule_emit(
                    "root_token_ceiling_reached",
                    rec.agent_id,
                    rec.team_id,
                    {"total_tokens": rec.total_tokens, "ts": time.time()},
                )
            return

        if getattr(rec, "_token_ceiling_fired", False):
            return
        rec._token_ceiling_fired = True  # type: ignore[attr-defined]
        leader_id = self.leader_of(rec.team_id)
        msg = Message(
            from_id="beidou",
            content=(
                f"agent {rec.agent_id} exceeded token ceiling "
                f"({rec.total_tokens} >= {TOKEN_CEILING}). "
                f"Consider terminate_child({rec.agent_id})."
            ),
            ts=time.time(),
            message_id=str(uuid.uuid4()),
            kind="user",
        )
        # Schedule on the loop without blocking emit_event's caller.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        t = loop.create_task(self.inbox_put(leader_id, msg))
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

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
        if self.gateway is None:
            raise PrimitiveError("gateway_unavailable", "no human gateway registered")
        # BaseGateway's surface_question wants a Question + broker; that
        # plumbing lives in QuestionBroker. The orchestrator-level gateway
        # hook here is the simpler "ask one thing, block for answer" shape
        # that the primitive expects. Implementations that only expose the
        # broker-style surface should adapt in their own wrapper; we call an
        # optional ``ask`` coroutine when present.
        ask = getattr(self.gateway, "ask", None)
        if ask is None:
            raise PrimitiveError("gateway_unavailable", "gateway has no ask() method")
        return await ask(caller_id, question, context)

    async def gateway_ask_user_structured(
        self,
        caller_id: str,
        questions: list[dict],
        context: Optional[str],
    ) -> dict:
        """Route a structured questions list through the QuestionBroker.

        Delegates to ``_GatewayAdapter.ask_structured`` (or any object that
        exposes that coroutine) so both SDK-builtin ``AskUserQuestion`` and
        ``mcp__beidou__ask_user`` land at the same ``broker._pending`` entry.

        Returns ``{"answers": [...], "answer_text": "..."}`` as produced by
        ``QuestionBroker.resolve_answer``.
        """
        if self.gateway is None:
            raise PrimitiveError("gateway_unavailable", "no human gateway registered")
        ask_structured = getattr(self.gateway, "ask_structured", None)
        if ask_structured is None:
            raise PrimitiveError("gateway_unavailable", "gateway has no ask_structured() method")
        return await ask_structured(caller_id, questions, context)

    def is_gateway_available(self) -> bool:
        return self.gateway is not None and hasattr(self.gateway, "ask")

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
                # Escalate to user gateway.
                self.emit_event(
                    "review.escalated_to_user",
                    {"agent_id": child_id, "team_id": rec.team_id, "leader_id": leader, "ts": now},
                )
                if self.is_gateway_available():
                    try:
                        await self.gateway_ask_user_structured(
                            leader,
                            [
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
                            ],
                            None,
                        )
                    except Exception:
                        # Best-effort.
                        pass
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
            # Skip if recent progress.
            if now - rec.last_progress_ts < IDLE_NUDGE_S:
                continue
            # Skip if a tool is currently running.
            if rec.inflight_tools > 0:
                continue
            # Skip if completion is pending — Pass A handles it.
            if rec.completion_pending:
                continue
            # Skip if already escalated.
            if rec.idle_nudge_count >= MAX_PINGS_BEFORE_ESCALATION:
                continue
            # Skip pure workers (no direct children) that are not root.
            teams_led = list(self.teams_led_by(agent_id))
            if len(teams_led) == 0 and agent_id != self._root_id:
                continue

            delta_s = int(now - rec.last_progress_ts)

            if rec.idle_nudge_count < 2:
                body = (
                    f"[BEIDOU LIVENESS CHECK] You have been idle {delta_s}s with no progress.\n"
                    f"Either:\n"
                    f"  (a) call terminate_child / send_message to a pending child, or\n"
                    f"  (b) call create_team to start the next phase, or\n"
                    f"  (c) emit a final completion message and call report_status(done) if this is your final state, or\n"
                    f"  (d) call ask_user if you are blocked on missing input.\n"
                    f"Choose one and act on this turn. Do not respond with \"still waiting\"."
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
                # Escalate to user gateway.
                self.emit_event(
                    "liveness.escalated_to_user",
                    {"agent_id": agent_id, "team_id": rec.team_id, "delta_s": delta_s, "ts": now},
                )
                if self.is_gateway_available():
                    try:
                        await self.gateway_ask_user_structured(
                            agent_id,
                            [
                                {
                                    "question": (
                                        f"Agent {agent_id} has been idle {delta_s}s with no progress."
                                        f" Approve continuation, redirect, or abort?"
                                    ),
                                    "header": "",
                                    "multiSelect": False,
                                    "options": [],
                                }
                            ],
                            None,
                        )
                    except Exception:
                        pass

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
        """
        current_spec = spec
        last_result: Optional[RunResult] = None
        while True:
            # Looked up lazily on every iteration so tests can monkeypatch
            # ``beidou.sdk_agent.run_agent``.
            result = await sdk_agent.run_agent(self, current_spec)
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
    "TOKEN_CEILING",
]
