"""Pure-Python implementation of Beidou's agent primitives.

Each primitive is an ``async`` function that takes an explicit ``caller_id``
(bound by the MCP per-spawn closure in production - never read from model
input) and an ``orch`` Orchestrator handle. The MCP wrappers in a later step
will simply call these functions.

See ``docs/tool-surface.md`` for the canonical input/output schemas and
error codes. See ``docs/limits.md`` for the hard boundaries enforced here.

Summary of primitives (full spec in ``docs/tool-surface.md``):

* :func:`send_message`         -- A2A enqueue to recipient inbox.
* :func:`list_peers`           -- Snapshot of peers in ``team``/``children``/``all`` scope.
* :func:`ask_user`             -- Routes a question to the human gateway.
* :func:`report_status`        -- Records agent state and emits a status event.
* :func:`create_team`          -- Spawns a sub-team; caller becomes leader by construction.
* :func:`terminate_child`      -- Posts a terminate sentinel to a direct child.
* :func:`list_pending_reviews` -- Read-only list of direct children awaiting leader review.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Limits constants. These mirror docs/limits.md. Changing any of them requires
# user approval AND an update to docs/limits.md in the same commit. Do not
# edit in isolation.
# ---------------------------------------------------------------------------

INBOX_CAP = 1000              # limits.md #3
FAN_OUT_CAP = 8               # limits.md #1
MAX_DEPTH = 5                 # limits.md #2
CONTRACT_STRIKES = 3          # limits.md #5 (used by orchestrator, not primitives)


# ---------------------------------------------------------------------------
# Errors, message/peer data shapes, Orchestrator protocol.
# ---------------------------------------------------------------------------


class PrimitiveError(Exception):
    """Base for structured tool errors.

    ``.code`` is the ``error_code`` from docs/tool-surface.md. The MCP wrapper
    layer serialises this into a structured tool error the model can react to.
    """

    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        self.code = code
        self.message = message or code
        self.details = details
        super().__init__(f"{code}: {self.message}")


class GatewayDeclined(Exception):
    """Dedicated signal from a human gateway that the user refused the question.

    The primitive translates this to :class:`PrimitiveError` with code
    ``user_declined`` (see tool-surface.md).
    """


@dataclass
class Message:
    from_id: str        # sender agent_id, or "beidou" for system messages
    content: str
    ts: float           # unix ts
    message_id: str
    kind: str = "user"  # "user" | "terminate"


@dataclass
class Peer:
    agent_id: str
    role: str
    team_id: str
    status: str              # "working" | "idle" | "blocked" | "done" | "unknown"
    is_leader_of: list[str] = field(default_factory=list)  # team_ids the agent leads
    name: str | None = None  # human-readable display name, e.g. "frontend-engineer-a3b2"


@runtime_checkable
class Orchestrator(Protocol):
    """Minimal structural interface the primitives need.

    The real orchestrator lands in a later step. Anything that satisfies
    this Protocol (duck-typed) can drive the primitives end to end -- which
    is exactly how the test suite's ``FakeOrchestrator`` works.
    """

    # --- Registry lookups ---------------------------------------------------
    def agent_exists(self, agent_id: str) -> bool: ...
    def agent_task(self, agent_id: str) -> str: ...           # task_id
    def agent_team(self, agent_id: str) -> str: ...           # parent team_id
    def leader_of(self, team_id: str) -> str: ...             # leader agent_id
    def teams_led_by(self, agent_id: str) -> list[str]: ...
    def team_depth(self, team_id: str) -> int: ...            # 0 for root team
    def team_members(self, team_id: str) -> list[str]: ...
    def peer_snapshot(self, agent_id: str, scope: str) -> list[Peer]: ...

    # --- Inbox operations (per-recipient asyncio.Queue) ---------------------
    async def inbox_put(self, recipient: str, msg: Message) -> None: ...
    def inbox_size(self, agent_id: str) -> int: ...

    # --- Team creation / termination ---------------------------------------
    async def spawn_team(
        self,
        leader_id: str,
        name: str,
        task: str,
        roles: list[dict],
        rules: list[str],
    ) -> dict: ...
    async def create_team_lock(self, agent_id: str) -> asyncio.Lock: ...

    # --- Observability / human gateway -------------------------------------
    def emit_event(self, name: str, payload: dict) -> None: ...
    async def gateway_ask_user(
        self,
        caller_id: str,
        question: str,
        context: Optional[str],
    ) -> str: ...
    async def gateway_ask_user_structured(
        self,
        caller_id: str,
        questions: list[dict],
        context: Optional[str],
    ) -> dict:
        # TODO(issue-744): concrete impl wires this to QuestionBroker.ask().
        ...
    def is_gateway_available(self) -> bool: ...
    def record_status(
        self,
        caller_id: str,
        state: str,
        detail: Optional[str],
    ) -> None: ...

    # --- Termination tracking ---------------------------------------------
    # True once the agent has consumed a terminate sentinel from its inbox.
    # The drain loop in beidou/sdk_agent.py queries this after query() exits
    # to distinguish a valid terminate-driven end_turn from a contract
    # violation. See docs/agent-runtime.md section 5.
    def was_terminated(self, caller_id: str) -> bool: ...

    # --- Completion-review accessors (used by list_pending_reviews) --------
    def agent_skill_name(self, agent_id: str) -> str: ...
    def agent_completion_pending(self, agent_id: str) -> bool: ...
    def agent_completion_pending_ts(self, agent_id: str) -> Optional[float]: ...
    def agent_last_status_detail(self, agent_id: str) -> str: ...

    # --- Name accessor (used by list_peers, list_pending_reviews) ----------
    def agent_name(self, agent_id: str) -> str | None: ...


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _iso_from(ts: float) -> str:
    """Unix seconds -> ISO-8601 UTC string used in tool outputs."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _msg_to_out(m: Message) -> dict:
    return {
        "from": m.from_id,
        "content": m.content,
        "ts": _iso_from(m.ts),
        "kind": m.kind,
    }


