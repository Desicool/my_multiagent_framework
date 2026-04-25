# Observability: events and accounting

Beidou observes agents by draining each SDK agent's async message iterator
(`async for msg in query(...)`). The drain loop lives in `beidou/sdk_agent.py`
and emits Beidou events to the JSONL sink; the orchestrator emits lifecycle
and contract events.

- **JSONL**: `~/.beidou/events/{task_id}.jsonl` (append-only, authoritative).
- **SQLite**: `~/.beidou/stats.db` (WAL mode, aggregated rollup cache only).

## Reducer contract

Consumers derive state by reducing the JSONL stream in order.
Dedup key for `turn.usage` messages: `(agent_id, message_id, ts)`.
Dedup key for tool pairs: `tool_use_id` (shared by `tool_called` and `tool_result`).

## Event catalogue

### `agent_started`

Lifecycle. Emitted before the first SDK message for a spawn.

| Field | Source |
|---|---|
| `ts` | Wall clock at spawn. |
| `task_id` | Context. |
| `team_id` | Context. |
| `agent_id` | Assigned by orchestrator. |
| `role` | From `create_team` roles entry. |
| `template` | Skill name (e.g. `junior_engineer`). |
| `model_requested` | The `model=` passed to `ClaudeAgentOptions` (HINT, see `agent-runtime.md` section 6). |

---

### `agent_completed`

Lifecycle. Emitted after the SDK iterator returns and the final ack has
been recorded (or after the contract-violation escalation ends the agent).

| Field | Source |
|---|---|
| `ts` | Wall clock at loop exit. |
| `agent_id` | Context. |
| `exit_reason` | One of: `terminate_ack` (normal), `contract_violation_escalated`, `orchestrator_shutdown`. |

---

### `agent_error`

Emitted when an unhandled exception escapes the SDK iterator before
`agent_completed` fires. An `agent_completed` event still follows, with its
`exit_reason` reflecting the error condition.

| Field | Source |
|---|---|
| `ts` | Wall clock at exception. |
| `agent_id` | Context. |
| `error_type` | Exception class name. |
| `message` | Exception message string. |
| `stop_reason` | Stop reason if available at the point of failure, otherwise absent. |

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

**Deduplication rule** (confirmed in `proto_02_token_granularity.py`): the
SDK may yield multiple `AssistantMessage` fragments that share a
`message_id` (one per block). Count usage once per id. A simple
`seen: set[str]` in the drain loop suffices.

| Field | Source |
|---|---|
| `ts` | Wall clock at emission. |
| `agent_id` | Context. |
| `message_id` | `AssistantMessage.message_id`. |
| `model` | `AssistantMessage.model`. **Authoritative**; may differ from `model_requested` (see `agent-runtime.md` section 6). |
| `stop_reason` | `AssistantMessage.stop_reason`. |
| `input_tokens` | `AssistantMessage.usage.input_tokens`. |
| `output_tokens` | `AssistantMessage.usage.output_tokens`. |
| `cache_creation_input_tokens` | `AssistantMessage.usage.cache_creation_input_tokens` (if present). |
| `cache_read_input_tokens` | `AssistantMessage.usage.cache_read_input_tokens` (if present). |

**Per-turn USD is NOT available.** The SDK does not expose a per-assistant
cost. Confirmed in `proto_02_token_granularity.py`. USD lands on `run.cost`
only. Do not synthesize per-turn dollars; it would misrepresent the cost
accounting the SDK actually performs.

---

### `tool_called`

Start of a tool span. Emitted by the drain loop when it observes a
`ToolUseBlock` in the message stream. The drain loop is the sole owner of
this emission — the MCP wrapper does NOT emit `tool_called`.

| Field | Source |
|---|---|
| `ts` | Wall clock at `ToolUseBlock` arrival. |
| `agent_id` | Context. |
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
| `agent_id` | Context. |
| `tool_use_id` | `ToolResultBlock.tool_use_id`. Join key — links back to the corresponding `tool_called`. |
| `duration_ms` | Orchestrator-measured: monotonic time from `ToolUseBlock` arrival to `ToolResultBlock` arrival. |
| `is_error` | `ToolResultBlock.is_error`; `false` when the field is absent. |

**Pairing rule:** `tool_use_id` is the join key between `tool_called` and
`tool_result`. Every `tool_called` will be followed by exactly one
`tool_result` with the same `tool_use_id`. The drain loop tracks in-flight
tool uses in a `pending_tool_uses: dict[str, float]` map (keyed by
`tool_use_id`, value = monotonic arrival time) to compute `duration_ms`.

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
| `agent_id` | Context (= `caller_id`). |
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
