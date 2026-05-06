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
| Work done | Call `signal_review(detail=<summary>)`, then end your turn. The runtime parks you on your inbox until the next message arrives. |
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

Any agent MAY call `declare_plan` + `spawn_agent` (or the deprecated
`create_team`) if its skill exposes those tools, regardless of whether that
agent was spawned as a "worker" or an "orchestrator". Leadership is acquired by
spawning — the moment an agent calls `spawn_agent` (or `create_team`), it
becomes the leader of that team. Leadership is never pre-assigned by skill or
role label.

The root agent has the same right: it may proceed solo, or it may declare a
plan and spawn a team, immediately becoming that team's leader. The only
invariant is the self-lead rule (see `orchestration.md`).

## 2.5 Disambiguation duty

Every agent role-skill (orchestrator, product_manager, software_architect,
junior_engineer, test_engineer, deployment_engineer, qa_engineer) is bound by
the same rule: do not silently resolve a user-relevant ambiguity. If the task
or upstream artifact leaves a binding choice unspecified — framework variant,
build chain, language version, persistence layer, auth model, deployment
target, file layout, test framework, or any decision the user could plausibly
care about — the agent MUST escalate via `ask_user` and BLOCK on the answer.
Writing an artifact (`requirements.md`, `SPEC.md`, `tasks.md`, code, deploy
plan, qa verdict) that bakes in an unverified assumption on a binding choice
is a contract violation.

**Question chain.** Agent-originated `ask_user` is routed through the leader
chain. The asker's question first lands in the asker's team leader's inbox
as a `[INBOX QUESTION]` system message; the leader can resolve it directly
via `mcp__beidou__answer_question` (when the leader already knows the answer
from the user task, an upstream artifact, or a prior user reply) or push it
one hop further with `mcp__beidou__escalate_question`. Each escalation walks
one team-nesting level toward the root. When the next hop is the user
sentinel, the question surfaces to the human gateway. This makes "the leader
already had this answer in `requirements.md`" a single quick reply instead
of a fresh user prompt, while the user remains the terminal authority for
genuinely-unresolved binding choices.

**Bubble model.** `escalate_question` is fire-and-forget: the escalator
dispatches the question one hop up and its tool call returns immediately.
The answer is delivered only to the original asker (via its `ask_user`
future) — intermediate escalators do NOT receive or process the answer.
Nothing in the system awaits on behalf of an escalator. The watchdog's
Pass B suppresses liveness nudges for intermediate-hop agents
(`QuestionRegistry.has_pending_through`) so they are not prompted to
re-ask questions they have already forwarded.

**Implementation.** Question routing lives in `beidou/orchestrator.py`
(`post_question`, `forward_question`, `_deliver_question`, `resolve_question`)
backed by `beidou/questions.py` (`QuestionRegistry`, `PendingQuestion`).
The `QuestionBroker` class (formerly `beidou/inbox.py`) no longer exists.

The orchestrator (the typical chain terminator before the user) never
answers on the user's behalf for genuinely-unresolved choices; it
escalates to the user via `escalate_question`. System-originated paths
(watchdog ping escalations, root completion review) bypass the chain and
surface to the gateway directly via `gateway_ask_user_structured`.

## 3. System prompt structure

The `system_prompt` delivered to each agent is assembled once at spawn time by
`build_system_prompt(skill, spawn_ctx)` in `beidou/agent/prompts.py`. It is
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

**First-user-message contract.** Every spawned agent's first user message
depends on how it was spawned:

- **Root agent**: always receives the originating user task, captured once at
  `Orchestrator.run_root(root_task=...)` time as `Orchestrator._user_task`.
- **Members spawned via `create_team` (legacy path)**: receive the originating
  user task (`Orchestrator._user_task`) propagated unchanged. The team-level
  `task` argument passed to `create_team(task=...)` is recorded on the
  `TeamRecord` for orchestrator-internal coordination but is **not** the
  agent's first user message.
