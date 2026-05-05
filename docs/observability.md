# Observability: events and accounting

Beidou observes agents by draining each SDK agent's async message iterator
(`async for msg in query(...)`). The drain loop lives in `beidou/sdk_agent.py`
and emits Beidou events to the JSONL sink; the orchestrator emits lifecycle
and contract events.

- **JSONL**: `~/.beidou/events/{task_id}.jsonl` (append-only, authoritative).
- **SQLite**: `~/.beidou/stats.db` (WAL mode, aggregated rollup cache only).

## Reducer contract

Consumers derive state by reducing the JSONL stream in order.
Dedup key for `turn.usage` messages: `(caller_id, message_id, ts)`.
Dedup key for tool pairs: `tool_use_id` (shared by `tool_called` and `tool_result`).

## Event catalogue

### `agent_started`

Lifecycle. Emitted before the first SDK message for a spawn.

| Field | Source |
|---|---|
| `ts` | Wall clock at spawn. |
| `task_id` | Context. |
| `team_id` | Context. **Nullable.** The root agent emits `team_id: null` because it starts teamless (no synthetic root team exists). Any subsequent `team_created` event (if any) is the first one a real agent emits when it explicitly calls `create_team`. Non-root agents always have a non-null `team_id`. |
| `agent_id` | Assigned by orchestrator. Stable join key across all events. |
| `role` | From `create_team` roles entry. `null` for the root agent (it is not spawned via `create_team`). |
| `name` | Human-readable display name, e.g. `frontend-engineer-a3b2`. `agent_id` is the stable join key; `name` is a convenience label for UIs and logs. |
| `template` | Skill name (e.g. `junior_engineer`). |
| `model_requested` | The `model=` passed to `ClaudeAgentOptions` (HINT, see `agent-runtime.md` section 6). |

---

### `agent_completed`

Lifecycle. Emitted after the SDK iterator returns and the final ack has
been recorded (or after the contract-violation escalation ends the agent).

| Field | Source |
|---|---|
| `ts` | Wall clock at loop exit. |
| `agent_id` | Context (emitted as `caller_id` in the JSONL). |
| `terminated` | Boolean. `true` when the agent consumed a terminate sentinel. |
| `stop_reason` | One of: `end_turn` (normal), `no_result`, `cancelled` (CancelledError), `crashed` (subprocess crash before re-raise), `crash_escalated` (crash strikes exhausted). |
| `exit_reason` | (Legacy) One of: `terminate_ack` (normal), `contract_violation_escalated`, `orchestrator_shutdown`. Present on orchestrator-emitted agent_exited events. |

---

### `agent_error`

Emitted when an unhandled exception escapes the SDK iterator. An
`agent_completed` event with `stop_reason="crashed"` always follows
(observability invariant: every `agent_started` has a paired
`agent_completed`).

| Field | Source |
|---|---|
| `ts` | Wall clock at exception. |
| `caller_id` | Agent context. |
| `exception` | Exception class name (string). |
| `msg` | Exception message string (synonym: `error`). |
| `stderr` | Captured subprocess stderr from the ring buffer (via `ClaudeAgentOptions.stderr` callback). May be empty string if no stderr was captured. |
| `stop_reason` | Stop reason if available at the point of failure, otherwise absent. |

---

### `agent_crashed`

Emitted by the orchestrator (`_run_agent_with_policy`) when a subprocess
crash is caught. One per crash attempt. Followed by either a resume/restart
or an escalation.

| Field | Source |
|---|---|
| `ts` | Wall clock at crash catch. |
| `caller_id` | Agent that crashed. |
| `exception` | Exception class name (string). |
| `msg` | Exception message string. |
| `strike_count` | Current crash strike (1-indexed, pre-reset). |
| `stderr` | Captured subprocess stderr from `AgentRecord.last_crash_stderr`. |
| `ts` | Wall clock at crash catch. |

---

### `root_crash_escalation`

Emitted when the root agent exhausts `CRASH_STRIKES` and cannot recover.
Analogous to `root_contract_escalation`.

