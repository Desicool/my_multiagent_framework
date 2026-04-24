# Observability: events and accounting

Beidou observes agents by draining each SDK agent's async message iterator
(`async for msg in query(...)`). The drain loop lives in `beidou/sdk_agent.py`
(to be built) and emits Beidou events to the existing sinks:

- **JSONL**: `~/.beidou/events/{task_id}.jsonl` (append-only).
- **SQLite**: `~/.beidou/stats.db` (WAL mode, aggregated tables).

Both are already implemented in `beidou/events.py` (`EventEmitter`) and
`beidou/db.py`. The drain loop is their new producer.

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

Per tool-use block. Emitted once per `ToolUseBlock` observed.

| Field | Source |
|---|---|
| `ts` | Wall clock at block arrival. |
| `agent_id` | Context. |
| `message_id` | Parent assistant message id. |
| `tool_use_id` | `ToolUseBlock.id`. |
| `name` | `ToolUseBlock.name` (includes `mcp__beidou__` prefix for Beidou primitives). |
| `input` | `ToolUseBlock.input` (may be redacted by a future layer - not redacted today). |
| `duration_ms` | Orchestrator-measured: time between tool_use arrival and corresponding `ToolResultBlock` arrival. |
| `is_error` | `ToolResultBlock.is_error` (False when no error field present). |

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

Emitted every time the SDK `query()` returns for an agent that had not
consumed a terminate sentinel. See `agent-runtime.md` section 4.

| Field | Source |
|---|---|
| `ts` | Wall clock. |
| `agent_id` | Context. |
| `strike_count` | Current consecutive-violation count (1-indexed). |
| `action` | One of: `resumed`, `escalated_to_leader`, `escalated_to_user` (root). |

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
  append-only. Canonical event log; replay-friendly.
- **SQLite** `~/.beidou/stats.db`: aggregated tables (`tasks`, `teams`,
  `agents`, `events`). Upserted for fast queries via `beidou stats`,
  `beidou events --follow`, and the web UI.

## Accounting granularity (plan reconciliation)

The original plan assumed per-agent rollup was acceptable. The prototype
showed per-**turn** token counts are also available (deduplicated by
`message_id`). We therefore record both: per-turn tokens (fine-grained, no
USD) plus terminal rollup (coarse, with USD). This is a strict upgrade; no
regression recorded.