# ---------------------------------------------------------------------------
# Primitives.
# ---------------------------------------------------------------------------


async def send_message(
    orch: Orchestrator,
    *,
    caller_id: str,
    to: str,
    content: str,
) -> dict:
    """A2A primitive. See docs/tool-surface.md#send_message."""
    if not orch.agent_exists(to):
        raise PrimitiveError("unknown_recipient", f"no such agent: {to}", to=to)

    # Same-task check. send_message is unrestricted on topology (cross-team
    # ok) but strictly scoped to the same task.
    if orch.agent_task(caller_id) != orch.agent_task(to):
        raise PrimitiveError(
            "task_mismatch",
            f"{to} belongs to a different task",
            to=to,
        )

    # Inbox cap -- see limits.md #3. On overflow the SENDER gets the error;
    # the recipient is never crashed for over-capacity.
    if orch.inbox_size(to) >= INBOX_CAP:
        raise PrimitiveError(
            "inbox_full",
            f"{to}'s inbox is at cap ({INBOX_CAP})",
            to=to,
            cap=INBOX_CAP,
        )

    message_id = str(uuid.uuid4())
    msg = Message(
        from_id=caller_id,
        content=content,
        ts=time.time(),
        message_id=message_id,
        kind="user",
    )
    await orch.inbox_put(to, msg)
    orch.emit_event(
        "message_sent",
        {
            "from": caller_id,
            "to": to,
            "message_id": message_id,
            "ts": msg.ts,
        },
    )
    orch.emit_event(
        "send_message",
        {
            "ts": time.time(),
            "caller_id": caller_id,
            "to": to,
            "content": content,
            "message_id": message_id,
        },
    )
    return {"delivered": True, "message_id": message_id}


async def list_peers(
    orch: Orchestrator,
    *,
    caller_id: str,
    scope: str = "team",
) -> dict:
    """Peer snapshot. See docs/tool-surface.md#list_peers."""
    if scope not in ("team", "children", "all"):
        # tool-surface.md documents no "invalid_scope" code today but the
        # spec's enum is explicit; returning a structured error is cheaper
        # than silently widening the scope.
        raise PrimitiveError("invalid_scope", f"unknown scope: {scope}", scope=scope)

    peers = orch.peer_snapshot(caller_id, scope)
    return {
        "peers": [
            {
                "agent_id": p.agent_id,
                "role": p.role,
                "team_id": p.team_id,
                "status": p.status,
                "is_leader_of": list(p.is_leader_of),
                "name": p.name,
            }
            for p in peers
        ]
    }


