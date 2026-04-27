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

The persistent-agent contract applies to **every** agent in the system — root
or otherwise, solo or team member. Being the root agent, or being a teamless
agent, does not relax any clause of this contract.

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

### Delegation is a right, not a role

Any agent MAY call `create_team` if its skill exposes the tool, regardless of
whether that agent was spawned as a "worker" or an "orchestrator". Leadership
is acquired by spawning — the moment an agent calls `create_team`, it becomes
the leader of that team. Leadership is never pre-assigned by skill or role
label.

The root agent has the same right: it may proceed solo, or it may call
`create_team` to spawn a team and immediately become that team's leader. The
only invariant is the self-lead rule (see `orchestration.md`).

## 2.5 Disambiguation duty

Every agent role-skill (orchestrator, product_manager, software_architect,
junior_engineer, test_engineer, deployment_engineer, qa_engineer) is bound by
the same rule: do not silently resolve a user-relevant ambiguity. If the task
or upstream artifact leaves a binding choice unspecified — framework variant,
build chain, language version, persistence layer, auth model, deployment
target, file layout, test framework, or any decision the user could plausibly
care about — the agent MUST escalate via `ask_user` (leaders) or via
`send_message` to the leader chain (members) and BLOCK on the answer. Writing
an artifact (`requirements.md`, `SPEC.md`, `tasks.md`, code, deploy plan, qa
verdict) that bakes in an unverified assumption on a binding choice is a
contract violation.

The orchestrator routes member-originated `ask_user` escalations to the human
user via its own `ask_user`. It never answers on the user's behalf.

## 3. System prompt structure

The `system_prompt` delivered to each agent is assembled once at spawn time by
`build_system_prompt(skill, spawn_ctx)` in `beidou/skills/loader.py`. It is
the same string on every turn (Anthropic prompt caching applies — full hit
on turn 2+).

**Section order (locked):**

1. `[ASSIGNED SKILL]` — skill body with `{role}` / `{role_description}` /
   `{team_name}` / `{workspace_path}` / `{project_workspace_path}` substituted.
   Goes first so agents sharing the same skill share a cache prefix.
2. `[IDENTITY]` — agent role, team name (if any), workspace path, project
   workspace path, and leader id. For the root agent, the agent's cwd is the
   project workspace, and `{workspace_path}` refers to an agent-scoped scratch
   directory (`{project}/.beidou/tasks/{task_id}/agents/{agent_id}/`) used for
   orchestrator-internal storage. The root agent has no team, so `{team_name}`
   is omitted or labelled accordingly.
3. `[PERSISTENT-AGENT CONTRACT]` — verbatim persistent-agent rules.
4. `[COMPLETION HANDOFF CONTRACT]` — verbatim completion handoff rules,
   including the nudge fallback if a summary is missing.
5. `[OTHER SKILLS]` — note that the `Skill` tool lists other available skills.

The per-turn `prompt=` argument carries only the task (first turn) or
incoming leader/peer messages (subsequent turns). Skill instructions never
appear in the user message.

### Completion reporting

Calling `report_status(state="done")` is a **request for review**, not a
self-declaration. The agent never marks itself done; only the leader's
`terminate_child` (approve) or `send_message` (rework) closes the loop. The
agent remains alive and parked on its inbox until the leader acts.

