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
* :func:`signal_review`        -- Signals work is ready for leader review.
* :func:`request_termination`  -- Requests final lifecycle termination.
* :func:`report_status`        -- DEPRECATED for done; records agent state and emits a status event.
* :func:`declare_plan`         -- Validate DAG + persist plan; no team/agent created.
* :func:`remove_plan`          -- Remove caller's active plan (hard-fork for replanning).
* :func:`spawn_agent`          -- Gated spawn from plan; lazily creates team on first call.
* :func:`list_ready`           -- Read-only: return ready task ids in caller's active plan.
* :func:`create_team`          -- DEPRECATED. Use declare_plan + spawn_agent.
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
MAX_DEPTH = 8                 # limits.md #2
CONTRACT_STRIKES = 3          # limits.md #5 (used by orchestrator, not primitives)
CRASH_STRIKES = 3             # agent-runtime.md §5.1; NOT in limits.md.


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
    kind: str = "user"  # "user" | "terminate" | "inquiry"


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
    async def spawn_lock(self, agent_id: str) -> asyncio.Lock: ...

    # --- Plan lifecycle (called by plan primitives) -------------------------
    async def register_plan(self, *, caller_id: str, specs: list[dict]) -> dict: ...
    async def remove_active_plan(self, *, caller_id: str) -> dict: ...
    async def spawn_for_task(self, *, caller_id: str, task_id: str) -> dict: ...
    def list_ready_tasks(self, *, caller_id: str) -> dict: ...
    async def mark_task_done(self, *, task_id: str) -> None: ...
    async def mark_task_failed(self, *, task_id: str) -> None: ...

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
        # Implemented on the Orchestrator; delegated via gateway_ask_via_chain.
        ...
    def is_gateway_available(self) -> bool: ...
    def record_status(
        self,
        caller_id: str,
        state: str,
        detail: Optional[str],
    ) -> None: ...

    # --- Question routing (replaces QuestionBroker) -----------------------
    def parent_for_chain(self, caller_id: str) -> Optional[str]: ...
    async def forward_question(
        self,
        *,
        qid: str,
        by_id: str,
        new_target_id: Optional[str],
        reason: str,
    ) -> dict: ...
    def resolve_question(self, qid: str, answers: list[dict]) -> dict: ...

    # --- Termination tracking ---------------------------------------------
    # True once the agent has consumed a terminate sentinel from its inbox.
    # The drain loop in beidou/sdk_agent.py queries this after query() exits
    # to distinguish a valid terminate-driven end_turn from a contract
    # violation. See docs/agent-runtime.md section 5.
    def was_terminated(self, caller_id: str) -> bool: ...

    # --- Completion-review accessors (used by list_pending_reviews) --------
    def agent_skill_name(self, agent_id: str) -> str: ...
    def agent_review_pending(self, agent_id: str) -> bool: ...
    def agent_review_pending_ts(self, agent_id: str) -> Optional[float]: ...
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
    expects_reply: bool = False,
) -> dict:
    """A2A primitive. See docs/tool-surface.md#send_message.

    When ``expects_reply`` is True, the Message is created with
    ``kind="inquiry"`` so the recipient's input stream can render it as a
    reply-expected inquiry and register a reply obligation.
    """
    if not orch.agent_exists(to):
        raise PrimitiveError(
            "unknown_recipient",
            f"no such agent: {to}. Use mcp__beidou__list_peers to discover "
            f"reachable agents. Spec: docs/tool-surface.md#send_message.",
            to=to,
        )

    # Same-task check. send_message is unrestricted on topology (cross-team
    # ok) but strictly scoped to the same task.
    if orch.agent_task(caller_id) != orch.agent_task(to):
        raise PrimitiveError(
            "task_mismatch",
            f"{to} belongs to a different task. send_message is scoped to "
            f"the same task; check list_peers(scope='all') for in-task peers. "
            f"Spec: docs/tool-surface.md#send_message.",
            to=to,
        )

    # Inbox cap -- see limits.md #3. On overflow the SENDER gets the error;
    # the recipient is never crashed for over-capacity.
    if orch.inbox_size(to) >= INBOX_CAP:
        raise PrimitiveError(
            "inbox_full",
            f"{to}'s inbox is at cap ({INBOX_CAP}); recipient is over-loaded. "
            f"Retry after the recipient drains. Spec: docs/limits.md #3.",
            to=to,
            cap=INBOX_CAP,
        )

    message_id = str(uuid.uuid4())
    msg = Message(
        from_id=caller_id,
        content=content,
        ts=time.time(),
        message_id=message_id,
        kind="inquiry" if expects_reply else "user",
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
        raise PrimitiveError(
            "invalid_scope",
            f"unknown scope: {scope}. Valid: 'team', 'children', 'all'. "
            f"Spec: docs/tool-surface.md#list_peers.",
            scope=scope,
        )

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
    Returns: {"answers": [...], "answer_text": "..."} from the registry resolver.
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
            "no human gateway registered for this task; ask_user cannot be "
            "satisfied. Spec: docs/tool-surface.md#ask_user.",
        )

    # Agent-originated ask_user goes through the leader chain so the
    # caller's leader sees the question first and can answer directly if it
    # already knows — avoiding redundant user prompts. Falls back to
    # direct-to-user routing when the caller has no leader (root).
    try:
        result = await orch.gateway_ask_via_chain(caller_id, questions, context)
    except GatewayDeclined as e:
        raise PrimitiveError(
            "user_declined",
            (str(e) or "user declined") + " Spec: docs/tool-surface.md#ask_user.",
        )
    return result