| Field | Source |
|---|---|
| `ts` | Wall clock at escalation. |
| `agent_id` | The root agent. |
| `crash_strikes` | Total crash strikes exhausted. |
| `stderr` | Last captured stderr. |

---

### `config_warning`

Non-fatal misconfiguration detected during skill loading (e.g. an agent
spawned with an empty `allowed_tools` list). Does not prevent the spawn but
almost always signals a mis-built spec.

| Field | Source |
|---|---|
| `ts` | Wall clock at detection. |
| `agent_id` | Context. |
| `skill` | Skill name that triggered the warning. |
| `warning` | Human-readable description string (e.g. `empty_allowed_tools`). |

---

### `turn.usage`

Per-turn token accounting. Emitted **once per unique
`AssistantMessage.message_id`** seen in the stream.

**Deduplication rule** (verified during the original SDK-behaviour probe;
the throwaway prototype that produced the finding has since been removed):
the SDK may yield multiple `AssistantMessage` fragments that share a
`message_id` (one per block). Count usage once per id. A simple
`seen: set[str]` in the drain loop suffices.

| Field | Source |
|---|---|
| `ts` | Wall clock at emission. |
| `caller_id` | Context. |
| `message_id` | `AssistantMessage.message_id`. |
| `model` | `AssistantMessage.model`. **Authoritative**; may differ from `model_requested` (see `agent-runtime.md` section 6). |
| `stop_reason` | `AssistantMessage.stop_reason`. |
| `input_tokens` | `AssistantMessage.usage.input_tokens`. |
| `output_tokens` | `AssistantMessage.usage.output_tokens`. |
| `cache_creation_input_tokens` | `AssistantMessage.usage.cache_creation_input_tokens` (if present). |
| `cache_read_input_tokens` | `AssistantMessage.usage.cache_read_input_tokens` (if present). |

**Per-turn USD is NOT available.** The SDK does not expose a per-assistant
cost (verified during the original SDK-behaviour probe). USD lands on
`run.cost` only. Do not synthesize per-turn dollars; it would misrepresent
the cost accounting the SDK actually performs.

---

### `tool_called`

Start of a tool span. Emitted by the drain loop when it observes a
`ToolUseBlock` in the message stream. The drain loop is the sole owner of
this emission — the MCP wrapper does NOT emit `tool_called`.

| Field | Source |
|---|---|
| `ts` | Wall clock at `ToolUseBlock` arrival. |
| `caller_id` | Context. |
| `message_id` | Parent `AssistantMessage.message_id`. |
| `tool_use_id` | `ToolUseBlock.id`. Join key for the matching `tool_result`. |
| `name` | `ToolUseBlock.name` (includes `mcp__beidou__` prefix for Beidou primitives). |
| `input` | `ToolUseBlock.input` (may be redacted by a future layer — not redacted today). |

---

### `tool_result`

End of a tool span. Emitted by the drain loop when it observes the
`ToolResultBlock` matching a previously-seen `tool_called`.

| Field | Source |
|---|---|
| `ts` | Wall clock at `ToolResultBlock` arrival. |
| `caller_id` | Context. |
| `tool_use_id` | `ToolResultBlock.tool_use_id`. Join key — links back to the corresponding `tool_called`. |
| `duration_ms` | Orchestrator-measured: monotonic time from `ToolUseBlock` arrival to `ToolResultBlock` arrival. |
| `is_error` | `ToolResultBlock.is_error`; `false` when the field is absent. |
| `error_reason` | Text from `ToolResultBlock.content` when `is_error=true`. For Beidou primitives this is the JSON `{error, message, details}` string produced by `beidou/primitives/mcp.py`. For SDK builtin tools it is the raw error text. Truncated to 2000 chars with a `…(truncated)` suffix. `null` when `is_error=false` or when the block carried no extractable text. |

**Pairing rule:** `tool_use_id` is the join key between `tool_called` and
`tool_result`. Every `tool_called` will be followed by exactly one
`tool_result` with the same `tool_use_id`. The drain loop tracks in-flight
tool uses in a `pending_tool_uses: dict[str, float]` map (keyed by
`tool_use_id`, value = monotonic arrival time) to compute `duration_ms`.

