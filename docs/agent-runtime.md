# Agent Runtime Contract

This spec defines what Beidou guarantees to each SDK agent, and what each SDK
agent is required to do. The contract is **soft** on the Python side (the SDK
owns the loop; Beidou cannot force the model's hand) and is enforced by a
combination of a prompt-side contract and an orchestrator-side recovery policy.

## 1. SDK owns the single-agent loop

- Every agent runs under `claude_agent_sdk.query(...)`.
- Beidou does NOT intercept per-LLM-call. There is no `on_llm_call` hook.
  `beidou/agent.py:18-99` (the old manual loop) is deleted.
- Turn-by-turn reasoning, tool dispatch, and message history are the SDK's
  responsibility. Beidou observes by draining the SDK's async message iterator.

## 2. Persistent-agent invariant

**No self-exit.** Completion of assigned work is a *state transition*, not a
process exit.

| Situation | Correct action |
|---|---|
| Work done | Call `report_status(state="done", detail=<summary>)`, then end your turn. The runtime parks you on your inbox until the next message arrives. |
| Nothing to do | End your turn. The runtime keeps your session alive and resumes you when a new inbox message arrives. |
| Re-assigned (parent sent new work) | The new task arrives as the next user-role turn from the runtime. Resume in the same SDK session. No re-spawn. |
| Asked a question by peer | Answer via `send_message`, then end your turn. |

**Why leader-only termination authority:** termination is the single most
disruptive event in the team graph. Centralising it in the leader guarantees
invariants are preserved: leaders know when their sub-team work is complete;
leaves cannot unilaterally exit leaving orphaned children. Beidou has exactly
ONE termination privilege: it can terminate the **root agent** on behalf of
the user. Non-root termination is exclusively leader-driven via
`terminate_child`.

## 3. System prompt structure

The `system_prompt` delivered to each agent is assembled once at spawn time by
`build_system_prompt(skill, spawn_ctx)` in `beidou/skills/loader.py`. It is
the same string on every turn (Anthropic prompt caching applies — full hit
on turn 2+).

**Section order (locked):**

1. `[ASSIGNED SKILL]` — skill body with `{role}` / `{role_description}` /
   `{team_name}` / `{workspace_path}` substituted. Goes first so agents
   sharing the same skill share a cache prefix.
2. `[IDENTITY]` — agent role, team name, workspace path, and leader id.
3. `[PERSISTENT-AGENT CONTRACT]` — verbatim persistent-agent rules.
4. `[OTHER SKILLS]` — note that the `Skill` tool lists other available skills.

The per-turn `prompt=` argument carries only the task (first turn) or
incoming leader/peer messages (subsequent turns). Skill instructions never
appear in the user message.

### Completion reporting

A `PostToolUse` SDK hook fires when an agent calls
`mcp__beidou__report_status(state="done")`. The hook reads the agent's most
recent assistant text from that same turn (bound by `tool_use_id`) and
delivers it to the leader's inbox as a `completion_report` message. This
emits a `completion.reported` event.

**Critical:** agents MUST emit a final summary assistant message **before**
calling `report_status(state="done")`. The hook has no second chance — if the
turn carries no assistant text, a `completion.empty` event is emitted instead
and the leader receives no completion report.

- Hook does NOT fire for the root agent (leader is the user sentinel);
  `completion.empty` with `reason=root_no_leader` is emitted instead.
- Hook is skipped if the `report_status` call itself errored (`is_error=True`).

## 4. Prompt-side contract rules

The `[PERSISTENT-AGENT CONTRACT]` section of the system prompt MUST include
the following constraints:

1. **After every tool call (or completion summary), end your turn.** The
   runtime keeps your session alive and resumes you when a new inbox message
   arrives. You do not need to call any waiting primitive.
2. **When you have no pending work**, end your turn. The runtime will deliver
   the next task as the next user-role turn.

Skill authors MUST NOT override these clauses. They can add role-specific
content but may not weaken the lifecycle contract.

## 5. Orchestrator recovery policy (contract violations)

The SDK hands control back whenever the model emits `stop_reason="end_turn"`.
Beidou cannot prevent that from Python - it can only react.

**Policy: resume-not-terminate.** If `query()` returns for an agent that has
not been terminated by the runtime:

1. Beidou emits a `contract_violation` event. Always. Every time.
2. Beidou keeps the agent alive by resuming the SDK session with a single
   injected user turn:
   > "You ended your turn unexpectedly. End your turn after completing your
   > current work or tool call; the runtime will resume you when new work
   > arrives."
3. On the **Nth consecutive** violation for the same agent (N = 3, see
   `limits.md`), Beidou stops resuming. Instead, it posts a `send_message`
   to the agent's team leader:
   > "agent <id> has violated the no-self-exit contract <N> times. Consider
   > `terminate_child(<id>)`."
4. The leader decides. Beidou does NOT terminate the agent itself, because
   termination authority belongs to the leader.

If the root agent is the violator, escalation goes to Beidou's user gateway
(same mechanism as `ask_user`), because the root has no leader.

## 6. Model-routing caveat

`claude-agent-sdk.query(...)` shells out to the local Claude Code CLI. The
`model=` field passed in `ClaudeAgentOptions` is therefore a **hint to the
local CLI**, not a direct API selector. Actual model execution depends on the
user's local CLI routing configuration. Prototype runs observed
`MiniMax-M2.7` as the reported model on one system. Consequences:

- Do not assume `ClaudeAgentOptions.model` is the ground-truth model string.
- The authoritative model string is what appears on `AssistantMessage.model`
  and `ResultMessage.model_usage` during the drain. Observability records
  that value (see `observability.md`).
- Cost figures in `ResultMessage.total_cost_usd` are computed by the SDK/CLI
  against whatever model actually ran, not necessarily the one requested.

## 7. SDK-runtime mechanics

Beidou runs each agent in streaming-input mode
(`claude_agent_sdk.query(prompt=AsyncIterable[dict])`). The outer loop in
`beidou/sdk_agent.py` yields the initial task, then awaits the agent's
per-agent `asyncio.Queue` between turns. When the orchestrator's
`deliver_message` (or `terminate_child` / `terminate_root`) pushes onto that
queue, the outer loop renders the message as the next user-role input.
Terminate sentinels short-circuit: the outer loop ends the SDK session and
emits `agent_completed` with `terminate_consumed=True`. Agents never see
terminate sentinels.

- The SDK imposes **no per-tool timeout**. Verified in
  `proto_01_long_tool.py`: blocking tool calls at 60s and 180s completed
  cleanly with no SDK-level timeout.
- `ask_user` has no timeout. The runtime parks the agent on the gateway
  response future and resumes it when an answer arrives.
- Beidou does NOT layer its own retry on top of `query()`. Per-call retries
  are delegated to `ClaudeAgentOptions`.
