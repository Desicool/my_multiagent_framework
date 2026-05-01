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
|  beidou/engine/                                              |
|    graph.py       — team graph, depth/fan-out caps           |
|    inbox.py       — message routing, termination cascade     |
|    lifecycle.py   — AgentRecord, spawn, recovery policy      |
|    watchdog.py    — liveness, review escalation              |
|    dispatcher.py  — internal event bus for eval handlers     |
|    config.py      — AgentConfig (pure-data boundary)         |
|      |                                                       |
|      |  AgentConfig (paths, strings, lists — no objects)     |
|      v                                                       |
|  beidou/agent/                                               |
|    loop.py        — SDK drain loop, input stream, RunResult  |
|    prompts.py     — system prompt assembly                   |
|    hooks.py       — built-in hooks + user gate/eval loading  |
|    context.py     — typed hook context dataclasses           |
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

## File map

### Engine layer (SDK-agnostic)

| File | Role |
|---|---|
| `beidou/engine/config.py` | `EngineConfig` + `AgentConfig` dataclasses — the pure-data boundary between engine and agent. |
| `beidou/engine/graph.py` | Team graph, self-lead invariant, depth/fan-out caps. |
| `beidou/engine/inbox.py` | Per-agent inbox (asyncio.Queue), send_message routing, termination cascade. |
| `beidou/engine/lifecycle.py` | AgentRecord, spawn, terminate, recovery policy (contract/crash strikes). |
| `beidou/engine/watchdog.py` | Pass A (review escalation) + Pass B (liveness nudge) + Pass C (grace backstop). |
| `beidou/engine/dispatcher.py` | Internal event bus for eval handler subscriptions (separate from JSONL/SQLite EventEmitter). |

### Agent layer (SDK-aware)

| File | Role |
|---|---|
| `beidou/agent/loop.py` | `run_agent()`: SDK drain loop, input stream generator, `RunResult`. |
| `beidou/agent/prompts.py` | System prompt assembly (5 sections, template substitution). |
| `beidou/agent/hooks.py` | `HookRegistry`: loads `module.toml`, registers user gate/eval handlers, builds SDK `HookMatcher` dicts. Also contains the three built-in hooks (AskUserQuestion bridge, review gate, completion routing). |
| `beidou/agent/context.py` | Typed per-hook-point context dataclasses (`ToolCallContext`, `TurnEvalContext`, etc.) for user gate/eval handlers. |

### Primitives and skills

| File | Role |
|---|---|
| `beidou/primitives/core.py` | Pure-Python implementation of each primitive; takes explicit `caller_id` and orchestrator handle. |
| `beidou/primitives/mcp.py` | `@tool`-decorated wrappers around `core.py`, packaged via `create_sdk_mcp_server`. |
| `beidou/skills/loader.py` | Parses SKILL.md frontmatter, resolves allowed-tools, provisions skill files into team workspaces. |
| `beidou/skills/<domain>/<name>/SKILL.md` | Skill prompt body with YAML frontmatter. |
| `beidou/skills/<domain>/<name>/module.toml` | Optional hook declarations (gate/eval handler registration). |
| `beidou/skills/<domain>/<name>/gate.py` | Optional gate handler implementations. |
| `beidou/skills/<domain>/<name>/eval.py` | Optional eval handler implementations. |

### Infrastructure (unchanged)

| File | Role |
|---|---|
| `beidou/orchestrator.py` | Thin facade re-exporting from `beidou/engine/` and `beidou/agent/`. |
| `beidou/sdk_agent.py` | Deprecated shim re-exporting from `beidou/agent/loop.py`. |
| `beidou/events.py` | `EventEmitter`: JSONL + SQLite sinks. Unchanged. |
| `beidou/db.py` | SQLite schema. Unchanged. |
| `beidou/workspace.py` | Workspace helpers. Updated for three-tier workspace model (project + team). |
| `beidou/context.py` | Deprecated. Kept only for legacy tool files that still import it; no longer on the hot path. |

## Per-spawn MCP server