**Caveat — parallel tool calls:** When multiple `ToolUseBlock`s are dispatched
in a single `AssistantMessage`, their corresponding `ToolResultBlock`s arrive
at roughly the same time (the drain loop processes them sequentially but the
results are produced in parallel by the model). All `duration_ms` values will
be approximately equal, reflecting the wall-clock time until the slowest tool
completes. Individual per-tool timing is not measurable from the drain loop.

**Error capture:** `error_reason` is populated by the drain loop in `beidou/sdk_agent.py` only when `is_error=true`. The drain loop concatenates text fragments from `ToolResultBlock.content` (which may be a string or a list of `{type,text}` blocks); non-text fragments (image blocks, etc.) are skipped. The field is the same on disk in JSONL and in the SQLite event row — no separate persistence path. Survives page refresh because the JSONL is replayed in full on every WebSocket connect (see `beidou/web/tail.py`).

---

### `run.cost`

Terminal, emitted once per agent spawn when the SDK yields a
`ResultMessage`. Carries the **authoritative** cost and duration figures.

| Field | Source |
|---|---|
| `ts` | Wall clock on ResultMessage. |
| `agent_id` | Context. |
| `total_cost_usd` | `ResultMessage.total_cost_usd`. |
| `duration_ms` | `ResultMessage.duration_ms`. |
| `duration_api_ms` | `ResultMessage.duration_api_ms`. |
| `num_turns` | `ResultMessage.num_turns`. |
| `usage` | `ResultMessage.usage` (aggregate rollup). |
| `model_usage` | `ResultMessage.model_usage` - per-model breakdown. Use to get authoritative per-model dollar cost. |

**Reconciliation:** the sum of `turn.usage` token counts should roughly
match `run.cost.usage` totals. If they diverge, trust `run.cost` (SDK
ground truth) and investigate the dedup logic.

---

### `ask_user_question.redirected`

Emitted by `on_ask_user_question` PreToolUse hook
(`beidou/agent/hooks.py`) when a model emits the SDK-builtin
`AskUserQuestion` tool. The hook denies the call (with a redirect reason
pointing the agent at `mcp__beidou__ask_user`) and emits this event for
observability. No tool_called/tool_result pair is synthesized — earlier
versions did, which made the redirect indistinguishable from a real
gateway call in the event stream.

| Field | Source |
|---|---|
| `ts` | Wall clock at emission. |
| `caller_id` | The agent that emitted the raw `AskUserQuestion`. |
| `questions_count` | Length of the `questions` array (or `0` if missing/malformed). |

---

### `disallowed_alias.denied`