**Review envelope.** Agents are required (by the prompt-side rule in
`beidou/skills/coding/orchestrator/SKILL.md`, §"Reviewing a child's
completion request") to embed a structured envelope in the final assistant
message of the reporting turn:

```
[REVIEW REQUIRED]
role=<skill name>     agent=<id>
Deliverables: <list>
Open questions / risks: <one line, or "none">
Leader action required: approve (terminate_child) OR rework (send_message)
```

A `PostToolUse` SDK hook fires when an agent calls
`mcp__beidou__report_status(state="done")` (`beidou/sdk_agent.py::on_report_status`).
The hook reads the agent's most recent assistant text from that same turn
(bound by `tool_use_id`) and delivers it to the leader's inbox as a
`completion_report` message. This emits a `completion.reported` event.

On the `AgentRecord`, the hook sets `completion_pending=True` and stamps
`completion_pending_ts` with the current time (`beidou/orchestrator.py`,
`AgentRecord` fields). The pending flag is cleared when:

- The agent's direct leader (or the human user gateway) delivers any
  non-system message to the agent → `completion.rework` event emitted
  (`beidou/orchestrator.py::inbox_put`).
- `terminate_child` fires for that agent → `completion.approved` event
  emitted (`beidou/orchestrator.py::inbox_put`).

**Defensive envelope synthesis.** If the child's body does not contain
`[REVIEW REQUIRED]` (case-insensitive check), the hook synthesizes the
envelope header and prepends it to the body before delivery, so the leader
always receives the unmissable signal even when the model drifts from the
prompt rule. Each synthesis emits a `completion.envelope_synthesized` event
(count these in observability to measure prompt-side rule drift).

**Detail fallback:** if the assistant text is empty, the hook reads the
`detail` parameter from the `report_status` tool call input. If `detail` is
present and non-empty, it is used as the completion report body. This covers
model providers that cannot emit a preceding text message after a tool call.

**Harness nudge checkpoint:** after all tool results in a given drain cycle
are processed, a checkpoint runs. If the agent has called
`report_status(state="done")` but both the assistant text AND `detail` are
empty or missing, the runtime injects a nudge message into the agent's inbox:

> "You called report_status(state=\"done\") without a summary. Please emit a
> final assistant message summarizing your work, then call
> report_status(state=\"done\", detail=\"...\") again."

The nudge is injected **at most once per agent session** to prevent loops.
When a nudge is injected, a `completion.nudged` event is emitted.

The plain `terminate_child(agent_id)` call is the leader's APPROVE verdict
on the child's `report_status(state="done")` request. If the child has not
reported done, the call fails with `child_not_pending_review`. The leader
may pass `force=true` to override (rare, audited via `terminate.forced`
with `reason="leader_force"`). The watchdog terminate-grace cancel (§3.1)
is the runtime backstop for genuinely livelocked children and emits the
same event with `reason="watchdog_grace"`.

The completion handoff rules are also present in the system prompt as the
`[COMPLETION HANDOFF CONTRACT]` block, appended after the persistent-agent
contract (see section order above). The leader-side obligation is in the
orchestrator skill's "Reviewing a child's completion request" section —
that is the prompt-side half of the contract.

- For the root agent (leader is `USER_SENTINEL`), the same hook fires and
  performs the same envelope synthesis, then routes the review through the
  human gateway via `Orchestrator.gateway_ask_user_structured`. The user is
  presented with **Approve** and **Rework** options:
  - **Approve** → `Orchestrator.terminate_root()` is awaited; the run unwinds
    via the existing terminate-sentinel path. A `completion.reported` event
    with `via=user_gateway, decision=approve` is emitted.
  - **Rework** → a `from_id="user"` rework message is delivered to the root's
    inbox (body prefixed with `rework: `) so the next turn can continue.
    `completion.reported` with `via=user_gateway, decision=rework` is emitted.
  If the gateway round-trip raises, the hook falls back to
  `completion.empty(reason="gateway_failure: <ExcType>")` and returns so the
  tool call does not deadlock. The hook execution timeout for both
  `AskUserQuestion` (PreToolUse) and `mcp__beidou__report_status` (PostToolUse)
  is `HOOK_REVIEW_TIMEOUT_S = 1800.0` (30 minutes), set in
  `beidou/sdk_agent.py`; this overrides claude-code's 60s default so a real
  human review is not silently truncated.
- Hook is skipped if the `report_status` call itself errored (`is_error=True`).

## 3.1 Liveness watchdog

A background `asyncio.Task` (`beidou-watchdog`) is started lazily on the
first agent registration (`Orchestrator._register_agent_record`). It runs
alongside the agent SDK loops for the lifetime of the orchestrator and is
cancelled cleanly by `Orchestrator.stop_watchdog()`. The watchdog constants
below are in-code implementation values; they are **not** entries in
`docs/limits.md`.

| Constant | Value | Location |
|---|---|---|
| `WATCHDOG_INTERVAL_S` | 30 s | `beidou/orchestrator.py` |
| `REVIEW_PING_INTERVAL_S` | 60 s | `beidou/orchestrator.py` |
| `IDLE_NUDGE_S` | 120 s | `beidou/orchestrator.py` |
| `MAX_PINGS_BEFORE_ESCALATION` | 3 | `beidou/orchestrator.py` |

Each tick calls `_watchdog_tick()`. Any exception inside that method is
caught, emitted as `watchdog.exception` (truncated traceback), and the
watchdog continues — it never crashes the orchestrator.

### Pass A — review-pending escalation

For every `AgentRecord` with `completion_pending=True`, let
Δt = now − `completion_pending_ts`. The escalation ladder:

| Δt | `review_ping_count` before action | Action |
|---|---|---|
| < 60 s | any | nothing |
| ≥ 60 s | 0 | ping #1: deliver directive message to leader — "child X awaiting your decision; call `terminate_child` or `send_message` NOW." `review_ping_count++`; reset `completion_pending_ts` to now (so next threshold counts from this ping). Emits `completion.reping`. |
| ≥ 60 s | 1 | ping #2: same body + warn "If you do not act, this will escalate to the user gateway in ~60s." `review_ping_count++`; reset `completion_pending_ts`. Emits `completion.reping`. |
| ≥ 60 s | 2 | escalate: emit `review.escalated_to_user`; if a gateway is registered, call `gateway_ask_user` (best-effort). `review_ping_count++`. |
| any | ≥ 3 | silent — user owns it; watchdog stops acting on this agent. |

Each ping resets `completion_pending_ts` so the countdown restarts from the
previous ping, not from the original report. The pings are delivered to the
**leader** via `deliver_message(from_id="beidou", kind="ping")`.

Implementation: `beidou/orchestrator.py::_watchdog_tick` (Pass A loop).

### Pass B — general liveness nudge

For every `AgentRecord` with `last_progress_ts` older than `IDLE_NUDGE_S`
(120 s), the watchdog sends a structured directive nudge, **unless**:

- `inflight_tools > 0` — a tool call is currently running; no false alarm.
- `completion_pending=True` — Pass A is already handling this agent.
- The agent has no direct children AND is not the root agent — pure workers
  parked on their inbox are the normal steady state; nudging them is noise.
- `idle_nudge_count >= MAX_PINGS_BEFORE_ESCALATION` — already escalated.

The nudge body names four concrete actions the agent can take (call
`terminate_child`/`send_message` on a pending child, call `create_team` for
the next phase, call `report_status(done)` if finished, or call `ask_user`
if blocked). After the nudge is delivered, `last_progress_ts` is reset to
now so the next threshold counts from this ping.

Same 3-strike escalation as Pass A: nudge #1, nudge #2 (with escalation
warning), then `gateway_ask_user` (best-effort). Events: `liveness.nudge`
(every fired nudge) and `liveness.escalated_to_user` (on escalation).

### In-flight tracking

`AgentRecord.inflight_tools` is incremented by the drain loop when a
`ToolUseBlock` arrives, and decremented when the matching `ToolResultBlock`
arrives (`beidou/sdk_agent.py`, drain loop). `AgentRecord.last_progress_ts`
is bumped on every tool start, every tool end, and every inbox arrival
(`beidou/orchestrator.py::inbox_put`). These two fields are the Pass B
eligibility criteria.

### Leader-side review gate (PreToolUse interceptor)

A leader agent with any direct child whose `completion_pending=True` is
blocked from calling any tool except the following allowlist
(`beidou/sdk_agent.py::on_review_gate`):

```
Read, Glob, Grep, Bash,
mcp__beidou__terminate_child,
mcp__beidou__send_message,
mcp__beidou__list_pending_reviews,
mcp__beidou__report_status,
mcp__beidou__ask_user
```

Any other tool call is denied with a directive message naming the pending
child and the two valid resolution actions. Each denial emits
`review_gate.denied`.

### New events summary

| Event | Emitted by | Meaning |
|---|---|---|
| `completion.envelope_synthesized` | `sdk_agent.py::on_report_status` | Hook synthesized the `[REVIEW REQUIRED]` envelope because the child failed to embed one. |
| `completion.approved` | `orchestrator.py::inbox_put` | `terminate_child` fired for a `completion_pending` agent; review accepted. |
| `completion.rework` | `orchestrator.py::inbox_put` | Leader or user delivered a message to a `completion_pending` agent; review returned for rework. |
| `completion.reping` | `orchestrator.py::_watchdog_tick` | Watchdog pinged the leader again (Pass A). |
| `review.escalated_to_user` | `orchestrator.py::_watchdog_tick` | Leader failed to act after 3 pings; escalated to user gateway. |
| `liveness.nudge` | `orchestrator.py::_watchdog_tick` | Idle agent nudged (Pass B). |
| `liveness.escalated_to_user` | `orchestrator.py::_watchdog_tick` | Idle agent nudged 3× without progress; escalated to user gateway. |
| `review_gate.denied` | `sdk_agent.py::on_review_gate` | Leader tried to call a non-allowlisted tool while a child review is pending. |
| `watchdog.exception` | `orchestrator.py::_watchdog_loop` | Exception inside `_watchdog_tick`; watchdog continues. |

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

**Note on scope.** Beidou runs each agent in streaming-input mode: the SDK
iterator parks on `queue.get()` between turns and never exits during normal
operation. As a result, this contract-violation policy (which fires only when
the SDK iterator fully exits) handles only the case where the model ends its
SDK session unexpectedly. It cannot see a model that is *alive but idle*
inside a parked iterator. The liveness watchdog (§3.1) covers that mid-stream
idle case.

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
- **SDK-builtin `AskUserQuestion` passthrough.** When a model emits the
  SDK-builtin `AskUserQuestion` tool call, the Beidou hook
  (`on_ask_user_question` in `sdk_agent.py`) forwards the structured
  `questions` array unchanged to `gateway_ask_user_structured`. The hook
  does NOT flatten sub-questions into a composite text prompt — the gateway
  and UI receive the same wire shape Claude Code uses natively. Both the
  SDK-builtin path and the MCP `mcp__beidou__ask_user` path produce the
  same enriched `Question` object via `QuestionBroker.ask(ctx, questions,
  ...)`, so escalation, persistence, and `question_*` events are identical
  regardless of which entry point the model uses.
- Beidou does NOT layer its own retry on top of `query()`. Per-call retries
  are delegated to `ClaudeAgentOptions`.
- The liveness watchdog (§3.1) is a separate `asyncio.Task` (`beidou-watchdog`)
  running alongside the agent SDK loops; it is not part of any single agent's
  lifecycle and imposes no per-agent or per-boundary constraint.
