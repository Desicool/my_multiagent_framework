"""SDK-aware agent spawn loop.

Moved from ``beidou/sdk_agent.py``. This module is the sole producer of events
catalogued in ``docs/observability.md``:

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
import collections
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import ClaudeAgentOptions

from beidou.primitives.core import Message, Orchestrator
from beidou.primitives.mcp import build_mcp_server_for

import re

from beidou.agent.hooks import HookRegistry, V2_DESIGN_COMMITTEE_SKILLS, build_hooks
from beidou.agent.prompts import build_system_prompt

from beidou.skills.loader import (
    LoadedSkill,
    SkillError,
    load_skill,
    load_skill_file,
    sdk_builtins_allowlist,
)

# Hook execution cap for hooks that block on a human gateway round-trip.
# Default in claude-code is 60s, which silently truncates real reviews.
HOOK_REVIEW_TIMEOUT_S: float = 1800.0


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
    max_turns: Optional[int] = None
    max_budget_usd: Optional[float] = None
    resume_session_id: Optional[str] = None


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


_FREEZE_LINE_RE = re.compile(r"^\s*(\[FREEZE (OK|NACK)[^\]]*\][^\n]*)", re.MULTILINE)


def detect_orphaned_freeze(
    assistant_text: Optional[str],
    turn_tool_info: dict,
    leader_id: str,
) -> Optional[tuple[str, str]]:
    """Detect a FREEZE reply that was written in text but never sent via send_message.

    Returns ``(freeze_kind, freeze_content)`` — where ``freeze_kind`` is
    ``"ok"`` or ``"nack"`` and ``freeze_content`` is the full matched line —
    or ``None`` when no orphaned FREEZE is found (either no pattern in text,
    or a matching send_message already went out this turn).

    Parameters
    ----------
    assistant_text:
        The agent's last assistant text for this turn. May be None.
    turn_tool_info:
        Mapping of ``tool_use_id -> (tool_name, tool_input)`` for this turn.
    leader_id:
        The agent's leader id. Used to check whether an existing send_message
        already delivered the FREEZE to the right recipient.
    """
    if not assistant_text:
        return None

    matches = _FREEZE_LINE_RE.findall(assistant_text)
    if not matches:
        return None

    # Use the LAST match (most authoritative).
    last_line, last_kind = matches[-1]
    freeze_content = last_line.strip()
    freeze_kind = "ok" if last_kind.upper() == "OK" else "nack"

    # Check whether a send_message already delivered the FREEZE to the leader.
    for tool_name, tool_input in turn_tool_info.values():
        if tool_name != "mcp__beidou__send_message":
            continue
        if tool_input.get("to") != leader_id:
            continue
        content = tool_input.get("content", "")
        if content.lstrip().startswith(("[FREEZE OK]", "[FREEZE NACK]")):
            return None

    return freeze_kind, freeze_content


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
    "mcp__beidou__answer_question",
    "mcp__beidou__escalate_question",
    "mcp__beidou__report_status",
    "mcp__beidou__signal_review",
    "mcp__beidou__request_termination",
    "mcp__beidou__declare_plan",
    "mcp__beidou__remove_plan",
    "mcp__beidou__spawn_agent",
    "mcp__beidou__list_ready",
    "mcp__beidou__create_team",
    "mcp__beidou__terminate_child",
    "mcp__beidou__list_pending_reviews",
]


def _build_options(
    *,
    system_prompt: str,
    mcp_server,
    allowed_tools: list[str],
    model: Optional[str],
    cwd: Optional[str],
    hooks: Optional[dict] = None,
    max_turns: Optional[int] = None,
    max_budget_usd: Optional[float] = None,
    stderr: Any = None,
    resume: Optional[str] = None,
    session_id: Optional[str] = None,
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
        # Three layers of defense against SDK team-mode tool shadowing.
        #
        # Layer 1 — env scrub: clear CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS in
        # the SDK subprocess env. SDK transport merges ``options.env`` on top
        # of inherited process env, so an empty value here overrides whatever
        # the user's profile (e.g. ~/profiles/beidou_minimax.sh) set globally.
        # The CLI binary only enables its experimental team-mode tool surface
        # when this var is truthy, so an empty value disables it. Beidou
        # implements its own team primitives via mcp__beidou__* — it does
        # not, and must not, depend on the SDK's experimental team mode.
        # This kills future SDK team tools too (Spawn, Task, etc. as they
        # ship), not just SendMessage.
        env={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": ""},
        # Layer 2 — disallowed_tools: even if the env scrub leaks (e.g. CLI
        # ignores the var, or a future flag re-enables team mode), the SDK
        # respects this list. Tools listed here are SDK builtins that compete
        # with Beidou's primitives or default-on tools we never want surfaced:
        #   - SendMessage: SDK team-mode messaging shadow of mcp__beidou__send_message.
        #   - TodoWrite: competes with mcp__beidou__report_status as a completion
        #     signal. Per tsk_658f44b6 the impl-leader called TodoWrite to mark
        #     its tasks complete in lieu of report_status, hanging the run.
        #     Beidou tracks tasks via bd (cross-session) and report_status
        #     (in-conversation); TodoWrite has no role.
        # Expand if new shadow-tools surface in events. See bd issue qi0v /
        # commit 3037709 (prompt-level NEVER DOs), commit 81bde03 (SendMessage),
        # and bd issue 8gen / this commit (TodoWrite).
        disallowed_tools=["SendMessage", "TodoWrite"],
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
    if max_turns is not None:
        kwargs["max_turns"] = max_turns
    if max_budget_usd is not None:
        kwargs["max_budget_usd"] = max_budget_usd
    if stderr is not None:
        kwargs["stderr"] = stderr
    if resume is not None:
        kwargs["resume"] = resume
        kwargs["fork_session"] = False
    if session_id is not None:
        kwargs["session_id"] = session_id
    return ClaudeAgentOptions(**kwargs)


# ---------------------------------------------------------------------------
# Helper: extract error text from a ToolResultBlock.
# ---------------------------------------------------------------------------

_ERROR_REASON_MAX = 2000


def _extract_error_reason(block: object) -> "str | None":
    """Extract a text reason from a ToolResultBlock when is_error=True.

    Joins text fragments from ``block.content`` (which may be a string, a list
    of dicts like {"type":"text","text":"..."}, or a list of objects with a
    ``.text`` attribute). Returns None if nothing extractable. Truncates to
    _ERROR_REASON_MAX chars with a "...(truncated)" suffix.
    """
    content = getattr(block, "content", None)
    if content is None:
        return None
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
            else:
                t = getattr(item, "text", None)
                if isinstance(t, str):
                    parts.append(t)
    if not parts:
        return None
    text = "".join(parts)
    if not text:
        return None
    if len(text) > _ERROR_REASON_MAX:
        text = text[:_ERROR_REASON_MAX] + "…(truncated)"
    return text


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

    # Discover and load skill module hooks (module.toml + gate.py / eval.py).
    skill_dir = skill.source_path.parent
    _hook_registry: HookRegistry | None = None
    if HookRegistry.has_modules(skill_dir):
        _hook_registry = HookRegistry(
            skill_dir=skill_dir,
            orch=orch,
            caller_id=spec.caller_id,
            leader_id=template_vars.get("leader_id", ""),
        )

    mcp = build_mcp_server_for(orch, caller_id=spec.caller_id)

    # Resolve allowed_tools. If an explicit override was supplied in the spec,
    # use it verbatim (test / advanced caller path). Otherwise compose from:
    #   1. The 8 Beidou MCP primitives, filtered by the skill's raw allowed-tools
    #      to preserve the per-skill primitive restriction contract.
    #   2. The SDK built-ins (Bash, Read, Write, etc.) derived from the skill's
    #      allowed-tools via sdk_builtins_allowlist().
    # Note: skills="all" auto-adds the Skill tool -- we do NOT add it here.
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
    if _hook_registry is not None:
        hooks = _hook_registry.merge_with_builtins(hooks)

    # Pre-generate session_id for crash-recovery tracking.
    # Stored on _drain_rec BEFORE any SDK call so the orchestrator can
    # read it even if the process crashes before the first ResultMessage.
    _drain_rec = orch._agents.get(spec.caller_id)  # type: ignore[attr-defined]
    session_id = str(uuid.uuid4())
    if _drain_rec is not None:
        _drain_rec.last_session_id = session_id

    # Stderr capture buffer -- the ONLY reliable path for capturing CLI
    # subprocess error output (ProcessError attributes are lost in SDK
    # serialization; see query.py:308).
    stderr_buf: collections.deque[str] = collections.deque(maxlen=200)

    def _on_stderr(line: str) -> None:
        # Normalize: strip trailing whitespace per line but preserve
        # empty lines so crash traces are readable.
        stderr_buf.append(line.rstrip())

    options = _build_options(
        system_prompt=rendered_prompt,
        mcp_server=mcp,
        allowed_tools=allowed_tools,
        model=spec.model,
        cwd=spec.cwd,
        hooks=hooks,
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        stderr=_on_stderr,
        resume=spec.resume_session_id,
        session_id=session_id,
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
            "session_id": session_id,
            "ts": time.time(),
        },
    )

    # Run before_agent_start eval handlers from skill modules.
    if _hook_registry is not None:
        from beidou.agent.context import AgentStartContext
        agent_start_ctx = AgentStartContext(
            agent_id=spec.caller_id,
            skill_name=skill.name,
            team_id=orch.agent_team(spec.caller_id) if hasattr(orch, "agent_team") else None,
            leader_id=template_vars.get("leader_id", ""),
            cwd=spec.cwd,
            emit=lambda n, p: orch.emit_event(n, {"caller_id": spec.caller_id, **p}),
        )
        await _hook_registry.run_eval_hook("before_agent_start", agent_start_ctx)

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
    # Agents already nudged about orphaned FREEZE replies (one-shot per agent).
    _freeze_nudged_agents: set[str] = set()

    # Build the streaming-input generator that parks the agent between turns
    # on its per-agent queue. The generator owns terminate detection and sets
    # terminate_consumed on the AgentRecord before returning (closing the SDK
    # session). The agent never sees a "wait" or "receive" tool.
    # queue_for() is the canonical path on the real Orchestrator. Fall back to
    # an empty Queue when the orchestrator under test doesn't expose it (e.g.
    # FakeOrchestrator in mechanical tests where query() is monkeypatched and
    # the generator is never consumed).
    _queue_for = getattr(orch, "queue_for", None)
    queue: asyncio.Queue = _queue_for(spec.caller_id) if _queue_for is not None else asyncio.Queue()

    async def input_stream():
        # Yield the initial task as the first user-role message.
        # Emit agent_input BEFORE yielding so the event lands even if the SDK
        # errors after the yield.
        orch.emit_event(
            "agent_input",
            {
                "ts": time.time(),
                "caller_id": spec.caller_id,
                "from": "user",
                "message_kind": "initial",
                "source": "initial",
                "content": spec.task,
                "message_id": f"{spec.caller_id}:initial",
            },
        )
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
                    # Clear any pending reply obligations before returning.
                    if rec.pending_replies:
                        for msg_id, obl in rec.pending_replies.items():
                            orch.emit_event("reply.abandoned", {
                                "agent_id": spec.caller_id,
                                "message_id": msg_id,
                                "from_id": obl["from_id"],
                                "ts": time.time(),
                            })
                        rec.pending_replies.clear()
                        rec.reply_gate_active = False
                    rec.terminate_consumed = True
                return  # Closing the generator ends the SDK session.
            # Emit agent_input BEFORE yielding (origin time, not consume time).
            # terminate sentinels do NOT produce agent_input -- the early return
            # above handles them without reaching here.
            orch.emit_event(
                "agent_input",
                {
                    "ts": msg_in.ts,
                    "caller_id": spec.caller_id,
                    "from": msg_in.from_id,
                    "message_kind": msg_in.kind,
                    "source": "queue",
                    "content": msg_in.content,
                    "message_id": msg_in.message_id,
                },
            )
            # Register inquiry obligations on the AgentRecord.
            if msg_in.kind == "inquiry":
                rec = orch._agents.get(spec.caller_id)  # type: ignore[attr-defined]
                if rec is not None:
                    priority = getattr(msg_in, "priority", "normal")
                    rec.pending_replies[msg_in.message_id] = {
                        "from_id": msg_in.from_id,
                        "ts": msg_in.ts,
                        "priority": priority,
                    }
                    if priority == "high":
                        rec.reply_gate_active = True
            # Render peer messages as user-role input to the next turn.
            if msg_in.kind == "inquiry":
                content = f"[INQUIRY from {msg_in.from_id} — reply expected] {msg_in.content}"
            else:
                content = f"[from {msg_in.from_id}] {msg_in.content}"
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
                "session_id": session_id,
            }

    # Resolve query lazily through beidou.sdk_agent so tests that
    # monkeypatch sdk_agent.query can still intercept the call.
    from beidou import sdk_agent as _sdk
    _query = _sdk.query

    try:
        async for msg in _query(prompt=input_stream(), options=options):
            cls = type(msg).__name__

            if cls == "AssistantMessage":
                mid = getattr(msg, "message_id", None)
                usage = getattr(msg, "usage", None)
                model_str = getattr(msg, "model", None)
                stop_reason = getattr(msg, "stop_reason", None)

                # Dedup turn.usage by message_id. Multiple AssistantMessage
                # fragments share the same message_id + usage payload
                # (verified during the SDK-behaviour probe; see
                # docs/observability.md#turn.usage).
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
                        is_err = bool(getattr(block, "is_error", False))
                        error_reason = _extract_error_reason(block) if is_err else None
                        orch.emit_event(
                            "tool_result",
                            {
                                "ts": time.time(),
                                "caller_id": spec.caller_id,
                                "tool_use_id": tool_use_id,
                                "duration_ms": duration_ms,
                                "is_error": is_err,
                                "error_reason": error_reason,
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
                drain_ts = time.time()
                # Update session_id from ResultMessage -- the SDK may have
                # assigned a different one (e.g. on resume).
                rmid = getattr(msg, "session_id", None)
                if rmid is not None:
                    session_id = rmid
                    if _drain_rec is not None:
                        _drain_rec.last_session_id = rmid
                result_data = {
                    "total_cost_usd": getattr(msg, "total_cost_usd", 0.0) or 0.0,
                    "usage": dict(getattr(msg, "usage", {}) or {}),
                    "model_usage": dict(getattr(msg, "model_usage", {}) or {}),
                    "duration_ms": getattr(msg, "duration_ms", 0) or 0,
                    "duration_api_ms": getattr(msg, "duration_api_ms", 0) or 0,
                    "num_turns": getattr(msg, "num_turns", 0) or 0,
                    "stop_reason": getattr(msg, "stop_reason", "unknown") or "unknown",
                    "session_id": rmid,
                }
                if _drain_rec is not None:
                    _drain_rec.last_drain_ts = drain_ts
                orch.emit_event(
                    "run.cost",
                    {
                        "caller_id": spec.caller_id,
                        "ts": drain_ts,
                        **result_data,
                    },
                )

                # --- REPLY OBLIGATION CHECK ---
                # Fires after EVERY turn. Checks pending_replies and activates the
                # reply gate if the agent didn't fulfill all reply obligations this turn.
                # The PostToolUse hook (on_send_message_reply) already clears fulfilled
                # obligations during the turn. This check catches anything the hook missed
                # (e.g., text-only turns, or if the hook's matcher didn't trigger).
                # We re-scan _turn_tool_info here for defense-in-depth.
                if _drain_rec is not None and getattr(_drain_rec, "pending_replies", None):
                    for tool_id, (tool_name, tool_input) in _turn_tool_info.items():
                        if tool_name == "mcp__beidou__send_message":
                            to = tool_input.get("to", "")
                            # FIFO: oldest matching obligation
                            oldest_msg_id = None
                            oldest_ts_val = None
                            for msg_id, obl in _drain_rec.pending_replies.items():
                                if obl["from_id"] == to:
                                    if oldest_ts_val is None or obl["ts"] < oldest_ts_val:
                                        oldest_ts_val = obl["ts"]
                                        oldest_msg_id = msg_id
                            if oldest_msg_id:
                                del _drain_rec.pending_replies[oldest_msg_id]

                    if _drain_rec.pending_replies:
                        # Idempotency guard: don't inject duplicate nudges.
                        if not _drain_rec.reply_gate_active:
                            still_pending = list(_drain_rec.pending_replies.values())
                            from_ids = list(dict.fromkeys(o["from_id"] for o in still_pending))
                            nudge = (
                                f"[REPLY REQUIRED] You have unreplied inquiries from: {', '.join(from_ids)}.\n"
                                f"Call mcp__beidou__send_message(to=<agent>, content='...') to reply "
                                f"before taking other actions. The reply gate will block every other "
                                f"tool until each pending sender has been answered. "
                                f"Spec: docs/agent-runtime.md#reply-obligation, "
                                f"docs/tool-surface.md#send_message."
                            )
                            orch.deliver_message(
                                from_id="beidou",
                                to_id=spec.caller_id,
                                body=nudge,
                                kind="nudge",
                            )
                            _drain_rec.reply_gate_active = True
                    else:
                        # All obligations cleared this turn.
                        _drain_rec.reply_gate_active = False

                # --- ORPHANED FREEZE NUDGE GATE ---
                # Catches the failure mode where a v2 design-committee agent
                # wrote "[FREEZE OK]" or "[FREEZE NACK]" in assistant text but
                # did not deliver via send_message (used report_status, plain
                # text, or send_message to the wrong target).
                #
                # Earlier behaviour synthesised the delivery to the leader
                # (loop impersonating the agent). That is exactly the bug
                # class fixed by 82d5290: when the regex misfires, the leader
                # acts on a degraded text copy. Now we nudge the agent itself
                # to use send_message; the loop never delivers anything to
                # the leader on the agent's behalf. See docs/coding-v2.md §5.
                rec = orch._agents.get(spec.caller_id)  # type: ignore[attr-defined]
                if (
                    rec is not None
                    and getattr(rec, "skill_name", None) in V2_DESIGN_COMMITTEE_SKILLS
                    and spec.caller_id not in _freeze_nudged_agents
                ):
                    _most_recent = getattr(orch, "_most_recent_assistant_text", {})
                    _asst_text = _most_recent.get(spec.caller_id)
                    _freeze_result = detect_orphaned_freeze(_asst_text, _turn_tool_info, leader_id)
                    if _freeze_result is not None:
                        _freeze_kind, _freeze_content = _freeze_result
                        nudge = (
                            f"You wrote {_freeze_content!r} in your assistant "
                            f"text but did not deliver it via send_message — "
                            f"the leader received nothing. Your VERY NEXT "
                            f"turn must call:\n"
                            f"  mcp__beidou__send_message("
                            f"to=\"{leader_id}\", "
                            f"content=\"[FREEZE OK]\")\n"
                            f"  # or content=\"[FREEZE NACK]: <reason>\"\n"
                            f"Spec: docs/coding-v2.md §5 (Convergence and "
                            f"freeze probe)."
                        )
                        orch.deliver_message(
                            from_id="beidou",
                            to_id=spec.caller_id,
                            body=nudge,
                            kind="nudge",
                        )
                        _freeze_nudged_agents.add(spec.caller_id)
                        orch.emit_event(
                            "freeze.nudge_injected",
                            {
                                "caller_id": spec.caller_id,
                                "leader_id": leader_id,
                                "freeze_kind": _freeze_kind,
                                "ts": time.time(),
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
        # Capture stderr from the buffer -- the ONLY reliable path
        # (ProcessError attributes are lost in SDK serialization).
        stderr_text = "\n".join(stderr_buf)
        if _drain_rec is not None:
            _drain_rec.last_crash_stderr = stderr_text
        orch.emit_event(
            "agent_error",
            {
                "caller_id": spec.caller_id,
                "exception": type(exc).__name__,
                "msg": str(exc),
                "error": str(exc),
                "stderr": stderr_text,
                "ts": time.time(),
            },
        )
        orch.emit_event(
            "agent_completed",
            {
                "caller_id": spec.caller_id,
                "terminated": False,
                "stop_reason": "crashed",
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


__all__ = ["SpawnSpec", "RunResult", "run_agent", "_resolve_skill"]