async def answer_question(
    orch: Orchestrator,
    *,
    caller_id: str,
    qid: str,
    answers: list[dict],
    reason: str,
) -> dict:
    """Resolve a question that landed in the caller's inbox via the leader chain.

    See ``docs/tool-surface.md#answer_question``. Any agent currently holding
    the question (i.e. chain[-1] == caller_id) may resolve it.
    """
    if not isinstance(qid, str) or not qid:
        raise PrimitiveError("invalid_input", "qid must be a non-empty string")
    if not isinstance(answers, list) or not answers:
        raise PrimitiveError(
            "invalid_input", "answers must be a non-empty list of {selected_labels, text} dicts"
        )
    for i, ans in enumerate(answers):
        if not isinstance(ans, dict):
            raise PrimitiveError("invalid_input", f"answers[{i}] must be a dict", index=i)
        sl = ans.get("selected_labels", [])
        if not isinstance(sl, list) or not all(isinstance(x, str) for x in sl):
            raise PrimitiveError(
                "invalid_input",
                f"answers[{i}].selected_labels must be a list[str]",
                index=i,
            )
        text = ans.get("text", None)
        if text is not None and not isinstance(text, str):
            raise PrimitiveError(
                "invalid_input", f"answers[{i}].text must be a string or null", index=i
            )

    if not isinstance(reason, str) or not reason.strip():
        raise PrimitiveError(
            "invalid_input", "reason must be a non-empty string explaining why you can answer directly"
        )

    registry = getattr(orch, "_questions", None)
    if registry is None:
        raise PrimitiveError(
            "gateway_unavailable",
            "orchestrator has no question registry. "
            "Spec: docs/tool-surface.md#answer_question.",
        )
    pq = registry.get(qid)
    if pq is None:
        raise PrimitiveError(
            "unknown_qid",
            f"no pending question {qid!r}; it may have already been resolved "
            f"or escalated. Spec: docs/tool-surface.md#answer_question.",
        )
    # Only the current holder (chain[-1]) may answer.
    current_holder = pq.chain[-1] if pq.chain else None
    if current_holder != caller_id:
        raise PrimitiveError(
            "not_holder",
            f"only the current question holder may answer (holder={current_holder}). "
            f"You may escalate_question instead. "
            f"Spec: docs/tool-surface.md#answer_question.",
            qid=qid,
            holder=current_holder,
            caller=caller_id,
        )
    expected = len(pq.questions)
    if len(answers) != expected:
        raise PrimitiveError(
            "answer_count_mismatch",
            f"question has {expected} sub-question(s) but you provided "
            f"{len(answers)} answer(s); answers list must match question count. "
            f"Spec: docs/tool-surface.md#answer_question.",
            qid=qid,
            expected=expected,
            provided=len(answers),
        )
    result = orch.resolve_question(qid, answers, answerer=caller_id, reason=reason)
    if not result.get("ok"):
        err_reason = result.get("reason", "unknown")
        raise PrimitiveError(
            err_reason,
            f"resolve_question rejected: {err_reason}. "
            f"Spec: docs/tool-surface.md#answer_question.",
            qid=qid,
        )
    return {"ok": True, "qid": qid}


