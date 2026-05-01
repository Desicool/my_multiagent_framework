"""Built-in SDK hooks for Beidou agent runtime.

Moved from ``beidou/sdk_agent.py``. These are the three built-in policy hooks
registered on every agent spawn:

* ``on_ask_user_question`` — intercept raw AskUserQuestion, route to human gateway.
* ``on_review_gate`` — block leader from advancing while children await review.
* ``on_report_status`` — completion handoff: read assistant text, route to leader/user.

``build_builtin_hooks()`` is the canonical name (post-refactor).
``build_hooks()`` is kept as a backward-compatible alias.

These hooks are **not** moving to skill modules. They stay here as built-in
runtime policy registered on every agent spawn. User gate handlers from
``module.toml`` run **after** these built-in hooks pass.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Optional

from claude_agent_sdk.types import HookMatcher

from beidou.engine.graph import USER_SENTINEL
from beidou.primitives.core import Orchestrator

# Hook execution cap for hooks that block on a human gateway round-trip.
# Default in claude-code is 60s, which silently truncates real reviews.
HOOK_REVIEW_TIMEOUT_S: float = 1800.0

# Tools a leader may call even when it has children awaiting review.
# Read/grep/bash let the leader inspect artifacts before deciding.
# The beidou primitives listed here are the two valid resolution actions
# (terminate_child, send_message) plus informational/status tools.
#
# bd issue xq1: also include SDK-builtin classic names that skill
# allowed-tools entries may resolve to via _SDK_BUILTIN_MAP or directly.
# - SendMessage: SDK-builtin classic for inter-agent messaging
# - AskUserQuestion: SDK-builtin; on_ask_user_question handles review-gate
#   semantics when pending children exist -- on_review_gate should pass through
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


def build_builtin_hooks(orch: Orchestrator, caller_id: str, leader_id: str) -> dict:
    """Build hook dicts for the SDK agent.

    Hooks registered:

    **PreToolUse -- AskUserQuestion**
        Intercepts raw ``AskUserQuestion`` tool calls emitted by models that do not
        use the ``mcp__beidou__ask_user`` namespaced primitive (e.g. MiniMax-M2.7 via
        the Anthropic-compatible proxy).  The hook routes the question(s) to the human
        gateway (same plumbing as ``ask_user``) and returns the answer as the tool's
        effective response by denying the tool call with a ``permissionDecisionReason``.
        Emits a synthetic ``tool_called`` + ``tool_result`` pair so the UI/JSONL log
        reflects what happened.

    **PostToolUse -- mcp__beidou__report_status**
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

    async def on_ask_user_question(input_data: Any, tool_use_id: Optional[str], context: Any) -> dict:
        """Intercept raw AskUserQuestion tool calls and route them to the human gateway.

        The SDK emits ``permissionDecision="deny"`` with the user's answer as
        ``permissionDecisionReason`` so the model receives the answer and proceeds.
        """
        # Defensive guard -- HookMatcher should filter, but be safe.
        if input_data.get("tool_name") != "AskUserQuestion":
            return {}

        raw_input = input_data.get("tool_input") or {}
        questions = raw_input.get("questions") or []
        if not isinstance(questions, list) or not questions:
            # Nothing to ask -- deny with a clear reason so the model knows to use ask_user.
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
        except Exception as exc:  # noqa: BLE001 -- gateway can be diverse
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
            rec.idle_nudge_count = 0

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
                f"Deliverables: (none provided -- child failed to embed envelope)\n"
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
        # agent leader. Approve -> terminate_root; Rework -> deliver a rework
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
        # (terminate_consumed=True) -- a stale completion_pending flag on a
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
            f"  o mcp__beidou__terminate_child(agent_id=\"{pid}\")  to approve, OR\n"
            f"    mcp__beidou__send_message(to=\"{pid}\", content=\"rework: <what>\")  to ask for changes."
            for pid in pending_ids
        )

        reason = (
            f"Cannot call {tool_name!r} -- {pending_desc}.\n"
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
            # matcher=None -> fires for every tool call.
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


# Backward-compatible alias.
build_hooks = build_builtin_hooks


__all__ = [
    "ALLOWED_DURING_PENDING_REVIEW",
    "HOOK_REVIEW_TIMEOUT_S",
    "build_builtin_hooks",
    "build_hooks",
]
