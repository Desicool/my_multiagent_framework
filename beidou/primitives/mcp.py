"""MCP adapter for Beidou's agent primitives.

Each primitive in :mod:`beidou.primitives.core` is exposed to a single
``claude-agent-sdk`` agent as an in-process MCP tool under the ``beidou``
server. The agent-visible names are (see ``docs/tool-surface.md``):

* ``mcp__beidou__send_message``         -- A2A enqueue.
* ``mcp__beidou__list_peers``           -- Peer snapshot (team/children/all).
* ``mcp__beidou__ask_user``             -- Question via the leader chain.
* ``mcp__beidou__answer_question``      -- Resolve an inbox question (leaders).
* ``mcp__beidou__escalate_question``    -- Push an inbox question up the chain.
* ``mcp__beidou__report_status``        -- State update + observability.
* ``mcp__beidou__declare_plan``         -- Validate DAG + persist plan; no team/agent created.
* ``mcp__beidou__remove_plan``          -- Remove caller's active plan for replanning.
* ``mcp__beidou__spawn_agent``          -- Gated spawn from plan; lazily creates team on first call.
* ``mcp__beidou__list_ready``           -- Read-only: return ready task ids in caller's active plan.
* ``mcp__beidou__create_team``          -- DEPRECATED. Spawn a sub-team; caller becomes leader.
* ``mcp__beidou__terminate_child``      -- Post a terminate sentinel to a child.
* ``mcp__beidou__list_pending_reviews`` -- Read-only list of children awaiting review.

Per ``docs/tool-surface.md``: ``caller_id`` is baked into the per-spawn MCP
server via closure -- the model NEVER supplies it, and every primitive runs
through a uniform error-translation wrapper (``_wrap``). Tool observability
(``tool_called`` / ``tool_result`` events) is owned by the drain loop in
``beidou/sdk_agent.py``, not here.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool

from .core import (
    Orchestrator,
    PrimitiveError,
    answer_question,
    ask_user,
    create_team,
    declare_plan,
    escalate_question,
    list_peers,
    list_pending_reviews,
    list_ready,
    remove_plan,
    report_status,
    send_message,
    spawn_agent,
    terminate_child,
)

# Max characters of a JSON-serialised arg value kept in observability events.
# Anything longer is replaced with a "<redacted: N chars>" placeholder so
# large messages / task descriptions don't bloat the event log.
_ARG_REDACT_THRESHOLD = 512


def _json(obj: Any) -> str:
    """Stable JSON encoding used for tool result content and error payloads."""
    return json.dumps(obj, default=str, ensure_ascii=False)


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Shallow copy of args, replacing large string values with a placeholder."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > _ARG_REDACT_THRESHOLD:
            out[k] = f"<redacted: {len(v)} chars>"
        elif isinstance(v, (list, dict)):
            encoded = _json(v)
            if len(encoded) > _ARG_REDACT_THRESHOLD:
                out[k] = f"<redacted: {len(encoded)} chars>"
            else:
                out[k] = v
        else:
            out[k] = v
    return out


def build_mcp_server_for(orch: Orchestrator, caller_id: str):
    """Build a per-spawn in-process MCP server bound to a single agent.

    The returned value is an ``McpSdkServerConfig`` suitable for:

    .. code-block:: python

        options = ClaudeAgentOptions(
            mcp_servers={"beidou": build_mcp_server_for(orch, caller_id)},
            allowed_tools=[
                "mcp__beidou__send_message",
                "mcp__beidou__list_peers",
                "mcp__beidou__ask_user",
                "mcp__beidou__report_status",
                "mcp__beidou__create_team",
                "mcp__beidou__terminate_child",
                "mcp__beidou__list_pending_reviews",
            ],
            ...,
        )

    ``caller_id`` is captured by closure and never read from tool input.
    Tool observability (``tool_called`` / ``tool_result`` events) is the
    responsibility of the drain loop in ``beidou/sdk_agent.py``.

    Errors are translated into structured tool results the model can reason
    about rather than raised exceptions: ``PrimitiveError`` becomes an
    ``is_error=True`` result carrying ``{error, message, details}``; any
    unexpected exception becomes a generic ``internal_error`` result.
    """

    async def _wrap(
        tool_name: str,
        fn: Callable[..., Any],
        raw_args: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            result = await fn(**kwargs)
        except PrimitiveError as e:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": _json(
                            {
                                "error": e.code,
                                "message": e.message,
                                "details": e.details,
                            }
                        ),
                    }
                ],
                "is_error": True,
            }
        except Exception as e:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": _json(
                            {"error": "internal_error", "message": str(e)}
                        ),
                    }
                ],
                "is_error": True,
            }
        else:
            return {
                "content": [{"type": "text", "text": _json(result)}],
            }

    # ------------------------------------------------------------------
    # Tool definitions. Schemas mirror ``docs/tool-surface.md`` field by
    # field. Tools with optional fields use explicit JSON Schema (with an
    # explicit ``required`` list) because the dict-style shorthand the SDK
    # accepts marks every listed field as required.
    # ------------------------------------------------------------------

    @tool(
        "send_message",
        "Post a message to another agent's inbox. The only agent-to-agent "
        "primitive. Same-task only; inbox cap enforced by Beidou.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient agent_id."},
                "content": {"type": "string", "description": "Message body."},
                "expects_reply": {
                    "type": "boolean",
                    "description": (
                        "Optional. Default false. When true, the message is "
                        "created as an inquiry (kind='inquiry') and the "
                        "recipient sees it as [INQUIRY ... reply expected]."
                    ),
                },
            },
            "required": ["to", "content"],
        },
    )
    async def _send_message(args: dict[str, Any]) -> dict[str, Any]:
        return await _wrap(
            "send_message",
            send_message,
            args,
            orch=orch,
            caller_id=caller_id,
            to=args["to"],
            content=args["content"],
            expects_reply=bool(args.get("expects_reply", False)),
        )

    @tool(
        "list_peers",
        "List peers in the task graph. scope='team' (default, direct "
        "teammates), 'children' (direct reports of teams you lead), or 'all' "
        "(entire task graph).",
        {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["team", "children", "all"],
                    "description": "Scope of the peer snapshot.",
                },
            },
            "required": [],
        },
    )
    async def _list_peers(args: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"orch": orch, "caller_id": caller_id}
        if "scope" in args and args["scope"] is not None:
            kwargs["scope"] = args["scope"]
        return await _wrap("list_peers", list_peers, args, **kwargs)

    @tool(
        "ask_user",
        "Route one or more questions to Beidou's human gateway. Blocks until "
        "the user answers all sub-questions. Returns a structured error if no "
        "gateway is available or the user declines.",
        {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "List of 1..4 sub-questions to ask the user.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The question text.",
                            },
                            "header": {
                                "type": "string",
                                "maxLength": 12,
                                "description": "Short label (<=12 chars). Empty string to omit.",
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "Whether multiple options may be chosen.",
                            },
                            "options": {
                                "type": "array",
                                "description": "Choice options. Length 0 for free-text, 2..4 for choice.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                        },
                                        "description": {
                                            "type": "string",
                                        },
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                        },
                        "required": ["question", "header", "multiSelect", "options"],
                    },
                },
                "context": {
                    "type": "string",
                    "description": "Optional background context to accompany the questions.",
                },
            },
            "required": ["questions"],
        },
    )
    async def _ask_user(args: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "orch": orch,
            "caller_id": caller_id,
            "questions": args["questions"],
        }
        if "context" in args and args["context"] is not None:
            kwargs["context"] = args["context"]
        return await _wrap("ask_user", ask_user, args, **kwargs)

    @tool(
        "answer_question",
        "Resolve a question that arrived in your inbox via the leader chain. "
        "Only the current holder may answer. Use this when you can answer "
        "the asker's question directly from what you already know (the user "
        "task, requirements.md, prior answers).",
        {
            "type": "object",
            "properties": {
                "qid": {
                    "type": "string",
                    "description": "The question id from the [INBOX QUESTION] system message.",
                },
                "answers": {
                    "type": "array",
                    "minItems": 1,
                    "description": "One entry per sub-question, in order. Each entry "
                                   "is {selected_labels: list[str], text: str|null}. For "
                                   "free-text or 'Other' answers, leave selected_labels "
                                   "empty and put the answer in text.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "selected_labels": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "text": {
                                "type": ["string", "null"],
                            },
                        },
                        "required": ["selected_labels"],
                    },
                },
                "reason": {
                    "type": "string",
                    "description": "Short note explaining why you can answer this directly "
                                   "instead of escalating (audit trail).",
                },
            },
            "required": ["qid", "answers", "reason"],
        },
    )
    async def _answer_question(args: dict[str, Any]) -> dict[str, Any]:
        return await _wrap(
            "answer_question",
            answer_question,
            args,
            orch=orch,
            caller_id=caller_id,
            qid=args["qid"],
            answers=args["answers"],
            reason=args["reason"],
        )

    @tool(
        "escalate_question",
        "Push a question one hop further up the leader chain. Use this when "
        "you can't answer the asker's question yourself — the next-up "
        "reviewer (your own leader, or eventually the user) will see it. "
        "Only the current holder may escalate.",
        {
            "type": "object",
            "properties": {
                "qid": {
                    "type": "string",
                    "description": "The question id from the [INBOX QUESTION] system message.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short note explaining why you can't answer.",
                },
            },
            "required": ["qid", "reason"],
        },
    )
    async def _escalate_question(args: dict[str, Any]) -> dict[str, Any]:
        return await _wrap(
            "escalate_question",
            escalate_question,
            args,
            orch=orch,
            caller_id=caller_id,
            qid=args["qid"],
            reason=args["reason"],
        )

    @tool(
        "report_status",
        "Report your current state (working|idle|blocked|done) with an "
        "optional free-text detail. 'done' triggers parent liveness "
        "re-evaluation.",
        {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["working", "idle", "blocked", "done"],
                    "description": "Current agent state.",
                },
                "detail": {
                    "type": "string",
                    "description": "Free-text summary (required in practice when state='done').",
                },
            },
            "required": ["state"],
        },
    )
    async def _report_status(args: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "orch": orch,
            "caller_id": caller_id,
            "state": args["state"],
        }
        if "detail" in args and args["detail"] is not None:
            kwargs["detail"] = args["detail"]
        return await _wrap("report_status", report_status, args, **kwargs)

    @tool(
        "create_team",
        "Spawn a sub-team. You become its leader by construction. 'roles' is "
        "a list of {role, skill, model?, description} dicts -- one per "
        "member. Beidou rejects any leader_id override.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable team name."},
                "task": {"type": "string", "description": "Task description."},
                "roles": {
                    "type": "array",
                    "description": "One object per member with role, skill, optional model, and description.",
                    "items": {"type": "object"},
                },
                "rules": {
                    "type": "array",
                    "description": "Optional coordination rules visible to each member.",
                    "items": {"type": "string"},
                },
                "consensus": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Default false. Set true only when deliberately spawning "
                        "N agents to attempt the same task in parallel for voting "
                        "or ensemble/consensus purposes. Without this flag, N>1 "
                        "roles that all share the same (skill, description) are "
                        "rejected as a footgun guard."
                    ),
                },
            },
            "required": ["name", "task", "roles"],
        },
    )
    async def _create_team(args: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "orch": orch,
            "caller_id": caller_id,
            "name": args["name"],
            "task": args["task"],
            "roles": args["roles"],
            "consensus": bool(args.get("consensus", False)),
        }
        if "rules" in args and args["rules"] is not None:
            kwargs["rules"] = args["rules"]
        return await _wrap("create_team", create_team, args, **kwargs)

    @tool(
        "declare_plan",
        "Declare a plan as a DAG of tasks. Validates structure, persists to disk, "
        "and returns the task list with initial ready/blocked statuses. Does NOT "
        "create a team or spawn any agents — use spawn_agent for that. "
        "Call remove_plan first if you already have an active plan.",
        {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": (
                        "List of task spec objects. Each must include: "
                        "id (unique string), role (display name), skill (skill name), "
                        "task (first message the spawned agent receives). "
                        "Optional: description, model, depends_on (list of task ids)."
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["tasks"],
        },
    )
    async def _declare_plan(args: dict[str, Any]) -> dict[str, Any]:
        return await _wrap(
            "declare_plan",
            declare_plan,
            args,
            orch=orch,
            caller_id=caller_id,
            tasks=args.get("tasks", []),
        )

    @tool(
        "remove_plan",
        "Remove your active plan so you can declare a new one. Renames the plan "
        "file to .removed.json (audit history preserved). Fails with plan_in_use "
        "if any task is still in_flight — approve or force-terminate those agents first.",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def _remove_plan(args: dict[str, Any]) -> dict[str, Any]:
        return await _wrap(
            "remove_plan",
            remove_plan,
            args,
            orch=orch,
            caller_id=caller_id,
        )

    @tool(
        "spawn_agent",
        "Spawn an agent for a ready task in your active plan. The first call "
        "lazily creates the team (team_created=true in the response). Subsequent "
        "calls append members to the same team. The task must be in 'ready' status; "
        "blocked/in_flight/done/failed tasks are rejected.",
        {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task id from your declared plan to spawn an agent for.",
                },
            },
            "required": ["task_id"],
        },
    )
    async def _spawn_agent(args: dict[str, Any]) -> dict[str, Any]:
        return await _wrap(
            "spawn_agent",
            spawn_agent,
            args,
            orch=orch,
            caller_id=caller_id,
            task_id=args["task_id"],
        )

    @tool(
        "list_ready",
        "Return the list of ready task ids in your active plan. Useful after "
        "approving agents to see which tasks have been unblocked.",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def _list_ready(args: dict[str, Any]) -> dict[str, Any]:
        return await _wrap(
            "list_ready",
            list_ready,
            args,
            orch=orch,
            caller_id=caller_id,
        )

    @tool(
        "terminate_child",
        "Post a terminate sentinel to a child agent's inbox. By default, only "
        "valid when the child has called report_status(state='done') (the "
        "approve path). Pass force=true to override; emits an audited "
        "terminate.forced event. Only valid if you lead the team that contains "
        "the target — crossing team boundaries is rejected even for ancestor "
        "leaders.",
        {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Target agent. Must be a member of a team you lead.",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Optional. Default false. When true, bypasses the "
                        "completion-review gate and emits an audited "
                        "terminate.forced event with reason='leader_force'."
                    ),
                },
            },
            "required": ["agent_id"],
        },
    )
    async def _terminate_child(args: dict[str, Any]) -> dict[str, Any]:
        return await _wrap(
            "terminate_child",
            terminate_child,
            args,
            orch=orch,
            caller_id=caller_id,
            agent_id=args["agent_id"],
            force=bool(args.get("force", False)),
        )

    @tool(
        "list_pending_reviews",
        "Return the list of your direct child agents currently awaiting your "
        "approve/rework decision (those that have called "
        "report_status(state='done') and not yet been terminated or sent "
        "rework). Use this if you are unsure which children still need a "
        "decision from you.",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def _list_pending_reviews(args: dict[str, Any]) -> dict[str, Any]:
        async def _call(**kwargs: Any) -> list[dict]:
            return list_pending_reviews(**kwargs)

        return await _wrap(
            "list_pending_reviews",
            _call,
            args,
            orch=orch,
            caller_id=caller_id,
        )

    return create_sdk_mcp_server(
        name="beidou",
        version="1.0.0",
        tools=[
            _send_message,
            _list_peers,
            _ask_user,
            _answer_question,
            _escalate_question,
            _report_status,
            _create_team,
            _declare_plan,
            _remove_plan,
            _spawn_agent,
            _list_ready,
            _terminate_child,
            _list_pending_reviews,
        ],
    )


__all__ = ["build_mcp_server_for"]
