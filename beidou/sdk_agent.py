"""Thin wrapper around ``claude_agent_sdk.query(...)``.

This module spawns a single SDK agent, drains its async message iterator,
and translates the stream into Beidou observability events. It is called by
the orchestrator (built in the next step). The drain loop is Beidou's sole
producer of the events catalogued in ``docs/observability.md``:

* ``agent_started``   -- pre-drain lifecycle event.
* ``turn.usage``      -- deduplicated per-turn token accounting.
* ``tool_called``     -- emitted when a ``ToolUseBlock`` arrives in an ``AssistantMessage``.
* ``tool_result``     -- emitted when a ``ToolResultBlock`` arrives in a ``UserMessage``.
* ``assistant_text``  -- emitted once per ``AssistantMessage`` that contains ``TextBlock``s.
* ``run.cost``        -- terminal authoritative cost/duration/num_turns rollup.
* ``agent_completed`` -- post-drain lifecycle event.
* ``agent_error``     -- emitted on unexpected exceptions, before re-raising.

The drain loop is the **sole owner** of ``tool_called`` and ``tool_result``
emission. The MCP wrapper (``beidou/primitives/mcp.py``) does NOT emit
``tool_called`` -- that was the prior design, now replaced.

``contract_violation`` is emitted by the *orchestrator*, not this module. We
only set the flag on :class:`RunResult`; the orchestrator reads it and
decides whether to resume-not-terminate (see ``docs/agent-runtime.md``
section 4).

**Outer loop.** The SDK agent runs as a persistent session driven by a per-agent
``asyncio.Queue``. After the initial task turn, the ``input_stream`` generator
parks on ``await queue.get()``. Whoever pushes (peer agent via ``send_message``,
orchestrator via ``terminate_root``/``terminate_child``, future web endpoint)
wakes the session. A ``kind=="terminate"`` sentinel ends the generator, which
closes the SDK session cleanly. The agent never sees a "wait" or "receive" tool.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import ClaudeAgentOptions, query

from .primitives.core import Message, Orchestrator
from .primitives.mcp import build_mcp_server_for
from claude_agent_sdk.types import HookMatcher

# Hook execution cap for hooks that block on a human gateway round-trip.
# Default in claude-code is 60s, which silently truncates real reviews.
HOOK_REVIEW_TIMEOUT_S: float = 1800.0

from .skills.loader import (
    LoadedSkill,
    SkillError,
    build_system_prompt,
    load_skill,
    load_skill_file,
    sdk_builtins_allowlist,
)


# ---------------------------------------------------------------------------
# Public API dataclasses.
# ---------------------------------------------------------------------------


@dataclass
class SpawnSpec:
    """Everything Beidou needs to spawn one SDK agent.

    Fields mirror ``docs/architecture.md`` / ``docs/skills.md``. ``caller_id``
    must match the agent_id the orchestrator registered for this spawn --
    the MCP server binds it by closure and every primitive call uses it.
    """

    caller_id: str
    skill_name: str
    skill_root: Path
    task: str
    model: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    template_vars: Optional[dict[str, str]] = None
    cwd: Optional[str] = None


@dataclass
class RunResult:
    """Return value of :func:`run_agent`.

    ``terminated`` is True if and only if the orchestrator observed this
    agent consume a terminate sentinel before the SDK iterator ended.
    ``contract_violation`` is simply ``not terminated`` when the loop ended
    normally (with or without a ``ResultMessage``). On exception or
    cancellation the loop exits abnormally; we leave ``contract_violation``
    False to distinguish that case -- the orchestrator uses the exception
    path, not the contract-violation path, to react.
    """

    final_text: str
    total_cost_usd: float
    total_usage: dict
    num_turns: int
    duration_ms: int
    stop_reason: str
    session_id: Optional[str]
    terminated: bool
    contract_violation: bool = False
    model_usage: dict = field(default_factory=dict)
    duration_api_ms: int = 0


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _resolve_skill(skill_root: Path, skill_name: str) -> LoadedSkill:
    """Locate the skill by name under ``skill_root``; fall back to direct path."""
    try:
        return load_skill(skill_root, skill_name)
    except SkillError:
        # Fallback: allow callers who already know the exact file path to
        # pass a directory containing a single SKILL.md.
        candidate = Path(skill_root) / "SKILL.md"
        if candidate.is_file():
            return load_skill_file(candidate)
        raise


# The canonical list of Beidou MCP primitive tool names (namespaced).
# These match the tools registered in beidou/primitives/mcp.py exactly.
_BEIDOU_PRIMITIVE_TOOLS: list[str] = [
    "mcp__beidou__send_message",
    "mcp__beidou__list_peers",
    "mcp__beidou__ask_user",
    "mcp__beidou__report_status",
    "mcp__beidou__create_team",
    "mcp__beidou__terminate_child",
]


def build_hooks(orch: "Orchestrator", caller_id: str, leader_id: str) -> dict:
    """Build hook dicts for the SDK agent.

    Hooks registered:

    **PreToolUse — AskUserQuestion**
        Intercepts raw ``AskUserQuestion`` tool calls emitted by models that do not
        use the ``mcp__beidou__ask_user`` namespaced primitive (e.g. MiniMax-M2.7 via
        the Anthropic-compatible proxy).  The hook routes the question(s) to the human
        gateway (same plumbing as ``ask_user``) and returns the answer as the tool's
        effective response by denying the tool call with a ``permissionDecisionReason``.
        Emits a synthetic ``tool_called`` + ``tool_result`` pair so the UI/JSONL log
        reflects what happened.

    **PostToolUse — mcp__beidou__report_status**
        Fires when the agent calls ``mcp__beidou__report_status(state="done")``.
        Reads the agent's last assistant text (bound to that turn) from the orchestrator
        and delivers it to the leader's inbox as a ``completion_report`` message.

        If ``leader_id`` is the user-sentinel (root agent), the hook still registers but
        emits ``completion.empty`` with reason ``root_no_leader`` instead of delivering.

        Guards:
        - Wrong tool_name: return early (the HookMatcher should filter this, but be safe).
        - state != "done": no-op.
        - is_error=True: the report_status call failed; skip delivery.
        - Empty summary: emit completion.empty with reason "no summary in report_status turn".
        - leader inbox_full: handled inside deliver_message, which emits completion.empty.
    """
    from .orchestrator import USER_SENTINEL

    async def on_ask_user_question(input_data: Any, tool_use_id: Optional[str], context: Any) -> dict:
        """Intercept raw AskUserQuestion tool calls and route them to the human gateway.

        The SDK emits ``permissionDecision="deny"`` with the user's answer as
        ``permissionDecisionReason`` so the model receives the answer and proceeds.
        """
        # Defensive guard — HookMatcher should filter, but be safe.
        if input_data.get("tool_name") != "AskUserQuestion":
            return {}

        raw_input = input_data.get("tool_input") or {}
        questions = raw_input.get("questions") or []
        if not isinstance(questions, list) or not questions:
            # Nothing to ask — deny with a clear reason so the model knows to use ask_user.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "AskUserQuestion received no questions. "
                        "Use mcp__beidou__ask_user(question, context_hint) instead."
                    ),
                }
            }

        # Use a fresh synthetic id so the hook-emitted pair is distinguishable
        # from any drain-loop pair on the original tool_use_id.
        synthetic_tool_use_id = f"hook_askuserquestion_{uuid.uuid4().hex[:8]}"
        started = time.time()
        orch.emit_event(
            "tool_called",
            {
                "ts": started,
                "caller_id": caller_id,
                "tool_use_id": synthetic_tool_use_id,
                "name": "AskUserQuestion",
                "input": raw_input,
            },
        )

        # TODO(post-m4g): gateway_ask_user (string-only) is now unused; remove from orchestrator.py in a cleanup pass.
        is_error = False
        answer_text: str
        try:
            result = await orch.gateway_ask_user_structured(caller_id, questions, None)
            answer_text = result.get("answer_text", "")
        except Exception as exc:  # noqa: BLE001 — gateway can be diverse
            is_error = True
            answer_text = f"ask_user failed: {type(exc).__name__}: {exc}"

        duration_ms = int(round((time.time() - started) * 1000))
        orch.emit_event(
            "tool_result",
            {
                "ts": time.time(),
                "caller_id": caller_id,
                "tool_use_id": synthetic_tool_use_id,
                "duration_ms": duration_ms,
                "is_error": is_error,
            },
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": answer_text,
            }
        }

    async def on_report_status(input_data: Any, tool_use_id: Optional[str], context: Any) -> dict:
        # Guard: wrong tool (HookMatcher should filter, but be defensive).
        if input_data.get("tool_name") != "mcp__beidou__report_status":
            return {}
        # Guard: only act on state=="done".
        if input_data.get("tool_input", {}).get("state") != "done":
            return {}
        # Guard: PostToolUse also fires for failed calls (is_error=True tool result).
        tool_response = input_data.get("tool_response") or {}
        if isinstance(tool_response, dict) and tool_response.get("is_error"):
            return {}

        # Mark completion as pending on the AgentRecord BEFORE branching on
        # leader/root, so root reuses the same approved/rework state transitions
        # as children. last_progress_ts also bumps: this IS the last meaningful
        # agent action.
        rec = orch._agents.get(caller_id)  # type: ignore[attr-defined]
        if rec is not None:
            rec.completion_pending = True
            rec.completion_pending_ts = time.time()
            rec.last_progress_ts = time.time()

        # Retrieve the assistant text from the same turn as the report_status call.
        # Exact binding: tool_use_id -> text recorded by the drain loop in the same message.
        # Fallback: most recent assistant text for this agent (from any prior turn).
        summary = orch.assistant_text_for_turn(caller_id, tool_use_id or "")
        if not summary or not summary.strip():
            # Fall back to the detail argument from the report_status call.
            tool_input = input_data.get("tool_input") or {}
            summary = tool_input.get("detail")
        if not summary or not summary.strip():
            orch.emit_event(
                "completion.empty",
                {
                    "agent_id": caller_id,
                    "leader_id": leader_id,
                    "reason": "no summary in report_status turn",
                },
            )
            return {}

        # Defensive envelope guard: ensure the body contains [REVIEW REQUIRED].
        # If the child embedded the prompt-side envelope, pass through unchanged.
        # If not, synthesize the envelope so the reviewer gets the unmissable signal.
        if "[review required]" not in summary.lower():
            # Read skill name from the AgentRecord for the synthetic header.
            skill_name_for_envelope = (
                rec.skill_name if rec is not None else caller_id
            )
            synthesized_body = (
                f"[REVIEW REQUIRED]\n"
                f"role={skill_name_for_envelope}     agent={caller_id}\n"
                f"Deliverables: (none provided — child failed to embed envelope)\n"
                f"Open questions / risks: (none provided)\n"
                f"Leader action required: approve (terminate_child) OR rework (send_message)\n"
                f"\n\n"
                f"Original child body:\n"
                f"{summary}"
            )
            orch.emit_event(
                "completion.envelope_synthesized",
                {
                    "agent_id": caller_id,
                    "leader_id": leader_id,
                    "body_chars": synthesized_body[:200],
                },
            )
            summary = synthesized_body

        # Root agent: route the review through the human gateway instead of an
        # agent leader. Approve → terminate_root; Rework → deliver a rework
        # message back to the root inbox so the next turn can continue.
        if leader_id == USER_SENTINEL:
            try:
                answer = await orch.gateway_ask_user_structured(
                    caller_id,
                    [
                        {
                            "question": summary,
                            "header": "Review",
                            "multiSelect": False,
                            "options": [
                                {
                                    "label": "Approve",
                                    "description": "Accept the deliverable and end the run.",
                                    "value": "approve",
                                },
                                {
                                    "label": "Rework",
                                    "description": "Send feedback back to the agent and continue.",
                                    "value": "rework",
                                    "requires_text": True,
                                },
                            ],
                        }
                    ],
                    "Root completion review",
                )
            except Exception as e:
                orch.emit_event(
                    "completion.empty",
                    {
                        "agent_id": caller_id,
                        "leader_id": leader_id,
                        "reason": f"gateway_failure: {type(e).__name__}",
                    },
                )
                return {}

            sub = (answer.get("answers") or [{}])[0]
            selected = sub.get("selected_values") or sub.get("selected_labels") or []
            decision = (selected[0] if selected else "").lower()

            # TerminalGateway / TUIGateway send free-text answers
            # (selected_labels=[]); fall back to recognising approval keywords
            # in the typed text so a typed "approve" / "yes" still terminates.
            if not decision:
                typed = (sub.get("text") or "").strip().lower()
                if typed in {"approve", "approved", "yes", "y", "ok", "lgtm"}:
                    decision = "approve"

            if decision in ("approve", "approved"):
                orch.emit_event(
                    "completion.reported",
                    {
                        "agent_id": caller_id,
                        "leader_id": leader_id,
                        "via": "user_gateway",
                        "decision": "approve",
                    },
                )
                await orch.terminate_root()
            else:
                # Treat anything that isn't "approve" as rework so the user can
                # send free-text guidance even if they pick the "Other" path.
                rework_text = sub.get("text") or "(no rework details provided)"
                orch.deliver_message(
                    from_id="user",
                    to_id=caller_id,
                    body=f"rework: {rework_text}",
                )
                orch.emit_event(
                    "completion.reported",
                    {
                        "agent_id": caller_id,
                        "leader_id": leader_id,
                        "via": "user_gateway",
                        "decision": "rework",
                    },
                )
            return {}

        orch.deliver_message(
            from_id=caller_id,
            to_id=leader_id,
            body=summary,
            kind="completion_report",
        )
        orch.emit_event(
            "completion.reported",
            {
                "agent_id": caller_id,
                "leader_id": leader_id,
                "via": "hook",
            },
        )
        return {}

    # Tools a leader may call even when it has children awaiting review.
    # Read/grep/bash let the leader inspect artifacts before deciding.
    # The beidou primitives listed here are the two valid resolution actions
    # (terminate_child, send_message) plus informational/status tools.
    #
    # bd issue xq1: also include SDK-builtin classic names that skill
    # allowed-tools entries may resolve to via _SDK_BUILTIN_MAP or directly.
    # - SendMessage: SDK-builtin classic for inter-agent messaging
    # - AskUserQuestion: SDK-builtin; on_ask_user_question handles review-gate
    #   semantics when pending children exist — on_review_gate should pass through
    # - Write, Edit: SDK-builtin file tools leaders may need during review
    # - WebSearch, WebFetch: SDK-builtin web tools mapped from web_search/web_fetch
    # MCP-namespaced versions are kept alongside for skills using lowercase MCP names.
    ALLOWED_DURING_PENDING_REVIEW = {
        "Read",
        "Glob",
        "Grep",
        "Bash",
        # SDK-builtin classic names (bd issue xq1)
        "Write",
        "Edit",
        "SendMessage",
        "AskUserQuestion",
        "WebSearch",
        "WebFetch",
        # MCP-namespaced beidou primitives
        "mcp__beidou__terminate_child",
        "mcp__beidou__send_message",
        "mcp__beidou__list_pending_reviews",
        "mcp__beidou__report_status",
        "mcp__beidou__ask_user",
    }

    async def on_review_gate(input_data: Any, tool_use_id: Optional[str], context: Any) -> dict:
        """Block a leader from advancing while it has direct children awaiting review.

        Fires on EVERY tool call (matcher=None) for the leader agent. If no
        children have completion_pending=True the hook is a transparent pass-through.
        When pending children exist, only tools in ALLOWED_DURING_PENDING_REVIEW
        are permitted; all others are denied with a directive explaining next steps.

        Note: AskUserQuestion is now in the allowlist (bd issue xq1), so this hook
        passes through for it. The on_ask_user_question hook handles review-gate
        semantics when pending children exist (bd issue be3 scope).
        """
        # Defensive: if the agent isn't registered, allow.
        rec = orch._agents.get(caller_id)  # type: ignore[attr-defined]
        if rec is None:
            return {}

        # Collect direct children with completion_pending=True.
        # bd issue xq1: skip children whose SDK session has already ended
        # (terminate_consumed=True) — a stale completion_pending flag on a
        # terminated child must not gate the leader.
        pending_ids: list[str] = []
        for tid in orch.teams_led_by(caller_id):  # type: ignore[attr-defined]
            team = orch._teams.get(tid)  # type: ignore[attr-defined]
            if team is None:
                continue
            for member_id in team.member_ids:
                child_rec = orch._agents.get(member_id)  # type: ignore[attr-defined]
                if child_rec is None:
                    continue
                if child_rec.terminate_consumed:  # bd issue xq1: skip terminated children
                    continue
                if child_rec.completion_pending:
                    pending_ids.append(member_id)

        if not pending_ids:
            return {}

        tool_name = input_data.get("tool_name", "")

        if tool_name in ALLOWED_DURING_PENDING_REVIEW:
            return {}

        # Deny: emit the gate event and return the deny shape.
        orch.emit_event(  # type: ignore[attr-defined]
            "review_gate.denied",
            {
                "agent_id": caller_id,
                "tool_name": tool_name,
                "pending_children": pending_ids,
            },
        )

        if len(pending_ids) == 1:
            pending_desc = f"child {pending_ids[0]} is awaiting your review"
        else:
            pending_desc = (
                "children "
                + ", ".join(pending_ids)
                + " are awaiting your review"
            )

        pending_list = "\n".join(
            f"  • mcp__beidou__terminate_child(agent_id=\"{pid}\")  to approve, OR\n"
            f"    mcp__beidou__send_message(to=\"{pid}\", content=\"rework: <what>\")  to ask for changes."
            for pid in pending_ids
        )

        reason = (
            f"Cannot call {tool_name!r} — {pending_desc}.\n"
            f"Resolve the pending review first by calling either:\n"
            f"{pending_list}\n"
            f"If you have multiple pending children, resolve all of them first."
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return {
        "PreToolUse": [
            HookMatcher(
                matcher="AskUserQuestion",
                hooks=[on_ask_user_question],
                timeout=HOOK_REVIEW_TIMEOUT_S,
            ),
            # matcher=None → fires for every tool call.
            HookMatcher(
                matcher=None,
                hooks=[on_review_gate],
            ),
        ],
        "PostToolUse": [
            HookMatcher(
                matcher="mcp__beidou__report_status",
                hooks=[on_report_status],
                timeout=HOOK_REVIEW_TIMEOUT_S,
            ),
        ],
    }


def _build_options(
    *,
    system_prompt: str,
    mcp_server,
    allowed_tools: list[str],
    model: Optional[str],
    cwd: Optional[str],
    hooks: Optional[dict] = None,
) -> ClaudeAgentOptions:
    """Assemble ``ClaudeAgentOptions`` for one SDK agent spawn.

    ``setting_sources=["user", "project"]`` enables SDK skill discovery:
    - "user": ~/.claude/skills/ (user skills, never copied)
    - "project": <cwd>/.claude/skills/ (team workspace, provisioned by provision_skills)

    ``skills="all"`` enables the Skill tool so agents can invoke any discovered skill.
    Note: skills="all" does NOT auto-add SDK builtins like Bash/Read/Write.
    Those come from the skill's allowed-tools via sdk_builtins_allowlist().

    ``permission_mode="bypassPermissions"`` matches docs/skills.md: every tool is
    either an MCP primitive we own or an explicitly-listed SDK built-in.
    """
    kwargs: dict = dict(
        system_prompt=system_prompt,
        mcp_servers={"beidou": mcp_server},
        allowed_tools=list(allowed_tools),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        skills="all",
    )
    if model:
        kwargs["model"] = model
    if cwd:
        kwargs["cwd"] = cwd
    if hooks:
        kwargs["hooks"] = hooks
    return ClaudeAgentOptions(**kwargs)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


async def run_agent(orch: Orchestrator, spec: SpawnSpec) -> RunResult:
    """Spawn an SDK agent, drain its messages, emit observability events.

    The caller (the orchestrator) is responsible for the
    resume-not-terminate policy on contract violations. This function only
    reports whether the agent terminated cleanly via the
    ``RunResult.terminated`` / ``RunResult.contract_violation`` flags.
    """
    skill = _resolve_skill(spec.skill_root, spec.skill_name)
    template_vars = spec.template_vars or {}

    # Build the four-section system prompt (skill body first for cache reuse).
    rendered_prompt = build_system_prompt(skill, template_vars)

    mcp = build_mcp_server_for(orch, caller_id=spec.caller_id)

    # Resolve allowed_tools. If an explicit override was supplied in the spec,
    # use it verbatim (test / advanced caller path). Otherwise compose from:
    #   1. The 8 Beidou MCP primitives, filtered by the skill's raw allowed-tools
    #      to preserve the per-skill primitive restriction contract.
    #   2. The SDK built-ins (Bash, Read, Write, etc.) derived from the skill's
    #      allowed-tools via sdk_builtins_allowlist().
    # Note: skills="all" auto-adds the Skill tool — we do NOT add it here.
    if spec.allowed_tools is not None:
        allowed_tools = list(spec.allowed_tools)
    else:
        # Primitive restriction: only expose the Beidou primitives the skill declares.
        raw_primitives = {
            t for t in skill.allowed_tools_raw
            if f"mcp__beidou__{t}" in _BEIDOU_PRIMITIVE_TOOLS
        }
        filtered_primitives = [
            t for t in _BEIDOU_PRIMITIVE_TOOLS
            if t[len("mcp__beidou__"):] in raw_primitives
        ]
        sdk_builtins = sdk_builtins_allowlist(skill.allowed_tools_raw)
        allowed_tools = filtered_primitives + sdk_builtins

    if not allowed_tools:
        # Not fatal -- an agent with no tools will hit the contract on its
        # first turn -- but the condition almost always signals a mis-built
        # spec. Surface it through observability.
        orch.emit_event(
            "config_warning",
            {
                "caller_id": spec.caller_id,
                "warning": "empty_allowed_tools",
                "message": "empty_allowed_tools",
                "skill": skill.name,
                "ts": time.time(),
            },
        )

    # Build the PostToolUse hook for completion reporting.
    # leader_id comes from template_vars (set by orchestrator at spawn time).
    leader_id = template_vars.get("leader_id", "")
    hooks = build_hooks(orch, spec.caller_id, leader_id)

    options = _build_options(
        system_prompt=rendered_prompt,
        mcp_server=mcp,
        allowed_tools=allowed_tools,
        model=spec.model,
        cwd=spec.cwd,
        hooks=hooks,
    )

    orch.emit_event(
        "agent_started",
        {
            "caller_id": spec.caller_id,
            "skill": skill.name,
            "model_requested": spec.model,
            "model": spec.model,
            "role": template_vars.get("role", ""),
            "name": orch.agent_name(spec.caller_id),
            "ts": time.time(),
        },
    )

    # Per-drain accounting state.
    seen_message_ids: set[str] = set()
    final_text_parts: list[str] = []
    last_message_id: Optional[str] = None
    result_data: Optional[dict] = None
    # Tool-span tracking: maps tool_use_id -> monotonic start time.
    # Populated on ToolUseBlock arrival; consumed on ToolResultBlock arrival.
    pending_tool_uses: dict[str, float] = {}
    # Per-turn assistant text accumulator for PostToolUse hook binding.
    # Cleared at the start of each new message_id. At end of each message,
    # the accumulated text is recorded on the orchestrator keyed by all
    # tool_use_ids in that message.
    current_turn_text_parts: list[str] = []
    current_turn_tool_ids: list[str] = []
    # Per-turn tool name/input tracking for the harness checkpoint.
    _turn_tool_info: dict[str, tuple[str, dict]] = {}
    # Per-turn tool result error tracking for the harness checkpoint.
    _turn_result_info: dict[str, bool] = {}
    # Agents already nudged by the harness (persists across turns to prevent duplicates).
    _nudged_agents: set[str] = set()

    # Build the streaming-input generator that parks the agent between turns
    # on its per-agent queue. The generator owns terminate detection and sets
    # terminate_consumed on the AgentRecord before returning (closing the SDK
    # session). The agent never sees a "wait" or "receive" tool.
    session_id = spec.caller_id
    # queue_for() is the canonical path on the real Orchestrator. Fall back to
    # an empty Queue when the orchestrator under test doesn't expose it (e.g.
    # FakeOrchestrator in mechanical tests where query() is monkeypatched and
    # the generator is never consumed).
    _queue_for = getattr(orch, "queue_for", None)
    queue: asyncio.Queue = _queue_for(spec.caller_id) if _queue_for is not None else asyncio.Queue()

    async def input_stream():
        # Yield the initial task as the first user-role message.
        yield {
            "type": "user",
            "message": {"role": "user", "content": spec.task},
            "parent_tool_use_id": None,
            "session_id": session_id,
        }
        # Park on the per-agent queue until terminated or a new message arrives.
        while True:
            msg_in: Message = await queue.get()
            if msg_in.kind == "terminate":
                # Mark consumed BEFORE returning so was_terminated() reads
                # True in the post-loop check.
                rec = orch._agents.get(spec.caller_id)  # type: ignore[attr-defined]
                if rec is not None:
                    rec.terminate_consumed = True
                return  # Closing the generator ends the SDK session.
            # Render peer messages as user-role input to the next turn.
            content = f"[from {msg_in.from_id}] {msg_in.content}"
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
                "session_id": session_id,
            }

    # Obtain the AgentRecord once for the whole drain loop — used for
    # inflight_tools tracking (bd issue qj2). Safe: everything is single-threaded asyncio.
    _drain_rec = orch._agents.get(spec.caller_id)  # type: ignore[attr-defined]

    try:
        async for msg in query(prompt=input_stream(), options=options):
            cls = type(msg).__name__

            if cls == "AssistantMessage":
                mid = getattr(msg, "message_id", None)
                usage = getattr(msg, "usage", None)
                model_str = getattr(msg, "model", None)
                stop_reason = getattr(msg, "stop_reason", None)

                # Dedup turn.usage by message_id. Multiple AssistantMessage
                # fragments share the same message_id + usage payload
                # (confirmed in proto_02_token_granularity.py).
                if mid and mid not in seen_message_ids and usage:
                    seen_message_ids.add(mid)
                    usage_payload = dict(usage) if isinstance(usage, dict) else {}
                    orch.emit_event(
                        "turn.usage",
                        {
                            "caller_id": spec.caller_id,
                            "message_id": mid,
                            "model": model_str,
                            "stop_reason": stop_reason,
                            "input_tokens": usage_payload.get("input_tokens"),
                            "output_tokens": usage_payload.get("output_tokens"),
                            "cache_creation_input_tokens": usage_payload.get(
                                "cache_creation_input_tokens"
                            ),
                            "cache_read_input_tokens": usage_payload.get(
                                "cache_read_input_tokens"
                            ),
                            "in_tok": usage_payload.get("input_tokens"),
                            "out_tok": usage_payload.get("output_tokens"),
                            "cache_create": usage_payload.get(
                                "cache_creation_input_tokens"
                            ),
                            "cache_read": usage_payload.get(
                                "cache_read_input_tokens"
                            ),
                            "ts": time.time(),
                        },
                    )

                # Collect final-assistant text. Reset the accumulators at the
                # start of each NEW message_id so we end up with the last
                # turn's text, not the concatenation of every assistant turn.
                if mid != last_message_id:
                    # Emit assistant_text for the PREVIOUS message_id (now
                    # complete) before clearing accumulators.
                    if last_message_id and current_turn_text_parts:
                        orch.emit_event(
                            "assistant_text",
                            {
                                "ts": time.time(),
                                "caller_id": spec.caller_id,
                                "message_id": last_message_id,
                                "text": "".join(current_turn_text_parts),
                                "stop_reason": stop_reason,
                            },
                        )
                    final_text_parts.clear()
                    current_turn_text_parts.clear()
                    current_turn_tool_ids.clear()
                    _turn_tool_info.clear()
                    _turn_result_info.clear()
                    last_message_id = mid

                for block in (getattr(msg, "content", None) or []):
                    block_cls = type(block).__name__
                    if block_cls == "TextBlock":
                        text = getattr(block, "text", "")
                        if text:
                            final_text_parts.append(text)
                            current_turn_text_parts.append(text)
                    elif block_cls == "ToolUseBlock":
                        tool_use_id = getattr(block, "id", None)
                        if tool_use_id:
                            current_turn_tool_ids.append(tool_use_id)
                            pending_tool_uses[tool_use_id] = time.monotonic()
                            _turn_tool_info[tool_use_id] = (
                                getattr(block, "name", ""),
                                getattr(block, "input", {}),
                            )
                            orch.emit_event(
                                "tool_called",
                                {
                                    "ts": time.time(),
                                    "caller_id": spec.caller_id,
                                    "message_id": mid,
                                    "tool_use_id": tool_use_id,
                                    "name": getattr(block, "name", None),
                                    "input": getattr(block, "input", {}),
                                },
                            )
                            # Track inflight tools (bd issue qj2).
                            if _drain_rec is not None:
                                _drain_rec.inflight_tools += 1
                                _drain_rec.last_progress_ts = time.time()

                # Record the current turn's text IMMEDIATELY after processing
                # each AssistantMessage so the PostToolUse hook (which fires
                # between messages, when the SDK calls the MCP tool) sees
                # the correctly-bound text. Recording after every fragment of
                # the same message_id is safe: current_turn_text_parts has
                # been accumulating all fragments, so we overwrite with the
                # growing cumulative text. This is the correct behaviour for
                # multi-fragment messages (same message_id yields).
                if mid and current_turn_text_parts:
                    orch_record = getattr(orch, "record_assistant_text", None)
                    if orch_record is not None:
                        orch_record(
                            spec.caller_id,
                            "".join(current_turn_text_parts),
                            list(current_turn_tool_ids),
                        )

            elif cls == "UserMessage":
                # UserMessage carries tool-result echoes from the SDK.
                # Drain ToolResultBlocks to close open tool spans.
                for block in (getattr(msg, "content", None) or []):
                    if type(block).__name__ == "ToolResultBlock":
                        tool_use_id = getattr(block, "tool_use_id", None)
                        start = pending_tool_uses.pop(tool_use_id, None) if tool_use_id else None
                        duration_ms: Optional[int] = (
                            int(round((time.monotonic() - start) * 1000.0))
                            if start is not None
                            else None
                        )
                        orch.emit_event(
                            "tool_result",
                            {
                                "ts": time.time(),
                                "caller_id": spec.caller_id,
                                "tool_use_id": tool_use_id,
                                "duration_ms": duration_ms,
                                "is_error": getattr(block, "is_error", False) or False,
                            },
                        )
                        _turn_result_info[tool_use_id] = getattr(block, "is_error", False) or False
                        # Decrement inflight tools (bd issue qj2).
                        if _drain_rec is not None:
                            _drain_rec.inflight_tools = max(0, _drain_rec.inflight_tools - 1)
                            _drain_rec.last_progress_ts = time.time()

                # --- HARNESS CHECKPOINT ---
                # After all tool results are recorded, run the completion handoff
                # repair check. Only runs when harness is installed and tool calls
                # were made this turn.
                if pending_tool_uses or current_turn_tool_ids:
                    try:
                        from beidou.harness import repair_completion_handoff
                    except ImportError:
                        pass
                    else:
                        turn_state = {
                            "message_id": last_message_id,
                            "text": "".join(current_turn_text_parts),
                            "tools": [
                                {
                                    "name": name,
                                    "input": inp,
                                    "tool_use_id": tid,
                                }
                                for tid, (name, inp) in _turn_tool_info.items()
                            ],
                        }
                        nudge = repair_completion_handoff(
                            orch, spec.caller_id, turn_state, _nudged_agents
                        )
                        if nudge:
                            orch.deliver_message(
                                from_id="__harness__",
                                to_id=spec.caller_id,
                                body=nudge,
                                kind="nudge",
                            )
                            orch.emit_event(
                                "completion.nudged",
                                {
                                    "caller_id": spec.caller_id,
                                    "ts": time.time(),
                                },
                            )

            elif cls == "ResultMessage":
                result_data = {
                    "total_cost_usd": getattr(msg, "total_cost_usd", 0.0) or 0.0,
                    "usage": dict(getattr(msg, "usage", {}) or {}),
                    "model_usage": dict(getattr(msg, "model_usage", {}) or {}),
                    "duration_ms": getattr(msg, "duration_ms", 0) or 0,
                    "duration_api_ms": getattr(msg, "duration_api_ms", 0) or 0,
                    "num_turns": getattr(msg, "num_turns", 0) or 0,
                    "stop_reason": getattr(msg, "stop_reason", "unknown") or "unknown",
                    "session_id": getattr(msg, "session_id", None),
                }
                orch.emit_event(
                    "run.cost",
                    {
                        "caller_id": spec.caller_id,
                        "ts": time.time(),
                        **result_data,
                    },
                )

            # SystemMessage / UserMessage / anything else: ignored by design.
            # UserMessage carries tool-result echoes; ToolUseBlocks are the
            # MCP layer's concern. Unknown types must not crash the loop.

        # Emit assistant_text for the final message_id (last message in the
        # stream, not followed by a new message_id to trigger the flush above).
        if last_message_id and current_turn_text_parts:
            orch.emit_event(
                "assistant_text",
                {
                    "ts": time.time(),
                    "caller_id": spec.caller_id,
                    "message_id": last_message_id,
                    "text": "".join(current_turn_text_parts),
                    "stop_reason": result_data["stop_reason"] if result_data else "unknown",
                },
            )

    except asyncio.CancelledError:
        # When the watchdog cancels run_task (terminate-grace backstop), the
        # drain loop exits here.  Mark terminate_consumed so was_terminated()
        # returns True and the resume-not-terminate policy is not triggered.
        if _drain_rec is not None and not _drain_rec.terminate_consumed:
            _drain_rec.terminate_consumed = True
        orch.emit_event(
            "agent_completed",
            {
                "caller_id": spec.caller_id,
                "terminated": False,
                "stop_reason": "cancelled",
                "ts": time.time(),
            },
        )
        raise
    except Exception as exc:
        orch.emit_event(
            "agent_error",
            {
                "caller_id": spec.caller_id,
                "exception": type(exc).__name__,
                "msg": str(exc),
                "error": str(exc),
                "ts": time.time(),
            },
        )
        raise

    terminated = bool(orch.was_terminated(spec.caller_id))
    stop_reason = result_data["stop_reason"] if result_data else "no_result"

    orch.emit_event(
        "agent_completed",
        {
            "caller_id": spec.caller_id,
            "terminated": terminated,
            "stop_reason": stop_reason,
            "ts": time.time(),
        },
    )

    # contract_violation == loop exited cleanly but the agent never
    # consumed a terminate sentinel. Orchestrator reads this flag and
    # applies the resume-not-terminate policy (docs/agent-runtime.md #4).
    contract_violation = not terminated

    return RunResult(
        final_text="".join(final_text_parts),
        total_cost_usd=float(result_data["total_cost_usd"]) if result_data else 0.0,
        total_usage=result_data["usage"] if result_data else {},
        num_turns=result_data["num_turns"] if result_data else 0,
        duration_ms=result_data["duration_ms"] if result_data else 0,
        duration_api_ms=result_data["duration_api_ms"] if result_data else 0,
        stop_reason=stop_reason,
        session_id=result_data["session_id"] if result_data else None,
        terminated=terminated,
        contract_violation=contract_violation,
        model_usage=result_data["model_usage"] if result_data else {},
    )


__all__ = ["SpawnSpec", "RunResult", "run_agent", "build_hooks"]