async def ask_user(
    orch: Orchestrator,
    *,
    caller_id: str,
    questions: list[dict],
    context: Optional[str] = None,
) -> dict:
    """Human gateway. See docs/tool-surface.md#ask_user.

    questions: list[1..4] of {question:str, header:str(<=12 chars), multiSelect:bool,
                              options: list[{label:str, description:str}] (length 0 or 2..4)}.
    Returns: {"answers": [...], "answer_text": "..."} from the broker resolver.
    """
    # --- Input validation -------------------------------------------------
    if not isinstance(questions, list) or not (1 <= len(questions) <= 4):
        raise PrimitiveError(
            "invalid_input",
            "questions must be a list of 1..4 items",
            provided_length=len(questions) if isinstance(questions, list) else None,
        )

    for i, sq in enumerate(questions):
        if not isinstance(sq, dict):
            raise PrimitiveError(
                "invalid_input",
                f"questions[{i}] must be a dict",
                index=i,
            )

        q_text = sq.get("question")
        if not isinstance(q_text, str) or not q_text:
            raise PrimitiveError(
                "invalid_input",
                f"questions[{i}].question must be a non-empty string",
                index=i,
                field="question",
            )

        header = sq.get("header", "")
        if not isinstance(header, str):
            raise PrimitiveError(
                "invalid_input",
                f"questions[{i}].header must be a string",
                index=i,
                field="header",
            )
        if len(header) > 12:
            raise PrimitiveError(
                "invalid_input",
                f"questions[{i}].header must be <= 12 characters (got {len(header)})",
                index=i,
                field="header",
                length=len(header),
            )

        multi_select = sq.get("multiSelect", False)
        if not isinstance(multi_select, bool):
            raise PrimitiveError(
                "invalid_input",
                f"questions[{i}].multiSelect must be a bool",
                index=i,
                field="multiSelect",
            )

        options = sq.get("options", [])
        if not isinstance(options, list):
            raise PrimitiveError(
                "invalid_input",
                f"questions[{i}].options must be a list",
                index=i,
                field="options",
            )
        if len(options) not in (0, 2, 3, 4):
            raise PrimitiveError(
                "invalid_input",
                f"questions[{i}].options length must be 0 (free-text) or 2..4 (choice), "
                f"got {len(options)}",
                index=i,
                field="options",
                length=len(options),
            )
        for j, opt in enumerate(options):
            if not isinstance(opt, dict):
                raise PrimitiveError(
                    "invalid_input",
                    f"questions[{i}].options[{j}] must be a dict",
                    index=i,
                    option_index=j,
                )
            label = opt.get("label")
            description = opt.get("description")
            if not isinstance(label, str):
                raise PrimitiveError(
                    "invalid_input",
                    f"questions[{i}].options[{j}].label must be a string",
                    index=i,
                    option_index=j,
                    field="label",
                )
            if not isinstance(description, str):
                raise PrimitiveError(
                    "invalid_input",
                    f"questions[{i}].options[{j}].description must be a string",
                    index=i,
                    option_index=j,
                    field="description",
                )

    # --- Gateway check ---------------------------------------------------
    if not orch.is_gateway_available():
        raise PrimitiveError(
            "gateway_unavailable",
            "no human gateway registered",
        )

    try:
        result = await orch.gateway_ask_user_structured(caller_id, questions, context)
    except GatewayDeclined as e:
        raise PrimitiveError("user_declined", str(e) or "user declined")
    return result


async def report_status(
    orch: Orchestrator,
    *,
    caller_id: str,
    state: str,
    detail: Optional[str] = None,
) -> dict:
    """Record state + emit event. See docs/tool-surface.md#report_status."""
    if state not in ("working", "idle", "blocked", "done"):
        raise PrimitiveError("invalid_state", f"unknown state: {state}", state=state)

    # tool-surface.md calls detail "required in practice" when state==done
    # but does NOT enforce -- we record whatever we're given.
    orch.record_status(caller_id, state, detail)
    orch.emit_event(
        "status",
        {
            "agent_id": caller_id,
            "state": state,
            "detail": detail,
            "ts": time.time(),
        },
    )
    return {"recorded": True}


