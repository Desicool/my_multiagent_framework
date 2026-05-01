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

``HookRegistry`` loads skill-module hooks (``module.toml`` + ``gate.py`` /
``eval.py``) and merges them with the built-in hooks. See
``docs/skill-modules.md`` for the full specification.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import tomllib
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk.types import HookMatcher

from beidou.agent.context import (
    Block,
    Pass,
    ToolCallContext,
    ToolResultContext,
)
from beidou.engine.graph import USER_SENTINEL
from beidou.primitives.core import Orchestrator

logger = logging.getLogger(__name__)

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

# Tools an agent may call even when the reply gate is active
# (i.e. when it has unreplied inquiries from its leader or another agent).
# Read/grep/bash permit information gathering; send_message is the resolution tool;
# report_status and ask_user are required for the agent to complete or get human input.
REPLY_GATE_ALLOWLIST = {
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "mcp__beidou__send_message",
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
            # Clear any pending reply obligations -- agent reported done,
            # reply obligations are moot.
            if rec.pending_replies:
                for msg_id, obl in rec.pending_replies.items():
                    orch.emit_event("reply.abandoned", {
                        "agent_id": caller_id,
                        "message_id": msg_id,
                        "from_id": obl["from_id"],
                        "reason": "completion_reported",
                        "ts": time.time(),
                    })
                rec.pending_replies.clear()
                rec.reply_gate_active = False

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

    async def on_send_message_reply(input_data: Any, tool_use_id: Optional[str], context: Any) -> dict:
        """Clear the oldest pending reply obligation when send_message succeeds.

        Fires on every successful ``mcp__beidou__send_message`` call.  Each reply
        clears one obligation:

        * **Explicit correlation:** if ``in_reply_to`` names a known obligation
          (the agent is intentionally replying to a specific inquiry), clear
          that exact message_id.
        * **FIFO fallback:** otherwise, clear the oldest obligation from the
          same sender (``to`` field).

        When all obligations are cleared, ``reply_gate_active`` is lifted so
        the agent regains full tool access.
        """
        if input_data.get("tool_name") != "mcp__beidou__send_message":
            return {}
        tool_response = input_data.get("tool_response") or {}
        if isinstance(tool_response, dict) and tool_response.get("is_error"):
            return {}

        rec = orch._agents.get(caller_id)  # type: ignore[attr-defined]
        if rec is None or not rec.pending_replies:
            return {}

        tool_input = input_data.get("tool_input") or {}
        to = tool_input.get("to", "")
        in_reply_to = tool_input.get("in_reply_to", None)

        # Explicit correlation: clear exact message_id if specified.
        if in_reply_to and in_reply_to in rec.pending_replies:
            del rec.pending_replies[in_reply_to]
            orch.emit_event("reply.fulfilled", {
                "agent_id": caller_id,
                "message_id": in_reply_to,
                "method": "explicit",
                "ts": time.time(),
            })
        else:
            # FIFO: clear oldest obligation from this sender.
            oldest_msg_id = None
            oldest_ts = None
            for msg_id, obl in rec.pending_replies.items():
                if obl["from_id"] == to:
                    if oldest_ts is None or obl["ts"] < oldest_ts:
                        oldest_ts = obl["ts"]
                        oldest_msg_id = msg_id
            if oldest_msg_id:
                del rec.pending_replies[oldest_msg_id]
                orch.emit_event("reply.fulfilled", {
                    "agent_id": caller_id,
                    "message_id": oldest_msg_id,
                    "method": "fifo",
                    "ts": time.time(),
                })

        # If no more pending obligations, lift the gate.
        if not rec.pending_replies:
            rec.reply_gate_active = False

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

    async def on_reply_gate(input_data: Any, tool_use_id: Optional[str], context: Any) -> dict:
        """Block an agent from taking non-reply actions while reply obligations are pending.

        Fires on EVERY tool call (matcher=None). When ``reply_gate_active`` is
        True on the agent's record, only tools in ``REPLY_GATE_ALLOWLIST`` are
        permitted. All other tools are denied with a directive to reply first.

        The gate is activated at turn-end (in the ResultMessage handler) when
        the agent has unreplied inquiries, and deactivated by the
        ``on_send_message_reply`` PostToolUse hook once all obligations are
        cleared.
        """
        rec = orch._agents.get(caller_id)  # type: ignore[attr-defined]
        if rec is None or not rec.reply_gate_active:
            return {}

        tool_name = input_data.get("tool_name", "")

        if tool_name in REPLY_GATE_ALLOWLIST:
            return {}

        # Build deny message naming pending senders.
        from_ids = list(dict.fromkeys(
            obl["from_id"] for obl in rec.pending_replies.values()
        ))

        orch.emit_event("reply_gate.denied", {
            "agent_id": caller_id,
            "tool_name": tool_name,
            "pending_senders": from_ids,
        })

        reason = (
            f"Cannot call {tool_name!r} — you have unreplied inquiries from: {', '.join(from_ids)}.\n"
            f"Call mcp__beidou__send_message(to=<agent>, content='...') to reply first."
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
            # Reply gate MUST run before review gate to avoid deadlock when
            # an agent is both a leader (has pending reviews) and a member
            # (has pending replies). matcher=None -> fires for every tool call.
            HookMatcher(
                matcher=None,
                hooks=[on_reply_gate],
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
            HookMatcher(
                matcher="mcp__beidou__send_message",
                hooks=[on_send_message_reply],
            ),
        ],
    }


# Backward-compatible alias.
build_hooks = build_builtin_hooks


# ---------------------------------------------------------------------------
# HookRegistry: skill-module hooks (module.toml + gate.py / eval.py)
# ---------------------------------------------------------------------------

# Hook points that use gate handlers (return Pass/Block).
_GATE_HOOK_POINTS = frozenset({
    "validate_tool_call",
    "validate_tool_result",
    "filter_input",
    "filter_output",
})

# Hook points that use eval handlers (return None, fire-and-forget).
_EVAL_HOOK_POINTS = frozenset({
    "before_agent_start",
    "evaluate_turn",
    "on_event",
})

# Hook points integrated via SDK HookMatcher (PreToolUse / PostToolUse).
_SDK_HOOK_MAP: dict[str, str] = {
    "validate_tool_call": "PreToolUse",
    "validate_tool_result": "PostToolUse",
}

# All valid hook point names recognised by the registry.
_KNOWN_HOOK_POINTS = _GATE_HOOK_POINTS | _EVAL_HOOK_POINTS


class HookRegistry:
    """Loads and manages skill-module hooks from ``module.toml`` / ``gate.py`` / ``eval.py``.

    Parameters
    ----------
    skill_dir:
        Directory containing ``SKILL.md`` and optionally ``module.toml``,
        ``gate.py``, ``eval.py``.
    orch:
        Orchestrator instance (for event emission).
    caller_id:
        The agent id this registry is scoped to.
    leader_id:
        The leader's agent id (or ``__user__`` for root).
    """

    def __init__(
        self,
        skill_dir: Path,
        orch: Orchestrator,
        caller_id: str,
        leader_id: str,
    ):
        self._skill_dir = Path(skill_dir)
        self._orch = orch
        self._caller_id = caller_id
        self._leader_id = leader_id

        self._config: dict = {}
        self._gate_module: Any = None
        self._eval_module: Any = None
        self._gate_import_error: str | None = None
        self._eval_import_error: str | None = None

        # Resolved handler config per hook point:
        #   {hook_point: {"mode": str, "handlers": [callable], "events": list[str]}}
        self._handlers: dict[str, dict] = {}

        self.load()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def has_modules(cls, skill_dir: Path) -> bool:
        """Return True if ``skill_dir`` contains a ``module.toml``."""
        return (Path(skill_dir) / "module.toml").is_file()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Parse ``module.toml``, import referenced ``gate.py`` / ``eval.py``,
        and resolve every declared handler to a callable.

        If a module file cannot be imported, gate hooks are replaced with a
        blocking sentinel (fail-closed) and eval hooks are silently skipped
        (fail-open), per ``docs/skill-modules.md``.
        """
        toml_path = self._skill_dir / "module.toml"
        if not toml_path.is_file():
            return

        # Parse module.toml (binary mode required by tomllib).
        with open(toml_path, "rb") as fh:
            self._config = tomllib.load(fh)

        hooks_cfg = self._config.get("hooks", {})
        if not hooks_cfg:
            return

        # Determine which module files are referenced by any handler.
        need_gate = False
        need_eval = False
        for _hp, cfg in hooks_cfg.items():
            for name in cfg.get("handlers", []):
                if name.startswith("gate."):
                    need_gate = True
                elif name.startswith("eval."):
                    need_eval = True

        # Import gate.py (fail-closed).
        if need_gate:
            gate_path = self._skill_dir / "gate.py"
            if gate_path.is_file():
                try:
                    self._gate_module = self._import_module("gate", gate_path)
                except Exception as exc:
                    self._gate_import_error = f"{type(exc).__name__}: {exc}"
                    self._orch.emit_event(
                        "module.gate_import_error",
                        {
                            "caller_id": self._caller_id,
                            "skill_dir": str(self._skill_dir),
                            "error": self._gate_import_error,
                            "ts": time.time(),
                        },
                    )
            else:
                self._gate_import_error = "gate.py not found"

        # Import eval.py (fail-open).
        if need_eval:
            eval_path = self._skill_dir / "eval.py"
            if eval_path.is_file():
                try:
                    self._eval_module = self._import_module("eval", eval_path)
                except Exception as exc:
                    self._eval_import_error = f"{type(exc).__name__}: {exc}"
                    self._orch.emit_event(
                        "module.eval_import_error",
                        {
                            "caller_id": self._caller_id,
                            "skill_dir": str(self._skill_dir),
                            "error": self._eval_import_error,
                            "ts": time.time(),
                        },
                    )
            else:
                self._eval_import_error = "eval.py not found"

        # Resolve handlers for each declared hook point.
        for hook_point, cfg in hooks_cfg.items():
            if hook_point not in _KNOWN_HOOK_POINTS:
                continue

            mode = cfg.get("mode", "all_must_pass")
            events = cfg.get("events", [])

            # Gate hook points with a failed gate.py import get a blocking
            # sentinel instead of trying (and failing) to resolve each name.
            if hook_point in _GATE_HOOK_POINTS and self._gate_import_error:
                self._handlers[hook_point] = {
                    "mode": mode,
                    "handlers": [self._make_blocking_gate_handler(hook_point)],
                    "events": events,
                }
                continue

            # Eval hook points with a failed eval.py import are silently
            # skipped — no handlers registered.
            if hook_point in _EVAL_HOOK_POINTS and self._eval_import_error:
                continue

            resolved: list = []
            for handler_name in cfg.get("handlers", []):
                try:
                    func = self._resolve_handler(handler_name)
                    resolved.append(func)
                except Exception as exc:
                    self._orch.emit_event(
                        "module.handler_resolve_error",
                        {
                            "caller_id": self._caller_id,
                            "hook_point": hook_point,
                            "handler_name": handler_name,
                            "error": f"{type(exc).__name__}: {exc}",
                            "ts": time.time(),
                        },
                    )

            if resolved:
                self._handlers[hook_point] = {
                    "mode": mode,
                    "handlers": resolved,
                    "events": events,
                }

    # ------------------------------------------------------------------
    # Module import
    # ------------------------------------------------------------------

    def _import_module(self, name: str, file_path: Path) -> Any:
        """Import a Python module with a unique name per spawn.

        Uses a UUID hex suffix to prevent cross-agent state leakage
        (``docs/skill-modules.md`` "Handler resolution").
        """
        unique_name = f"beidou_skill_{name}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(unique_name, str(file_path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create module spec for {file_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    # ------------------------------------------------------------------
    # Handler resolution
    # ------------------------------------------------------------------

    def _resolve_handler(self, handler_name: str):
        """Resolve ``"module.func_name"`` to the actual callable.

        Raises
        ------
        ValueError
            If the handler name format is invalid or the module prefix is
            not ``gate`` or ``eval``.
        ImportError
            If the referenced module has not been loaded.
        AttributeError
            If the named function does not exist on the module.
        """
        parts = handler_name.split(".", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid handler name {handler_name!r}: "
                f"expected 'module.func_name' format"
            )
        module_name, func_name = parts

        if module_name == "gate":
            if self._gate_module is None:
                raise ImportError("gate.py is not loaded")
            func = getattr(self._gate_module, func_name, None)
            if func is None:
                raise AttributeError(
                    f"gate.py has no function named {func_name!r}"
                )
            return func

        elif module_name == "eval":
            if self._eval_module is None:
                raise ImportError("eval.py is not loaded")
            func = getattr(self._eval_module, func_name, None)
            if func is None:
                raise AttributeError(
                    f"eval.py has no function named {func_name!r}"
                )
            return func

        else:
            raise ValueError(
                f"Unknown module prefix {module_name!r} in handler {handler_name!r}"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_emit(self):
        """Return an ``emit(event_type, payload)`` callable bound to this agent.

        Auto-injects ``caller_id`` and ``ts`` so handler authors don't need to
        manage them manually.
        """
        orch = self._orch
        caller_id = self._caller_id

        def emit(event_type: str, payload: dict) -> None:
            payload.setdefault("caller_id", caller_id)
            payload.setdefault("ts", time.time())
            orch.emit_event(event_type, payload)

        return emit

    def _make_blocking_gate_handler(self, hook_point: str):
        """Return an async handler that always returns Block.

        Used when gate.py fails to import — the gate is fail-closed so no
        tool call or output passes through.
        """
        reason = (
            f"gate.py import failed for hook {hook_point}: "
            f"{self._gate_import_error}"
        )

        async def _blocking(ctx) -> Block:
            return Block(reason=reason)

        _blocking.__name__ = f"blocking_gate_{hook_point}"
        return _blocking

    def _make_context_for(
        self,
        hook_point: str,
        input_data: dict,
        tool_use_id: Optional[str],
    ):
        """Create the typed context dataclass from SDK ``input_data``.

        Only called for the two SDK-integrated hook points
        (``validate_tool_call`` → ``ToolCallContext``,
        ``validate_tool_result`` → ``ToolResultContext``).
        """
        emit = self._make_emit()

        if hook_point == "validate_tool_call":
            return ToolCallContext(
                agent_id=self._caller_id,
                tool_name=input_data.get("tool_name", ""),
                tool_input=dict(input_data.get("tool_input", {}) or {}),
                tool_use_id=input_data.get("tool_use_id", tool_use_id or ""),
                emit=emit,
            )

        if hook_point == "validate_tool_result":
            tool_response = input_data.get("tool_response") or {}
            if isinstance(tool_response, dict):
                is_error = bool(tool_response.get("is_error"))
                duration_ms = tool_response.get("duration_ms", 0)
                result_val = tool_response
            else:
                is_error = False
                duration_ms = 0
                result_val = tool_response
            return ToolResultContext(
                agent_id=self._caller_id,
                tool_name=input_data.get("tool_name", ""),
                tool_use_id=input_data.get("tool_use_id", tool_use_id or ""),
                result=result_val,
                is_error=is_error,
                duration_ms=duration_ms if isinstance(duration_ms, int) else 0,
                emit=emit,
            )

        raise ValueError(f"Unsupported SDK hook point: {hook_point!r}")

    # ------------------------------------------------------------------
    # Gate / eval chain runners
    # ------------------------------------------------------------------

    async def _run_gate_chain(
        self,
        handlers: list,
        mode: str,
        ctx,
    ) -> Pass | Block:
        """Run gate handlers sequentially, short-circuit on first Block.

        Both ``all_must_pass`` and ``first_block_wins`` execute the same way:
        handlers run in declaration order; the first Block stops the chain;
        all must return Pass for the gate to pass.

        **Fail-closed:** any exception raised by a handler is treated as Block.
        """
        for handler in handlers:
            try:
                result = handler(ctx)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, Block):
                    return result
                # Pass or None or anything else — continue to next handler.
            except Exception as exc:
                return Block(
                    reason=(
                        f"gate_handler_error: {type(exc).__name__}: {exc}"
                    )
                )
        return Pass()

    async def _run_eval_chain(self, handlers: list, ctx) -> None:
        """Run all eval handlers concurrently.

        **Fail-open:** exceptions are logged via ``module.eval_handler_error``
        events but never block the agent.
        """
        orch = self._orch

        async def _safe_run(handler):
            try:
                result = handler(ctx)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                orch.emit_event(
                    "module.eval_handler_error",
                    {
                        "caller_id": self._caller_id,
                        "handler": getattr(handler, "__name__", str(handler)),
                        "error": f"{type(exc).__name__}: {exc}",
                        "ts": time.time(),
                    },
                )

        await asyncio.gather(*[_safe_run(h) for h in handlers])

    # ------------------------------------------------------------------
    # SDK hook construction
    # ------------------------------------------------------------------

    def build_sdk_hooks(self) -> dict:
        """Build SDK ``HookMatcher`` dicts for the two SDK-integrated hook points.

        Returns a dict with ``"PreToolUse"`` and/or ``"PostToolUse"`` keys,
        each mapping to a list of ``HookMatcher`` objects.  Returns an empty
        dict when the skill has no handlers for either point.

        Hook points covered:

        * ``validate_tool_call`` → ``PreToolUse`` (matcher=None, fires every tool)
        * ``validate_tool_result`` → ``PostToolUse`` (matcher=None, fires every tool)

        The returned dict is suitable for merging with built-in hooks via
        :meth:`merge_with_builtins`.
        """
        result: dict[str, list] = {}

        for hook_point, sdk_event in _SDK_HOOK_MAP.items():
            if hook_point not in self._handlers:
                continue
            cfg = self._handlers[hook_point]
            handler_func = self._build_sdk_gate_handler(
                hook_point, cfg["handlers"], cfg["mode"], sdk_event
            )
            result.setdefault(sdk_event, []).append(
                HookMatcher(
                    matcher=None,
                    hooks=[handler_func],
                    timeout=HOOK_REVIEW_TIMEOUT_S,
                )
            )

        return result

    def _build_sdk_gate_handler(
        self,
        hook_point: str,
        handlers: list,
        mode: str,
        sdk_event_name: str,
    ):
        """Build an async handler function suitable for an SDK HookMatcher.

        The returned callable has the signature
        ``(input_data, tool_use_id, context) -> dict`` required by the SDK.
        """
        make_ctx = self._make_context_for
        run_chain = self._run_gate_chain

        async def _gate_hook(
            input_data: Any,
            tool_use_id: Optional[str],
            context: Any,
        ) -> dict:
            ctx = make_ctx(hook_point, input_data, tool_use_id)
            result = await run_chain(handlers, mode, ctx)
            if isinstance(result, Block):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": sdk_event_name,
                        "permissionDecision": "deny",
                        "permissionDecisionReason": result.reason,
                    }
                }
            return {}

        return _gate_hook

    # ------------------------------------------------------------------
    # Non-SDK hook point runners
    # ------------------------------------------------------------------

    async def run_gate_hook(self, hook_point: str, ctx) -> Pass | Block:
        """Run gate handlers for a non-SDK hook point.

        Used for ``filter_input`` and ``filter_output`` which are called
        directly from the agent loop, not via SDK hooks.

        Parameters
        ----------
        hook_point:
            One of ``"filter_input"`` or ``"filter_output"``.
        ctx:
            The typed context (``InputContext`` or ``OutputContext``).
        """
        if hook_point not in self._handlers:
            return Pass()
        cfg = self._handlers[hook_point]
        return await self._run_gate_chain(cfg["handlers"], cfg["mode"], ctx)

    async def run_eval_hook(self, hook_point: str, ctx) -> None:
        """Run eval handlers for a non-SDK hook point.

        Used for ``before_agent_start``, ``evaluate_turn``, and ``on_event``
        which are called directly from the agent loop.

        For ``on_event``, the ``events`` subscription list from
        ``module.toml`` is honoured: if the event type does not appear in the
        list, the handlers are skipped.

        Parameters
        ----------
        hook_point:
            One of ``"before_agent_start"``, ``"evaluate_turn"``, or ``"on_event"``.
        ctx:
            The typed context (``AgentStartContext``, ``TurnEvalContext``,
            or ``EventContext``).
        """
        if hook_point not in self._handlers:
            return
        cfg = self._handlers[hook_point]

        # on_event subscription filter.
        if hook_point == "on_event":
            subscribed: list[str] = cfg.get("events", [])
            if subscribed:
                event_type = getattr(ctx, "event_type", "")
                if event_type not in subscribed:
                    return

        await self._run_eval_chain(cfg["handlers"], ctx)

    # ------------------------------------------------------------------
    # Merge with built-ins
    # ------------------------------------------------------------------

    def merge_with_builtins(self, builtins_hooks_dict: dict) -> dict:
        """Merge module SDK hooks **after** built-in hooks.

        Built-in hooks ALWAYS run first per ``docs/skill-modules.md``.
        Module hooks are appended after them for each SDK event type.

        Parameters
        ----------
        builtins_hooks_dict:
            The dict returned by :func:`build_builtin_hooks`.
        """
        merged: dict[str, list] = {}

        # Shallow-copy built-in lists so we don't mutate the original.
        for event_type, matchers in builtins_hooks_dict.items():
            merged[event_type] = list(matchers)

        # Append module hooks.
        for event_type, matchers in self.build_sdk_hooks().items():
            merged.setdefault(event_type, []).extend(matchers)

        return merged


__all__ = [
    "ALLOWED_DURING_PENDING_REVIEW",
    "HOOK_REVIEW_TIMEOUT_S",
    "REPLY_GATE_ALLOWLIST",
    "HookRegistry",
    "build_builtin_hooks",
    "build_hooks",
]
