# Architecture

Beidou is an **orchestrator**. `claude-agent-sdk` owns each single-agent loop.
Beidou never intercepts per-LLM-call; it spawns agents, feeds them primitives
via an in-process MCP server, and drains their message stream for observability.

## Process layout

```
+--------------- beidou (Python, orchestrator) ---------------+
|                                                              |
|  CLI / task input                                            |
|      |                                                       |
|      v                                                       |
|  Orchestrator  (beidou/orchestrator.py)                      |
|    - resolves task -> team composition                       |
|    - spawns each agent via sdk_agent.run(...)                |
|    - drains each agent's async message iterator              |
|        -> events: agent_started / tool_called /              |
|           turn.usage / run.cost / agent_completed            |
|    - routes A2A traffic (send_message)                       |
|    - maintains the team graph / inbox registry               |
|    - persists events to ~/.beidou/ (unchanged sinks)         |
|                                                              |
|  Per-spawn MCP server (create_sdk_mcp_server)                |
|    - one server instance per agent spawn                     |
|    - exposes mcp__beidou__<primitive> tools                  |
|    - every @tool closure binds caller_id + orchestrator      |
|                                                              |
+---------------------------------------------------------------+
                   ^
                   | custom tools (in-process MCP)
                   v
+-------- claude-agent-sdk (per agent, subprocess) ----------+
|  runs its own turn loop                                    |
|  reads SKILL.md body as system_prompt                      |
|  calls mcp__beidou__* tools when it needs a peer / Beidou  |
+------------------------------------------------------------+
```

## File map (intended layout)

| File | Role |
|---|---|
| `beidou/orchestrator.py` (to be built) | Composes teams, spawns SDK agents, drains streams, routes A2A. |
| `beidou/sdk_agent.py` (to be built) | Thin wrapper around `claude_agent_sdk.query(...)`; builds `ClaudeAgentOptions`; emits Beidou events from the message iterator. |
| `beidou/primitives/core.py` (to be built) | Pure-Python implementation of each primitive; takes explicit `caller_id` and orchestrator handle, no module globals. |
| `beidou/primitives/mcp.py` (to be built) | `@tool`-decorated wrappers around `core.py`, packaged via `create_sdk_mcp_server`. |
| `beidou/skills/loader.py` (to be built) | Parses SKILL.md frontmatter -> `ClaudeAgentOptions`. |
| `beidou/events.py` (existing) | `EventEmitter`: JSONL + SQLite sinks. Unchanged. |
| `beidou/db.py` (existing) | SQLite schema. Unchanged. |
| `beidou/workspace.py` (existing) | Workspace helpers. Unchanged. |
| `beidou/context.py` (existing) | `AgentContext` still carries `task_id`, `workspace`, `emitter`, `caller_id` down spawn chain. `on_llm_call` / `on_tool_call` hooks are retired. |

## Per-spawn MCP server

One `create_sdk_mcp_server` instance is built **per agent spawn**. The reason
is explicit context: every primitive needs `caller_id` (the spawned agent's id)
and a handle to the orchestrator. Those are closed over at spawn time, so the
MCP tool body never has to read `caller_id` from the model's tool input. This
guarantees the self-lead invariant for `create_team` and the leadership check
for `terminate_child` cannot be spoofed by the agent.

## Context propagation

`AgentContext` chains parent -> child via `_kv` lookup (Go-style). Four keys
must be set before any spawn:

| Key | Provenance | Consumed by |
|---|---|---|
| `task_id` | Top-level CLI invocation | Event emitter, workspace path, JSONL filename |
| `workspace` | `WorkspaceLayer` per team | File tools inside the SDK agent (scoped writes) |
| `emitter` | Root orchestrator construction | Drain loop in `sdk_agent.py` |
| `caller_id` | Orchestrator at spawn time | Bound into every primitive closure for validation |

Child contexts inherit these via the `_kv` chain. Sub-team spawns call
`ctx.child(...)` from the calling agent's context so the leader id resolves to
that agent.

## Event flow

```
SDK agent  --emit-->  async iterator of messages  (query() generator)
                                  |
                                  v
                      drain loop in sdk_agent.py
                                  |
                      translates SDK messages
                        -> Beidou events
                                  |
                                  v
                          EventEmitter
                          /           \
                         v             v
              JSONL append           SQLite upsert
           ~/.beidou/events/          ~/.beidou/stats.db
           {task_id}.jsonl
```

Events emitted per SDK message type: see `observability.md` for the exact
mapping and the deduplication rules (assistant-message fragments share a
`message_id`; usage counted once per unique id).

## Why this split

- The SDK already implements an anti-looping, cache-friendly, retryable
  single-agent loop. Re-implementing it was a waste and (historically) a source
  of drift in `beidou/agent.py:18-99`.
- All Beidou-specific behaviour (team graph, A2A routing, observability
  aggregation, termination authority) is external to the single-agent loop. It
  belongs in the orchestrator boundary.
- Tools-as-MCP means the agent-facing surface is declarative: changes flow
  through `tool-surface.md` and the primitives module, without touching the SDK.