async def create_team(
    orch: Orchestrator,
    *,
    caller_id: str,
    name: str,
    task: str,
    roles: list[dict],
    rules: Optional[list[str]] = None,
    consensus: bool = False,
) -> dict:
    """Spawn a sub-team. See docs/tool-surface.md#create_team.

    The self-lead invariant (orchestration.md) is guaranteed by construction:
    this Python signature has no ``leader_id`` parameter, so the model cannot
    pass one. The orchestrator passes ``leader_id=caller_id`` unconditionally.

    ``consensus=True`` opts into legitimate "N parallel attempts" scenarios
    (voting, ensemble) where all members intentionally share the same task.
    Without it, N>1 roles that all share the same (skill, description) tuple
    are rejected as a footgun guard (see ``duplicate_member_descriptions``).
    """
    # Fan-out cap -- limits.md #1.
    if len(roles) > FAN_OUT_CAP:
        raise PrimitiveError(
            "fanout_exceeded",
            f"{len(roles)} roles exceeds fan-out cap {FAN_OUT_CAP} (limits.md #1)",
            got=len(roles),
            cap=FAN_OUT_CAP,
        )

    # Depth cap -- limits.md #2.
    caller_team = orch.agent_team(caller_id)
    caller_depth = orch.team_depth(caller_team)
    if caller_depth + 1 > MAX_DEPTH:
        raise PrimitiveError(
            "depth_exceeded",
            f"caller at depth {caller_depth}; new team would be {caller_depth + 1} "
            f"(max {MAX_DEPTH}, limits.md #2)",
            caller_depth=caller_depth,
            max_depth=MAX_DEPTH,
        )

    rules_list = list(rules) if rules else []

    # Defensive: although Python signature has no leader_id, the spec calls
    # out that any "leader_id" arriving in tool input must be rejected. A
    # malformed rules list carrying a dict with that key is the only way
    # such a payload could sneak in through this function.
    for r in rules_list:
        if isinstance(r, dict) and "leader_id" in r:
            raise PrimitiveError(
                "leader_override_attempted",
                "rules may not carry leader_id overrides",
            )

    # Duplicate-description guard -- tool-surface.md#create_team.
    # When N>1 roles all share the same (skill, description) tuple and the
    # caller has not opted into consensus mode, every member would redundantly
    # implement the whole task in parallel.  Reject early to prevent that
    # footgun.  Uses .get() so roles that omit 'skill' or 'description' still
    # compare correctly (missing fields all collapse to None).
    if not consensus and len(roles) > 1:
        sigs = {(r.get("skill"), r.get("description")) for r in roles}
        if len(sigs) == 1:
            raise PrimitiveError(
                "duplicate_member_descriptions",
                "All roles share the same (skill, description); every member "
                "would redundantly implement the whole task. Either write "
                "distinct descriptions for each role, or pass consensus=true "
                "if parallel attempts are genuinely intentional (e.g. voting).",
            )

    # One in-flight create_team per caller -- limits.md #6. asyncio.Lock has
    # no acquire_nowait(); use .locked() to detect contention, then take the
    # lock. Because this coroutine runs without awaiting between the check
    # and the acquire (neither is a suspension point), this is race-free on
    # a single event loop.
    lock = await orch.create_team_lock(caller_id)
    if lock.locked():
        raise PrimitiveError(
            "concurrent_create_team",
            f"{caller_id} already has an in-flight create_team (limits.md #6)",
        )
    await lock.acquire()
    try:
        # The orchestrator owns skill resolution. Propagate
        # unknown_skill as-is if it raises PrimitiveError; everything
        # else bubbles.
        result = await orch.spawn_team(
            leader_id=caller_id,
            name=name,
            task=task,
            roles=roles,
            rules=rules_list,
        )
    finally:
        lock.release()

    return result