One `create_sdk_mcp_server` instance is built **per agent spawn**. The reason
is explicit context: every primitive needs `caller_id` (the spawned agent's id)
and a handle to the orchestrator. Those are closed over at spawn time, so the
MCP tool body never has to read `caller_id` from the model's tool input. This
guarantees the self-lead invariant for `create_team` and the leadership check
for `terminate_child` cannot be spoofed by the agent.

## Workspace and skill provisioning

Beidou uses a three-tier workspace model:

| Tier | Path | Scope |
|---|---|---|
| **Project** | `<user-supplied PATH>` (via `beidou run --workspace`) | Shared by every agent in every team in the task. Cross-team artifacts, shared inputs, final outputs. |
| **Team** | `<PATH>/.beidou/tasks/{task_id}/teams/{team_id}/` | Shared within one team. Team scratch, mid-step outputs, orchestrator-internal storage (inbox files, artifacts). |
| **Agent** | _(no per-agent subdirectory; agents in a team share the team dir)_ | — |

At team creation (`spawn_team`) and root launch (`run_root`), `provision_skills`
copies every bundled Beidou SKILL.md into:

```
<cwd>/.claude/skills/<skill_name>/SKILL.md
```

where `<cwd>` is the agent's working directory. For sub-team agents, `cwd` equals
the team workspace. For the root agent, `cwd` equals the project workspace
(`{project}/.claude/skills/`). The copies are canonical/raw — no `{role}`
substitution on disk. This populates the project-scope skill directory so the
SDK's `setting_sources=["project"]` discovery can find them.

**Root agent is teamless.** The root agent has no team — it is just the first
agent. Its cwd is the project workspace itself (so user files live in their
natural place), and its scratch dir for inbox files and artifacts is
`{project}/.beidou/tasks/{task_id}/agents/{agent_id}/`. Real teams the root
spawns get team workspaces under `{project}/.beidou/tasks/{task_id}/teams/{team_id}/`
as usual.

**Known limitation — concurrent runs:** running two `beidou run` instances
against the same `--workspace` directory is not safe. Both processes race on
`{project}/.claude/skills/` during skill provisioning and on
`{project}/.beidou/tasks/{task_id}/...` during workspace creation. This is a
documented non-issue for the common single-run case; operators running parallel
experiments should use separate workspace directories.

## SDK skill discovery

`ClaudeAgentOptions` is built with:

```python
setting_sources=["user", "project"],
skills="all",
```

- `"user"`: discovers skills under `~/.claude/skills/` (user skills, never copied).
- `"project"`: discovers skills under `<cwd>/.claude/skills/` (provisioned by
  `provision_skills` at team creation time; `cwd` is the team workspace for
  sub-team agents and the project workspace for the root agent).
- `skills="all"`: enables the `Skill` tool so agents can list and invoke any
  discovered skill. **Does NOT auto-add Bash/Read/Write** — those come from the
  skill's `allowed-tools` via `sdk_builtins_allowlist()`.

## PostToolUse hook — completion reporting

The orchestrator registers a `PostToolUse` hook on `mcp__beidou__report_status`
for every agent (including the root). The hook:

1. Fires when the agent calls `report_status(state="done")`.
2. Reads the agent's last assistant text (bound to the same turn's `tool_use_id`)
   via `Orchestrator.assistant_text_for_turn()`; falls back to the `detail`
   tool-input argument; emits `completion.empty(reason="no summary in report_status turn")`
   if both are empty.
3. Synthesizes a `[REVIEW REQUIRED]` envelope if the body lacks one, so the
   reviewer always gets the unmissable signal.
4. **Routing depends on `leader_id`:**
   - **Non-root** (`leader_id != USER_SENTINEL`): delivers the body to the
     leader's inbox as a `completion_report` message via
     `Orchestrator.deliver_message()` and emits
     `completion.reported(via="hook")`.
   - **Root** (`leader_id == USER_SENTINEL`): awaits
     `Orchestrator.gateway_ask_user_structured()` to ask the human reviewer
     **Approve / Rework**. Approve → `Orchestrator.terminate_root()` is
     awaited; Rework → a `from_id="user"` `rework: …` message is delivered
     to the root's own inbox so the next turn continues. Either branch
     emits `completion.reported(via="user_gateway", decision=...)`. A
     gateway exception is caught and downgraded to
     `completion.empty(reason="gateway_failure: <ExcType>")` so the tool
     call does not deadlock.

The hook is **owned by the orchestrator**, not by any agent. It is built in
`beidou/sdk_agent.py::build_hooks(orch, caller_id, leader_id)` and passed to
`ClaudeAgentOptions.hooks`. Both `AskUserQuestion` (PreToolUse) and
`mcp__beidou__report_status` (PostToolUse) HookMatchers are constructed with
`timeout=HOOK_REVIEW_TIMEOUT_S` (1800s) so human review round-trips are not
truncated by claude-code's 60s default.

## Context propagation

`AgentContext` chains parent -> child via `_kv` lookup (Go-style). Four keys
must be set before any spawn:

| Key | Provenance | Consumed by |
|---|---|---|
| `task_id` | Top-level CLI invocation | Event emitter, workspace path, JSONL filename |
| `workspace` | Orchestrator per team for spawned members; per-agent scratch dir for the teamless root | File tools inside the SDK agent (scoped writes); `{workspace_path}` substitution in system prompt |
| `project_workspace` | Top-level CLI `--workspace PATH` | `{project_workspace_path}` substitution in system prompt; cross-team file sharing via absolute paths |
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
- The engine/agent split (May 2026) separates SDK-agnostic policy (graph, inbox,
  lifecycle, watchdog) from SDK-aware runtime (drain loop, hooks, prompts) with
  `AgentConfig` as a pure-data boundary. See `docs/skill-modules.md` for the
  user-extensible hook system built on this split.

## Hook system

Beidou provides three **built-in hooks** registered on every agent spawn (owned
by the runtime, not by any skill):

| Hook | SDK Hook Type | Purpose |
|------|--------------|---------|
| `on_ask_user_question` | PreToolUse | Intercept raw `AskUserQuestion` calls, route to human gateway |
| `on_review_gate` | PreToolUse | Block leader from advancing while children await review |
| `on_report_status` | PostToolUse | Completion handoff: read assistant text, route to leader/user gateway |

Skills may optionally declare **user hooks** via `module.toml` in the skill
directory. User hooks are additive — they run **after** built-in hooks pass,
and cannot override or bypass built-in policy. User hooks come in two kinds:

- **Gate handlers**: synchronous chain, return `Pass` or `Block(reason)`.
  Mapped to SDK PreToolUse/PostToolUse matchers. Fail-closed.
- **Eval handlers**: fire-and-forget subscribers, return `None`.
  Invoked directly from the drain loop or event dispatch side-channel.
  Fail-open.

Full specification: `docs/skill-modules.md`.