async def escalate_question(
    orch: Orchestrator,
    *,
    caller_id: str,
    qid: str,
    reason: str,
) -> dict:
    """Push a question one hop further up the leader chain. Fire-and-forget.

    See ``docs/tool-surface.md#escalate_question``. Only the current holder
    may escalate. The next holder is derived from the caller's team leader;
    if the caller is the root or its leader is the user sentinel, the
    question surfaces to the user gateway. This call returns immediately —
    the escalator does NOT await the future (bubble model).
    """
    if not isinstance(qid, str) or not qid:
        raise PrimitiveError("invalid_input", "qid must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        raise PrimitiveError(
            "invalid_input", "reason must be a non-empty string explaining why you can't answer"
        )

    registry = getattr(orch, "_questions", None)
    if registry is None:
        raise PrimitiveError(
            "gateway_unavailable",
            "orchestrator has no question registry. "
            "Spec: docs/tool-surface.md#escalate_question.",
        )
    pq = registry.get(qid)
    if pq is None:
        raise PrimitiveError(
            "unknown_qid",
            f"no pending question {qid!r}; it may have already been resolved. "
            f"Spec: docs/tool-surface.md#escalate_question.",
        )
    # Only the current holder (chain[-1]) may escalate.
    current_holder = pq.chain[-1] if pq.chain else None
    if current_holder != caller_id:
        raise PrimitiveError(
            "not_holder",
            f"only the current question holder may escalate (holder={current_holder}). "
            f"Spec: docs/tool-surface.md#escalate_question.",
            qid=qid,
            holder=current_holder,
            caller=caller_id,
        )

    # Derive the next target from the caller's team leader.
    new_target_id: Optional[str] = None
    if hasattr(orch, "parent_for_chain"):
        new_target_id = orch.parent_for_chain(caller_id)  # type: ignore[attr-defined]
    else:
        # Fallback for test stubs without parent_for_chain.
        rec = getattr(orch, "_agents", {}).get(caller_id)
        if rec is not None and rec.team_id is not None:
            team = getattr(orch, "_teams", {}).get(rec.team_id)
            if team is not None:
                leader = getattr(team, "leader_id", None)
                try:
                    from beidou.orchestrator import USER_SENTINEL  # local import to avoid cycle
                    if leader is not None and leader != USER_SENTINEL:
                        new_target_id = leader
                except ImportError:
                    if leader is not None and leader != "__user__":
                        new_target_id = leader

    out = await orch.forward_question(  # type: ignore[attr-defined]
        qid=qid, by_id=caller_id, new_target_id=new_target_id, reason=reason,
    )
    if not out.get("ok"):
        raise PrimitiveError(
            out.get("reason", "stale"),
            f"forward_question rejected: {out.get('reason', 'stale')}. "
            f"Spec: docs/tool-surface.md#escalate_question.",
            qid=qid,
        )
    orch.emit_event(
        "question_escalated",
        {
            "agent_id": caller_id,
            "qid": qid,
            "by": caller_id,
            "new_holder": out.get("new_holder"),
            "reason": reason[:200],
        },
    )
    return {"ok": True, "qid": qid, "new_holder": out.get("new_holder")}