async def terminate_child(
    orch: Orchestrator,
    *,
    caller_id: str,
    agent_id: str,
    force: bool = False,
) -> dict:
    """Post a terminate sentinel. See docs/tool-surface.md#terminate_child.

    Per orchestration.md: termination authority is leader -> direct child-team
    member. Crossing team boundaries (even for ancestor leaders) is rejected.

    The plain (force=False) call requires the child to have called
    report_status(state="done") first (completion_pending=True) or to already
    have consumed a prior terminate (terminate_consumed=True).  Pass
    force=True to override; an audited terminate.forced event is emitted.
    """
    if not orch.agent_exists(agent_id):
        raise PrimitiveError("unknown_agent", f"no such agent: {agent_id}", agent_id=agent_id)

    target_team = orch.agent_team(agent_id)
    if orch.leader_of(target_team) != caller_id:
        raise PrimitiveError(
            "not_leader",
            f"{caller_id} does not lead team {target_team}",
            caller_id=caller_id,
            target_team=target_team,
        )

    # Completion gate: child must have reported done (or already been
    # terminated) unless the caller explicitly passes force=True.
    target_rec = orch._agents.get(agent_id)
    # Defensive (target_rec should exist if agent_exists passed) — keep simple.
    target_pending = bool(target_rec and target_rec.completion_pending)
    target_terminated = bool(target_rec and target_rec.terminate_consumed)
    if not force and not target_pending and not target_terminated:
        raise PrimitiveError(
            "child_not_pending_review",
            f"{agent_id} has not called report_status(state='done'). "
            f"Wait for completion review, send a rework message, or pass force=true.",
            agent_id=agent_id,
            caller_id=caller_id,
        )

    # Emit audited event when force overrides the gate.
    if force and not target_pending and not target_terminated:
        orch.emit_event(
            "terminate.forced",
            {
                "caller_id": caller_id,
                "agent_id": agent_id,
                "team_id": target_team,
                "reason": "leader_force",
                "ts": time.time(),
            },
        )

    # Idempotency: tool-surface.md defines an ``already_terminating`` code.
    # We can't tell reliably without draining the inbox, so for v1 we accept
    # the double-post and emit a warning event if we detect size >= 1 that
    # looks suspicious. Keeping this simple per the task brief.
    if orch.inbox_size(agent_id) >= INBOX_CAP:
        orch.emit_event(
            "terminate_possibly_duplicate",
            {"caller_id": caller_id, "agent_id": agent_id},
        )

    sentinel = Message(
        from_id="beidou",
        content="__terminate__",
        ts=time.time(),
        message_id=str(uuid.uuid4()),
        kind="terminate",
    )
    await orch.inbox_put(agent_id, sentinel)
    orch.emit_event(
        "terminate_posted",
        {
            "caller_id": caller_id,
            "agent_id": agent_id,
            "message_id": sentinel.message_id,
            "ts": sentinel.ts,
        },
    )
    return {"sentinel_posted": True}


def list_pending_reviews(
    orch: Orchestrator,
    *,
    caller_id: str,
) -> list[dict]:
    """Return direct children of ``caller_id`` that are awaiting review.

    "Direct children" are members of any team that ``caller_id`` leads
    (via ``orch.teams_led_by``). Only those with ``completion_pending=True``
    are returned. The caller itself is excluded even if it somehow appears
    as a member of one of its own teams.

    Each entry is a dict with:
      - ``agent_id``              -- the child's agent id
      - ``role``                  -- skill name (from ``agent_skill_name``)
      - ``completion_pending_ts`` -- unix float when the child called done, or null
      - ``age_s``                 -- seconds since completion_pending_ts, or null
      - ``summary``               -- ``last_status_detail`` text (may be empty string)

    Results are sorted ascending by ``completion_pending_ts`` (oldest first).
    A ``None`` ts sorts after all timestamped entries.

    Returns ``[]`` if no direct children have ``completion_pending=True``.
    See ``docs/tool-surface.md#list_pending_reviews``.
    """
    now = time.time()
    pending: list[dict] = []

    for team_id in orch.teams_led_by(caller_id):
        for member_id in orch.team_members(team_id):
            if member_id == caller_id:
                continue
            if not orch.agent_completion_pending(member_id):
                continue
            ts = orch.agent_completion_pending_ts(member_id)
            age_s = (now - ts) if ts is not None else None
            pending.append(
                {
                    "agent_id": member_id,
                    "role": orch.agent_skill_name(member_id),
                    "completion_pending_ts": ts,
                    "age_s": age_s,
                    "summary": orch.agent_last_status_detail(member_id),
                    "name": orch.agent_name(member_id),
                }
            )

    # Sort by ts ascending; None ts sorts last.
    pending.sort(key=lambda d: (d["completion_pending_ts"] is None, d["completion_pending_ts"] or 0))
    return pending


__all__ = [
    # constants
    "INBOX_CAP",
    "FAN_OUT_CAP",
    "MAX_DEPTH",
    "CONTRACT_STRIKES",
    # errors / data
    "PrimitiveError",
    "GatewayDeclined",
    "Message",
    "Peer",
    "Orchestrator",
    # primitives
    "send_message",
    "list_peers",
    "ask_user",
    "report_status",
    "create_team",
    "terminate_child",
    "list_pending_reviews",
]