Emitted by `on_disallowed_alias` PreToolUse hook
(`beidou/agent/hooks.py`) when a forbidden SDK shadow tool reaches the
hook. In normal operation the SDK's `disallowed_tools` filter drops these
tools before any hook runs, so this event is a backstop signal: if it
fires, the SDK layer leaked. Useful as a regression alarm. Currently
covers `SendMessage` and `TodoWrite` (see
[`docs/tool-surface.md#forbidden-tools`](tool-surface.md#forbidden-tools)).

| Field | Source |
|---|---|
| `ts` | Wall clock at emission. |
| `caller_id` | The agent that attempted the forbidden tool. |
| `tool_name` | `"SendMessage"` or `"TodoWrite"`. |

---

### `question_asked`

Emitted by `QuestionBroker` when an agent calls `ask_user`. The `prompt`
field is **truncated to 200 characters**; the full record (full `questions`
array, `context`, `chain`) is only available via `GET /api/questions/pending`.
Treat this event as a ping and re-poll the API for rich data.

| Field | Source |
|---|---|
| `ts` | Wall clock at emission. |
| `qid` | Unique question id (e.g. `q_abc12345`). |
| `asker` | `agent_id` of the agent that called `ask_user`. |
| `holder` | `agent_id` of the team-leader currently holding the question, or `null` if surfaced to the user. |
| `prompt` | Derived flat-text summary of the question, truncated to 200 chars. |
| `questions` | The raw `questions` array (Claude Code wire shape) as passed to the broker. Included alongside `prompt` for consumers that prefer the structured form. |

---

### `question_answered`

Emitted by `QuestionBroker` after the question future resolves.

| Field | Source |
|---|---|
| `ts` | Wall clock at answer receipt. |
| `qid` | Matches the corresponding `question_asked` event. |
| `asker` | `agent_id` of the original asker. |
| `chain_len` | Length of the audit chain (kept for back-compat). |
| `answers` | Structured answer payload: `[{selected_labels: list[str], text: str|null}, ...]`, one entry per sub-question in order. |
| `answer_text` | Single human-readable rendering used by the web UI to render an answer chat bubble in the asker's stream. |

---

### `agent_input`

Delivery-side event. Emitted by the `input_stream` generator in
`beidou/sdk_agent.py` immediately **before** each user-role turn is yielded
to the SDK session. This is the unified "agent received a message" record —
every user-role text an agent ingests passes through one of the two yield
sites in `input_stream`, so this event covers all four input classes:

1. The initial user task (synthetic, `source="initial"`).
2. Peer messages from `send_message` (`source="queue"`).
3. Web-injected user messages via REST (`source="queue"`, `from="user"`).
4. System notifications via direct `inbox_put` (`source="queue"`, `from="beidou"`).

`terminate` sentinels do **not** produce an `agent_input` event — the
`input_stream` generator returns before yielding them.

**Ordering with `send_message`:** `send_message` (action-side) fires when the
sender calls the primitive. `agent_input` (delivery-side) fires when the
recipient consumes the queue. These events are distinct in time; the frontend
renders them as "A sent X" then "B received X" with potentially other events
between. This is intentionally truthful.

**Behavior note:** A `send_message` queued to a recipient that is terminated
before consumption will produce a `send_message` event on the sender side but
**no `agent_input` event on the recipient side**. The new behavior is more
accurate (the agent did not actually see the message) but differs from the
prior behavior where the recipient bubble appeared regardless.

| Field | Type | Source |
|---|---|---|
| `ts` | float | Delivery time. For queue yields: `msg_in.ts` (origin time, not consume time). For initial task: `time.time()`. |
| `caller_id` | string | The receiving agent. |
| `from` | string | `Message.from_id`: `"user"` for initial task and web-injected; `"beidou"` for system notifications; else the sender `agent_id`. |
| `message_kind` | string | `Message.kind`: `"user"` \| `"system"`. Plus synthetic `"initial"` for the first yield (no underlying `Message`). |
| `source` | string | `"initial"` = first yield from `spec.task`; `"queue"` = pulled from the per-agent inbox. Lets the frontend identify the boot bubble without inspecting `from`/`message_kind`. |
| `content` | string | Raw `msg_in.content` (or `spec.task` for initial). NOT the `[from X] ...` wrapper applied at yield time — that is SDK prompt presentation, not wire content. |
| `message_id` | string | Carried from `Message.message_id` when present; deterministic synthetic for initial: `f"{caller_id}:initial"`. |

**Dedup key for replay/reconnect:** `(caller_id, message_id)`.

---

### `send_message`

Action-side event. Emitted by the `send_message` primitive whenever an agent
(or the user via the web composer) **puts** a message into another agent's
inbox. User messages have `caller_id == "user"`. No `kind` field.

The delivery-side counterpart — when the recipient **consumes** the message
from the queue — is `agent_input` (see above).

| Field | Source |
|---|---|
| `ts` | Wall clock at delivery. |
| `caller_id` | Sender — an `agent_id` or the literal string `"user"`. |
| `to` | Recipient `agent_id`. |
| `content` | Full message text. |
| `message_id` | UUID-based id shared with the inbox `Message` record. |

---

### `contract_violation`

Emitted every time the SDK `query()` returns for an agent that has not been
terminated by the runtime. See `agent-runtime.md` section 5.

| Field | Source |
|---|---|
| `ts` | Wall clock. |
| `agent_id` | Context. |
| `strike_count` | Current consecutive-violation count (1-indexed). |
| `action` | One of: `resumed`, `escalated_to_leader`, `escalated_to_user` (root). |

---

### `assistant_text`

Emitted once per `AssistantMessage` that contains `TextBlock` content (the
agent's natural-language reasoning for a turn). JSONL-only — no SQLite
rollup.

| Field | Source |
|---|---|
| `ts` | Wall-clock seconds at emission. |
| `caller_id` | Context. |
| `message_id` | `AssistantMessage.message_id` — ties to the `turn.usage` event with the same `message_id`. |
| `text` | Concatenation of every `TextBlock` in this `AssistantMessage`. |
| `stop_reason` | SDK-reported stop reason for this turn; `null` if absent. |

Useful for showing per-agent live output in observability tools (e.g. the
web UI right pane).

---

### `status`

Emitted on every `report_status` call.

| Field | Source |
|---|---|
| `ts` | Wall clock. |
| `agent_id` | Context. |
| `state` | One of: `working`, `idle`, `blocked`, `done`. |
| `detail` | Free-text from the call. |

---

### `liveness_check`

Data-only event. Emitted by the orchestrator when an agent calls
`report_status(state="done")`. JSONL-only -- no SQLite rollup. Leader agents
observe member status via `list_peers`.

| Field | Source |
|---|---|
| `ts` | Wall clock. |
| `agent_id` | The agent reporting done. |
| `team_id` | The agent's team. `null` for the root agent (it is teamless). |
| `all_done` | `true` when every member of the team is also in `done` state; `false` otherwise. `null` for the root agent (no team). |

---

### `completion.reported`

Emitted by the `on_report_status` PostToolUse hook after the completion
review has been routed. JSONL-only.

| Field | Source |
|---|---|
| `agent_id` | The agent that reported done. |
| `leader_id` | The reviewer. Either the agent's leader id, or `USER_SENTINEL` for the root. |
| `via` | Routing path: `"hook"` (delivered to leader inbox) or `"user_gateway"` (root review surfaced through the human gateway). |
| `decision` | Present only when `via == "user_gateway"`. One of `"approve"` (root will be terminated) or `"rework"` (a rework message was delivered back to the root's own inbox). |

For non-root agents the leader's resolution (terminate-child vs.
send-message) still emits the existing `completion.approved` / `completion.rework`
events (see `agent-runtime.md`). For the root, those are replaced by the
gateway decision recorded above.

---

### `completion.empty`

Emitted when the root-agent hook's gateway round-trip fails. JSONL-only.

| Field | Source |
|---|---|
| `agent_id` | The agent that reported done. |
| `leader_id` | The reviewer (`USER_SENTINEL` for root). |
| `reason` | `"gateway_failure: <ExcType>"` when the root's gateway round-trip raised an exception. |

This event is only emitted for root agents (where the gateway is the reviewer).
For non-root agents, envelope validation is enforced at the primitive level —
calls without a valid envelope are rejected before the hook runs.

The legacy `reason="root_no_leader"` and `reason="no summary in report_status turn"` values are retired.

---

### `terminate.forced`

Audited override of the completion-review gate or runtime backstop
cancellation. Always paired with a subsequent `agent_completed` (or
equivalent) event when the SDK session ends.

Emitted in two situations:

1. A leader called `terminate_child(force=true)`, bypassing the
   completion-review gate for a member that had not yet
   acknowledged its terminate sentinel.
2. The runtime watchdog cancelled an agent's `run_task` because the
   agent did not consume its terminate sentinel within
   `TERMINATE_GRACE_S` (see `agent-runtime.md` §3.1).

| Field | Source |
|---|---|
| `ts` | Unix timestamp at forced termination. |
| `caller_id` | The actor that triggered the forced terminate. `"watchdog"` when emitted by the runtime watchdog; the leader's `agent_id` when emitted by `terminate_child(force=true)`. |
| `agent_id` | Target agent being forcibly terminated. |
| `team_id` | The team containing the target. |
| `reason` | One of: `"leader_force"` (leader passed `force=true` to bypass the completion-review gate) or `"watchdog_grace"` (target did not consume its terminate sentinel within `TERMINATE_GRACE_S`; runtime cancelled `run_task`). |

---

### `plan_declared`

Emitted by the orchestrator when an agent calls `declare_plan` successfully.
`team_id` is null because `declare_plan` is a pure-data primitive — no team
is created at this point.

| Field | Source |
|---|---|
| `ts` | Wall clock at declaration. |
| `plan_id` | Stable plan identifier. |
| `owner_agent_id` | The agent that declared the plan. |
| `team_id` | Always `null` — no team exists yet. |
| `tasks` | List of task descriptors: `[{id, role, skill, depends_on, status}, ...]`. |

---

### `plan_removed`

Emitted by the orchestrator when an agent calls `remove_plan` successfully.
The plan file is renamed to `{plan_id}.removed.json` on disk for audit.

| Field | Source |
|---|---|
| `ts` | Wall clock at removal. |
| `plan_id` | The plan that was removed. |
| `owner_agent_id` | The agent that removed the plan. |

---

### `task_ready`

Emitted when a task transitions from `blocked` to `ready`. This happens after
a dependency's `terminate_child` approval drives that dependency `in_flight → done`
and all of the newly-ready task's other dependencies are also `done`.

| Field | Source |
|---|---|
| `ts` | Wall clock at transition. |
| `plan_id` | The plan containing the task. |
| `task_id` | The task that became ready. |

---

### `task_spawned`

Emitted when `spawn_agent` transitions a task `ready → in_flight` and launches
the agent. If this is the first `spawn_agent` for the plan's leader, `team_created`
is emitted **before** this event (producer-side ordering invariant: `team_created`
always precedes the first `agent_spawned` for a given team).

| Field | Source |
|---|---|
| `ts` | Wall clock at spawn. |
| `plan_id` | The plan containing the task. |
| `task_id` | The task being executed. |
| `team_id` | The team the agent was added to (materialized on first spawn). |
| `agent_id` | The newly-spawned agent. |

---

### `task_done`

Emitted when `terminate_child` (approve) drives a task `in_flight → done`.
After emission, the orchestrator recomputes the readiness set and emits
`task_ready` for any newly-unblocked dependents.

| Field | Source |
|---|---|
| `ts` | Wall clock at approval. |
| `plan_id` | The plan containing the task. |
| `task_id` | The task that completed. |

---

### `task_failed`

Emitted when `terminate_child(force=true)` drives a task `in_flight → failed`.
Dependents that list the failed task become permanently blocked; recovery requires
`remove_plan` + `declare_plan`.

| Field | Source |
|---|---|
| `ts` | Wall clock at forced termination. |
| `plan_id` | The plan containing the task. |
| `task_id` | The task that failed. |
| `reason` | Reason string from the forced termination (e.g. `"leader_force"`). |

**Note on `team_created` timing:** Prior to the plan-as-data model, `team_created`
was emitted at `create_team` time (atomically with member spawns). With the new
`spawn_agent` flow, `team_created` is emitted on the **first** `spawn_agent` call
(lazy team creation), not at `declare_plan`. The producer-side ordering invariant
is preserved: `team_created` fires before the first `agent_spawned` for that team.
Web frontend reducers that normalise event ordering do not need to change.

---

## Sinks

- **JSONL** `~/.beidou/events/{task_id}.jsonl`: one JSON object per line,
  append-only. **Authoritative event log.** All consumers (web UI,
  `beidou events --follow`) read event history by tailing this file. The
  stream is replay-friendly; state is derived by reducing it.
- **SQLite** `~/.beidou/stats.db`: **aggregated cache only.** Holds three
  rollup tables — `tasks`, `teams`, `agents` — written by the orchestrator
  at state transitions (team created, agent started/ended, task ended) and
  updated incrementally from `turn.usage` and `run.cost`. There is no
  per-event table. Do not treat SQLite as a source of truth for raw event
  data.

## Accounting granularity (plan reconciliation)

The original plan assumed per-agent rollup was acceptable. The prototype
showed per-**turn** token counts are also available (deduplicated by
`message_id`). We therefore record both: per-turn tokens (fine-grained, no
USD) plus terminal rollup (coarse, with USD). This is a strict upgrade; no
regression recorded.