async def signal_review(
    orch: Orchestrator,
    *,
    caller_id: str,
    detail: str,
    _legacy_done: bool = False,
) -> dict:
    """Signal that work is ready for leader review.

    The agent remains alive; review is a state, not an exit.
    Reentrant — call each time work is ready for review.
    See docs/tool-surface.md#signal_review.
    """
    detail_text = (detail or "").strip()
    detail_lower = detail_text.lower()
    if "[review required]" not in detail_lower and "[iteration ready]" not in detail_lower:
        raise PrimitiveError(
            "envelope_missing",
            "signal_review requires [REVIEW REQUIRED] or [ITERATION READY] envelope "
            "to appear in detail. Resubmit with the full envelope (role, agent, "
            "Deliverables, Open questions / risks, Leader action required) inside "
            "detail. Spec: docs/tool-surface.md#signal_review.",
            reason="envelope_missing" if detail_text else "detail_empty",
        )

    rec = getattr(orch, "_agents", {}).get(caller_id)
    if rec is not None:
        rec.review_pending = True
        rec.review_pending_ts = time.time()

    # Record status so leader-side list_pending_reviews picks it up.
    orch.record_status(caller_id, "review_pending", detail)
    orch.emit_event(
        "status",
        {
            "agent_id": caller_id,
            "state": "review_pending",
            "detail": detail,
            "legacy_done": _legacy_done,
            "ts": time.time(),
        },
    )
    return {"recorded": True, "review_pending": True}


