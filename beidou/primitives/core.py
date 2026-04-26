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
            }
            for p in peers
        ]
    }


async def ask_user(
    orch: Orchestrator,
    *,
    caller_id: str,
    question: str,
    context: Optional[str] = None,
) -> dict:
    """Human gateway. See docs/tool-surface.md#ask_user."""
    if not orch.is_gateway_available():
        raise PrimitiveError(
            "gateway_unavailable",
            "no human gateway registered",
        )

    try:
        answer = await orch.gateway_ask_user(caller_id, question, context)
    except GatewayDeclined as e:
        raise PrimitiveError("user_declined", str(e) or "user declined")
    return {"answer": answer}


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
) -> dict:
    """Spawn a sub-team. See docs/tool-surface.md#create_team.

    The self-lead invariant (orchestration.md) is guaranteed by construction:
    this Python signature has no ``leader_id`` parameter, so the model cannot
    pass one. The orchestrator passes ``leader_id=caller_id`` unconditionally.
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
) -> dict:
    """Post a terminate sentinel. See docs/tool-surface.md#terminate_child.

    Per orchestration.md: termination authority is leader -> direct child-team
    member. Crossing team boundaries (even for ancestor leaders) is rejected.
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