- **Members spawned via `spawn_agent` (new plan flow)**: receive the per-task
  `task` text from their `declare_plan` entry as their first user message. They
  do **not** receive the originating user task auto-prepended. Leaders are
  responsible for making each `task` field self-contained, including any context
  the worker needs to ground its work. Before the task text, the runtime
  **prepends** a `[TASK ASSIGNMENT]...[/TASK ASSIGNMENT]` header containing
  `plan_task_id` (the task's id from `declare_plan`) and `artifacts_path`
  (`{project_workspace_path}/artifacts/{plan_task_id}`). This header is injected
  into the user message — not the system prompt — to preserve KV cache on the
  skill body. `create_team`-born agents do not receive this header.

> **Leader skills MUST treat each `task` field as self-contained because the
> spawned agent will not see the original user request — only what the leader
> wrote.**

**`{role_description}` carries only the role-specific scope.** For
`create_team`-born members, `{role_description}` is substituted with
`roles[i].description` from the `create_team` call (e.g. "Write
requirements.md to the team workspace"). For `spawn_agent`-born members,
`{role_description}` is substituted with the per-task `description` field from
the plan entry (defaults to `""`). The root agent has no role-specific scope
(its scope IS the user task), so its `{role_description}` substitutes to the
empty string. Worker skills that surface the placeholder in their body get a
clean role-description section; the user task arrives separately as a user
message and is never duplicated into the system prompt.

### Completion reporting

Calling `signal_review(detail=...)` is a **request for review**, not a
self-declaration. The agent never marks itself done; only the leader's
`terminate_child` (approve) or `send_message` (rework) closes the loop. The
agent remains alive and parked on its inbox until the leader acts.

**Review envelope.** The `detail` argument to `signal_review(detail=...)`
MUST contain a structured envelope. The primitive rejects calls where `detail`
is missing or does not contain `[REVIEW REQUIRED]` (case-insensitive) with an
`envelope_missing` tool error; the agent sees the error in its next tool result
and retries with the corrected call. The required envelope format is:

```
[REVIEW REQUIRED]
role=<skill name>     agent=<id>
Deliverables: <list>
Open questions / risks: <one line, or "none">
Leader action required: approve (terminate_child) OR rework (send_message)
```

`detail` is the canonical envelope source — there is no fallback to assistant
text. The prompt-side rule in `[COMPLETION HANDOFF CONTRACT]` (assembled by
`build_system_prompt` in `beidou/agent/prompts.py`) instructs agents to include
the full envelope in `detail` on every `done` call.

A `PostToolUse` SDK hook fires when an agent calls
`mcp__beidou__signal_review(detail=...)` (`beidou/agent/hooks.py::on_signal_review`).
When `is_error=False` (primitive accepted the call), the hook reads `detail`
from the tool input and delivers it verbatim to the leader's inbox as a
`completion_report` message. This emits a `completion.reported` event.

When `is_error=True` (primitive rejected the call — `envelope_missing`,
`plan_incomplete`, etc.), the hook is a strict no-op: no delivery, no state
mutation. The agent already sees the error in its next tool result.

On the `AgentRecord`, the hook sets `review_pending=True` and stamps
`review_pending_ts` with the current time (`beidou/orchestrator.py`,
`AgentRecord` fields). This mutation happens **at the end of the success path**,
after the leader-vs-root delivery is set up, so an unexpected raise during
delivery does not leave the agent stuck pending. The pending flag is cleared when:

- The agent's direct leader (or the human user gateway) delivers any
  non-system message to the agent → `completion.rework` event emitted
  (`beidou/orchestrator.py::inbox_put`).
- `terminate_child` fires for that agent → `completion.approved` event
  emitted (`beidou/orchestrator.py::inbox_put`).

The plain `terminate_child(agent_id)` call is the leader's APPROVE verdict
on the child's `signal_review(detail=...)` request. If the child has not
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
  routes the review through the human gateway via
  `Orchestrator.gateway_ask_user_structured`. The user is presented with
  **Approve** and **Rework** options:
  - **Approve** → `Orchestrator.terminate_root()` is awaited; the run unwinds
    via the existing terminate-sentinel path. A `completion.reported` event
    with `via=user_gateway, decision=approve` is emitted.
  - **Rework** → a `from_id="user"` rework message is delivered to the root's
    inbox (body prefixed with `rework: `) so the next turn can continue.
    `completion.reported` with `via=user_gateway, decision=rework` is emitted.
  If the gateway round-trip raises, the hook falls back to
  `completion.empty(reason="gateway_failure: <ExcType>")` and returns so the
  tool call does not deadlock. The hook execution timeout for both
  `AskUserQuestion` (PreToolUse) and `mcp__beidou__signal_review` (PostToolUse)
  is `HOOK_REVIEW_TIMEOUT_S = 1800.0` (30 minutes), set in
  `beidou/agent/hooks.py`; this overrides claude-code's 60s default so a real
  human review is not silently truncated.
- Hook is skipped if the `signal_review` call itself errored (`is_error=True`).

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

For every `AgentRecord` with `review_pending=True`, let
Δt = now − `review_pending_ts`. The escalation ladder:

| Δt | `review_ping_count` before action | Action |
|---|---|---|
| < 60 s | any | nothing |
| ≥ 60 s | 0 | ping #1: deliver directive message to leader — "child X awaiting your decision; call `terminate_child` or `send_message` NOW." `review_ping_count++`; reset `review_pending_ts` to now (so next threshold counts from this ping). Emits `completion.reping`. |
| ≥ 60 s | 1 | ping #2: same body + warn "If you do not act, this will escalate to the user gateway in ~60s." `review_ping_count++`; reset `review_pending_ts`. Emits `completion.reping`. |
| ≥ 60 s | 2 | escalate: emit `review.escalated_to_user`; if a gateway is registered, call `gateway_ask_user` (best-effort). `review_ping_count++`. |
| any | ≥ 3 | silent — user owns it; watchdog stops acting on this agent. |

Each ping resets `review_pending_ts` so the countdown restarts from the
previous ping, not from the original report. The pings are delivered to the
**leader** via `deliver_message(from_id="beidou", kind="ping")`.

Implementation: `beidou/orchestrator.py::_watchdog_tick` (Pass A loop).

### Pass B — general liveness nudge

Pass B uses a single **freshness** number per agent computed by
`_agent_freshness_ts(agent_id)`. Freshness is `0` when the runtime knows the
agent is still active or legitimately waiting:

- `inflight_tools > 0` — a tool call is currently running.
- `review_pending=True` — this agent is waiting for its leader to review
  its completion; Pass A handles waking that leader.
- `_questions.has_pending_through(agent_id)` — the agent forwarded a question
  upstream and is waiting for it to be resolved (nudging it to "call ask_user"
  would trigger the duplicate-ask bug, tsk_80cac529).
- `last_drain_ts is None` or `last_progress_ts > last_drain_ts` — inbox/tool
  activity is newer than the most recent natural drain, so the current turn is
  still in-flight or the agent was freshly awakened since the last drain.

Freshness equals `last_drain_ts` only when all zero-freshness conditions are
absent. The Pass B threshold is: `freshness != 0 AND now − freshness >= IDLE_NUDGE_S`.

**Leaf-only restriction.** Pass B only nudges agents that are currently
**leaves** — agents with no *active* children. The predicate is
`Orchestrator.has_active_children(agent_id)`: an agent is a leaf iff every
team it leads has zero still-registered members. An agent that has *ever*
spawned a team is NOT a leaf only while at least one of its children is
still alive in `_agents`; once every child has exited (and been removed
from `_agents`), the ex-parent is treated as a leaf and becomes eligible
for Pass-B nudges. This rule fixes the `tsk_658f44b6` hang where the
impl-leader terminated its last child but never reported done — under the
old "ever-spawned" predicate it was excluded from Pass B forever; under
the active-children predicate it gets nudged ~`IDLE_NUDGE_S` after going
idle and either reports done or escalates. Active leaders (those still
holding children) are still skipped here — Pass A (review-pending
escalation) and Pass C (terminate-grace backstop) cover them.

`idle_nudge_count >= MAX_PINGS_BEFORE_ESCALATION` — already escalated; skip.

The nudge body names four concrete actions the agent can take:

1. Call `signal_review(detail=...)` if work is complete.
2. Take the needed coordination step (e.g. `send_message` or `terminate_child`)
   if waiting on another agent.
3. Call `ask_user` if blocked on missing user input.
4. **Keep working** — if work is not finished and there are concrete next steps,
   emit the next plan or tool call on this turn.

After the nudge is delivered, `last_progress_ts` is reset to now so the next
threshold counts from this ping.

Same 3-strike escalation as Pass A: nudge #1, nudge #2 (with escalation
warning), then a watchdog-owned background ask to the user gateway. When the
user answers, `_watchdog_ask_and_deliver_liveness_answer` delivers the answer
into the asking agent's inbox via `deliver_message(kind="liveness_answer")`,
because the agent's prior SDK turn has already drained and it is parked on
inbox, not awaiting the question future. Events: `liveness.nudge` (every fired
nudge) and `liveness.escalated_to_user` (on escalation).

### In-flight tracking

`AgentRecord.inflight_tools` is incremented by the drain loop when a
`ToolUseBlock` arrives, and decremented when the matching `ToolResultBlock`
arrives (`beidou/sdk_agent.py`, drain loop). `AgentRecord.last_drain_ts` is
stamped at the moment a `ResultMessage` arrives — the natural end of an SDK
turn. `AgentRecord.last_progress_ts` is bumped on every tool start, every tool
end, and every inbox arrival (`beidou/orchestrator.py::inbox_put`); it is
runtime support state that `_agent_freshness_ts` consults to determine whether
any activity has occurred since the most recent drain. The Pass B watchdog
itself only consumes freshness — it does not read `last_progress_ts` directly.

### Leader-side review gate (PreToolUse interceptor)

A leader agent with any direct child whose `review_pending=True` is
blocked from calling any tool except the following allowlist
(`beidou/sdk_agent.py::on_review_gate`):

```
Read, Glob, Grep, Bash,
mcp__beidou__terminate_child,
mcp__beidou__send_message,
mcp__beidou__list_pending_reviews,
mcp__beidou__signal_review,
mcp__beidou__ask_user,
mcp__beidou__answer_question,
mcp__beidou__escalate_question
```

Any other tool call is denied with a directive message naming the pending
child and the two valid resolution actions. Each denial emits
`review_gate.denied`.

### Reply obligation (PreToolUse interceptor)

When an agent receives a `kind="inquiry"` message (sent via
`send_message(..., expects_reply=True)`), the orchestrator records a pending
reply obligation on the recipient's `AgentRecord.pending_replies` and sets
`reply_gate_active=True` at turn-end if any obligations remain unreplied.

While `reply_gate_active=True`, every tool call is screened by the
`on_reply_gate` PreToolUse hook (`beidou/agent/hooks.py`). Only tools in
`REPLY_GATE_ALLOWLIST` may proceed (read tools, status reporting,
ask_user / answer_question / escalate_question, send_message itself,
terminate_child, list_pending_reviews). Every other tool is denied with a
`permissionDecisionReason` naming the pending sender(s) and directing the
agent to reply via `mcp__beidou__send_message(to=<sender>, content=...)`
first. Each denial emits `reply_gate.denied`.

The gate clears when the agent calls `send_message` to each unreplied
sender; the `on_send_message_reply` PostToolUse hook decrements obligations
and flips `reply_gate_active` back to False once the dict is empty.

This obligation is enforced even when the agent has chosen to defer or
ignore the inquiry. The agent must explicitly reply ("acknowledged",
"deferred until X", or any substantive response) before continuing other
work.

### New events summary

| Event | Emitted by | Meaning |
|---|---|---|
| `completion.approved` | `orchestrator.py::inbox_put` | `terminate_child` fired for a `review_pending` agent; review accepted. |
| `completion.rework` | `orchestrator.py::inbox_put` | Leader or user delivered a message to a `review_pending` agent; review returned for rework. |
| `completion.reping` | `orchestrator.py::_watchdog_tick` | Watchdog pinged the leader again (Pass A). |
| `review.escalated_to_user` | `orchestrator.py::_watchdog_tick` | Leader failed to act after 3 pings; escalated to user gateway. |
| `liveness.nudge` | `orchestrator.py::_watchdog_tick` | Idle agent nudged (Pass B). |
| `liveness.escalated_to_user` | `orchestrator.py::_watchdog_tick` | Idle agent nudged 3× without progress; escalated to user gateway. |
| `review_gate.denied` | `agent/hooks.py::on_review_gate` | Leader tried to call a non-allowlisted tool while a child review is pending. |
| `reply_gate.denied` | `agent/hooks.py::on_reply_gate` | Agent tried to call a non-allowlisted tool while a reply obligation was pending. |
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

### 5.1 Crash recovery policy (subprocess crashes)

When the Claude Agent SDK CLI subprocess crashes (exit code 1 as seen by
`beidou/sdk_agent.py`), the exception propagates into the orchestrator's
`_run_agent_with_policy` loop. Beidou applies a hybrid retry strategy before
escalating:

| Strike | Action | Why |
|---|---|---|
| 1 | **Resume** via `resume=session_id` + recovery prompt | Preserves full conversation history; handles transient crashes (OOM, network). Wrapped in inner try — if resume fails (mid-tool-call API 400), promoted directly to strike 2 without counting an extra crash strike. |
| 2 | **Fresh restart** with recovery prompt referencing workspace | Handles deterministic crashes (CLI max turns, context limits) by resetting state. |
| 3 | **Escalate** to leader (members) or user gateway (root) | Gives up; human/leader decides. |

Reset `crash_strikes` to 0 on any successful `run_agent()` return.
`asyncio.CancelledError` is re-raised, not treated as a crash (it signals
watchdog cancellation, not a subprocess failure).

**Constants:** `CRASH_STRIKES = 3` (in `beidou/primitives/core.py`), NOT
in `docs/limits.md` (implementation constant, not a system boundary).

**Stderr capture:** `ProcessError` attributes (`.exit_code`, `.stderr`) are
lost in SDK serialization (`query.py:308` serializes only `str(e)`). The
ONLY reliable capture path is a Python stderr callback wired via
`ClaudeAgentOptions.stderr`. `beidou/sdk_agent.py::run_agent()` creates a
`collections.deque(maxlen=200)` buffer and a closure callback; on crash the
buffer contents are flushed to `AgentRecord.last_crash_stderr` and included
in the `agent_error` and `agent_crashed` events.

**Session continuity:** `fork_session=False` is set explicitly when resuming
to ensure session continuity, not branching. `session_id` is pre-generated
(`str(uuid.uuid4())`) and stored on `AgentRecord.last_session_id` before any
SDK call, so the orchestrator can read it even if the process crashes before
the first `ResultMessage`.

**MCP rebuild risk:** MCP server is rebuilt per `run_agent()` call. A resumed
session may reference stale `tool_use_id`s from the old MCP server. The
strike-1 inner try/catch handles this: if the resume 400s, it promotes to
strike 2 (fresh restart with new MCP) without burning another crash slot.

**`agent_crashed` event** is emitted on every crash, carrying `caller_id`,
`exception`, `msg`, `strike_count`, `stderr`, and `ts`.

**`agent_completed` with `stop_reason="crashed"`** is emitted in the
`sdk_agent.py` exception handler before re-raising, restoring the
observability invariant that every `agent_started` is paired with an
`agent_completed`.

**`root_crash_escalation` event** is emitted when the root agent's crash
strikes exhaust (mirrors `root_contract_escalation`). For non-root agents,
the escalation path posts a `send_message` to the team leader recommending
`terminate_child` or re-spawn.

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

- The SDK imposes **no per-tool timeout**. Verified by an SDK-behaviour
  probe (since removed): blocking tool calls at 60s and 180s completed
  cleanly with no SDK-level timeout.
- `ask_user` has no timeout. The runtime parks the agent on the gateway
  response future and resumes it when an answer arrives.
- **SDK-builtin `AskUserQuestion` is rejected.** When a model emits the
  SDK-builtin `AskUserQuestion` tool call, the Beidou hook
  (`on_ask_user_question` in `beidou/agent/hooks.py`) returns a
  `permissionDecision="deny"` whose `permissionDecisionReason` redirects the
  agent to `mcp__beidou__ask_user`. The hook does NOT call the gateway and
  does NOT synthesize tool_called/tool_result events — earlier versions did
  both, which (a) bypassed the leader chain so questions surfaced straight
  to the user, and (b) followed the same hook-synth bug class fixed in
  commit 82d5290 (envelope_missing). The hook emits a single
  `ask_user_question.redirected` event for observability, then the agent
  re-issues via `mcp__beidou__ask_user`, which routes through the leader
  chain (`Orchestrator.gateway_ask_via_chain`) so each leader can answer
  locally before the question reaches the user.
- Beidou does NOT layer its own retry on top of `query()`. Per-call retries
  are delegated to `ClaudeAgentOptions`.
- The liveness watchdog (§3.1) is a separate `asyncio.Task` (`beidou-watchdog`)
  running alongside the agent SDK loops; it is not part of any single agent's
  lifecycle and imposes no per-agent or per-boundary constraint.