async def request_termination(
    orch: Orchestrator,
    *,
    caller_id: str,
    detail: Optional[str] = None,
) -> dict:
    """Request final lifecycle termination after review is approved.

    The leader must have already approved the work; this primitive signals
    that the agent is ready to be torn down. See docs/tool-surface.md#request_termination.
    """
    # Plan-incomplete guard (same as legacy report_status done-path).
    plan_id = orch._active_plan_by_agent.get(caller_id)
    if plan_id is not None:
        plan = orch._plans.get(plan_id)
        if plan is not None:
            incomplete = [
                t.id for t in plan.tasks.values()
                if t.status not in ("done", "failed")
            ]
            if incomplete:
                raise PrimitiveError(
                    "plan_incomplete",
                    f"cannot request termination: {len(incomplete)} plan task(s) "
                    f"unfinished: {', '.join(incomplete)}. Mark each task "
                    f"done via update_plan_task before requesting termination. "
                    f"Spec: docs/tool-surface.md#declare_plan.",
                    plan_id=plan_id,
                    incomplete_task_ids=incomplete,
                )

    # Set both review_pending (so leader sees pending review) and
    # termination_requested (so leader knows this is a lifecycle-end signal).
    rec = getattr(orch, "_agents", {}).get(caller_id)
    if rec is not None:
        rec.review_pending = True
        rec.review_pending_ts = time.time()
        rec.termination_requested = True

    orch.emit_event(
        "status",
        {
            "agent_id": caller_id,
            "state": "termination_requested",
            "detail": detail,
            "ts": time.time(),
        },
    )
    return {"recorded": True, "termination_requested": True}




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

    DEPRECATED: Use declare_plan + spawn_agent instead.

    The self-lead invariant (orchestration.md) is guaranteed by construction:
    this Python signature has no ``leader_id`` parameter, so the model cannot
    pass one. The orchestrator passes ``leader_id=caller_id`` unconditionally.

    ``consensus=True`` opts into legitimate "N parallel attempts" scenarios
    (voting, ensemble) where all members intentionally share the same task.
    Without it, N>1 roles that all share the same (skill, description) tuple
    are rejected as a footgun guard (see ``duplicate_member_descriptions``).
    """
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

    if caller_depth + 1 > 6:
        orch.emit_event(
            "depth_warning",
            {
                "caller_id": caller_id,
                "caller_depth": caller_depth,
                "new_depth": caller_depth + 1,
                "max_depth": MAX_DEPTH,
                "task_id": task_id,
                "ts": time.time(),
            },
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
                "rules may not carry leader_id overrides; the self-lead "
                "invariant is enforced. Spec: docs/orchestration.md.",
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
                "if parallel attempts are genuinely intentional (e.g. voting). "
                "Spec: docs/tool-surface.md#create_team.",
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


_TASK_SPEC_REQUIRED = ("id", "role", "skill", "task")


def _validate_task_specs(tasks: Any) -> None:
    """Raise PrimitiveError if tasks is not a list of dicts with required keys."""
    if not isinstance(tasks, list):
        raise PrimitiveError(
            "invalid_task_spec",
            "tasks must be a list of task spec dicts. "
            "Spec: docs/tool-surface.md#declare_plan.",
        )
    for i, spec in enumerate(tasks):
        if not isinstance(spec, dict):
            raise PrimitiveError(
                "invalid_task_spec",
                f"tasks[{i}] must be a dict, got {type(spec).__name__}. "
                f"Spec: docs/tool-surface.md#declare_plan.",
            )
        for key in _TASK_SPEC_REQUIRED:
            if key not in spec:
                raise PrimitiveError(
                    "invalid_task_spec",
                    f"tasks[{i}] missing required key {key!r}; required keys: "
                    f"{', '.join(_TASK_SPEC_REQUIRED)}. "
                    f"Spec: docs/tool-surface.md#declare_plan.",
                    index=i,
                    missing_key=key,
                )


async def declare_plan(
    orch: Orchestrator,
    *,
    caller_id: str,
    tasks: list[dict],
) -> dict:
    """Pure-data: validate DAG, persist, return rich introspection.

    No team, workspace, or agent is created here. The only registry side-effect
    is updating the caller's active_plan_id. caller_id is bound by the MCP
    closure — never trusted from model input.
    """
    _validate_task_specs(tasks)
    # PrimitiveError subclasses from register_plan bubble up unchanged.
    return await orch.register_plan(caller_id=caller_id, specs=tasks)


async def remove_plan(
    orch: Orchestrator,
    *,
    caller_id: str,
) -> dict:
    """Hard-fork the active plan: rename file to .removed.json and clear the slot.

    caller_id is bound by the MCP closure — never trusted from model input.
    """
    return await orch.remove_active_plan(caller_id=caller_id)


async def spawn_agent(
    orch: Orchestrator,
    *,
    caller_id: str,
    task_id: str,
) -> dict:
    """Gated spawn. First call lazily creates the team.

    caller_id is bound by the MCP closure — never trusted from model input.
    """
    if not isinstance(task_id, str) or not task_id:
        raise PrimitiveError(
            "invalid_task_id",
            "task_id must be a non-empty string; use list_ready to discover "
            "ready tasks. Spec: docs/tool-surface.md#spawn_agent.",
        )

    # One concurrent spawn per caller — mirrors create_team's lock pattern.
    # The .locked() check is best-effort: in the rare case two spawn_agent
    # calls interleave before spawn_for_task acquires the lock, they both
    # pass the check and then serialize inside spawn_for_task's `async with lock`.
    # This is acceptable; the primary guard is the orchestrator's own lock.
    lock = await orch.spawn_lock(caller_id)
    if lock.locked():
        raise PrimitiveError(
            "concurrent_spawn",
            f"{caller_id} already has an in-flight spawn_agent (limits.md #5)",
            caller_id=caller_id,
        )
    # spawn_for_task acquires the lock internally; no acquire here.
    return await orch.spawn_for_task(caller_id=caller_id, task_id=task_id)


async def list_ready(
    orch: Orchestrator,
    *,
    caller_id: str,
) -> dict:
    """Read-only query: return ready task ids in the caller's active plan.

    caller_id is bound by the MCP closure — never trusted from model input.
    """
    return orch.list_ready_tasks(caller_id=caller_id)


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
    report_status(state="done") first (review_pending=True) or to already
    have consumed a prior terminate (terminate_consumed=True).  Pass
    force=True to override; an audited terminate.forced event is emitted.
    """
    if not orch.agent_exists(agent_id):
        raise PrimitiveError(
            "unknown_agent",
            f"no such agent: {agent_id}; use list_peers to discover children. "
            f"Spec: docs/tool-surface.md#terminate_child.",
            agent_id=agent_id,
        )

    target_team = orch.agent_team(agent_id)
    if orch.leader_of(target_team) != caller_id:
        raise PrimitiveError(
            "not_leader",
            f"{caller_id} does not lead team {target_team}; only direct "
            f"team leaders may terminate. "
            f"Spec: docs/orchestration.md, docs/tool-surface.md#terminate_child.",
            caller_id=caller_id,
            target_team=target_team,
        )

    # Completion gate: child must have reported done (or already been
    # terminated) unless the caller explicitly passes force=True.
    target_rec = orch._agents.get(agent_id)
    # Defensive (target_rec should exist if agent_exists passed) — keep simple.
    target_pending = bool(target_rec and target_rec.review_pending)
    target_terminated = bool(target_rec and target_rec.terminate_consumed)
    if not force and not target_pending and not target_terminated:
        raise PrimitiveError(
            "child_not_pending_review",
            f"{agent_id} has not called report_status(state='done'). "
            f"Wait for completion review, send a rework message, or pass "
            f"force=true. Spec: docs/tool-surface.md#terminate_child.",
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

    # Plan-task lifecycle hook: drive task status alongside agent termination.
    # getattr fallback handles FakeOrchestrator in tests (no plan_task_id attr).
    plan_task_id = getattr(target_rec, "plan_task_id", None) if target_rec is not None else None
    if plan_task_id is not None:
        if force:
            await orch.mark_task_failed(task_id=plan_task_id)
        else:
            await orch.mark_task_done(task_id=plan_task_id)

    return {"sentinel_posted": True}


def list_pending_reviews(
    orch: Orchestrator,
    *,
    caller_id: str,
) -> list[dict]:
    """Return direct children of ``caller_id`` that are awaiting review.

    "Direct children" are members of any team that ``caller_id`` leads
    (via ``orch.teams_led_by``). Only those with ``review_pending=True``
    are returned. The caller itself is excluded even if it somehow appears
    as a member of one of its own teams.

    Each entry is a dict with:
      - ``agent_id``              -- the child's agent id
      - ``role``                  -- skill name (from ``agent_skill_name``)
      - ``review_pending_ts`` -- unix float when the child called done, or null
      - ``age_s``                 -- seconds since review_pending_ts, or null
      - ``summary``               -- ``last_status_detail`` text (may be empty string)

    Results are sorted ascending by ``review_pending_ts`` (oldest first).
    A ``None`` ts sorts after all timestamped entries.

    Returns ``[]`` if no direct children have ``review_pending=True``.
    See ``docs/tool-surface.md#list_pending_reviews``.
    """
    now = time.time()
    pending: list[dict] = []

    for team_id in orch.teams_led_by(caller_id):
        for member_id in orch.team_members(team_id):
            if member_id == caller_id:
                continue
            if not orch.agent_review_pending(member_id):
                continue
            ts = orch.agent_review_pending_ts(member_id)
            age_s = (now - ts) if ts is not None else None
            pending.append(
                {
                    "agent_id": member_id,
                    "role": orch.agent_skill_name(member_id),
                    "review_pending_ts": ts,
                    "age_s": age_s,
                    "summary": orch.agent_last_status_detail(member_id),
                    "name": orch.agent_name(member_id),
                }
            )

    # Sort by ts ascending; None ts sorts last.
    pending.sort(key=lambda d: (d["review_pending_ts"] is None, d["review_pending_ts"] or 0))
    return pending


__all__ = [
    # constants
    "INBOX_CAP",
    "MAX_DEPTH",
    "CONTRACT_STRIKES",
    "CRASH_STRIKES",
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
    "answer_question",
    "escalate_question",
    "signal_review",
    "request_termination",
    "create_team",
    "terminate_child",
    "list_pending_reviews",
]
