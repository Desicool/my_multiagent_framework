# Limits

**EVERY LINE IN THIS FILE IS A BOUNDARY.** Changing any value in this file
requires explicit user approval via `AskUserQuestion` *before* code is
written. Bug fixes that preserve all values in this file proceed without
approval. See `README.md` for the approval rule.

## 1. Concurrent in-flight members per team

| | |
|---|---|
| **Value** | **8 concurrently in-flight (spawned) members per team** |
| Rationale | Keeps per-team cognitive load manageable for the leader; bounds the inbox + spawn burst Beidou has to absorb at once. Plan size is unbounded — the readiness queue handles backpressure. |
| Enforced in | `spawn_agent` primitive and (legacy) `create_team` primitive (`beidou/primitives/core.py`). Returns structured error `team_cap_exceeded` (or `fanout_exceeded` for `create_team`) when exceeded. |
| Change policy | Requires user approval to change. |

## 2. Team recursion depth

| | |
|---|---|
| **Value** | **5 (team nesting depth; depth 0 = teamless agent)** |
| Rationale | Bounds the depth of the cascade termination and the liveness walk. 5 is plenty for orchestrator -> phase -> task -> spike -> probe; deeper usually indicates a missing aggregation step. |
| Enforced in | `spawn_agent` primitive (and legacy `create_team` primitive). A teamless agent (depth 0) spawning its first team creates a depth-1 team. Beidou reads the caller's current team depth from the registry, rejects with `depth_exceeded` when `caller_team_depth + 1 > 5`. For `spawn_agent`, the depth check is deferred to spawn time (not `declare_plan` time) because `declare_plan` is a pure-data primitive that does not know about the runtime team graph. |
| Change policy | Requires user approval to change. |

## 3. Per-agent inbox size cap

| | |
|---|---|
| **Value** | **1000 pending messages per agent** |
| Rationale | Bounds memory per agent and forces back-pressure to the sender (who sees `inbox_full` and can escalate or slow down). |
| Enforced in | `send_message` primitive. On overflow, returns structured error `inbox_full` to the SENDER. The recipient is not crashed; the sender decides. |
| Change policy | Requires user approval to change. |

## 4. Contract-violation strikes before escalation

| | |
|---|---|
| **Value** | **3 consecutive violations** |
| Rationale | Occasional end-turn-without-tool is a model quirk; patterned violation is a contract problem. Three strikes balances tolerance against stuck agents. |
| Enforced in | Orchestrator recovery loop (`beidou/orchestrator.py`). After N=3, orchestrator stops resuming and posts a `send_message` to the agent's team leader recommending `terminate_child`. For the root agent, escalates to the user gateway instead. See `agent-runtime.md` section 4. |
| Change policy | Requires user approval to change. |

## 5. Per-agent concurrent in-flight team-spawn calls

| | |
|---|---|
| **Value** | **1 (serialized; no simultaneous `create_team` or `spawn_agent` from the same agent)** |
| Rationale | Removes a class of race conditions in leader assignment and depth accounting. The agent can still spawn sequentially without limit (subject to the in-flight member cap and depth caps). |
| Enforced in | Per-agent asyncio `spawn_lock` held by the orchestrator during `create_team` (legacy) or `spawn_agent`. Returns `concurrent_create_team` or `concurrent_spawn` on contention. `declare_plan` and `remove_plan` are pure-data primitives and do **not** hold the spawn lock; they take a separate per-agent `plan_lock` that serialises plan mutations. |
| Change policy | Requires user approval to change. |

## 6. Workspace max size (per team)

| | |
|---|---|
| **Value** | **500 MiB per team workspace** |
| Rationale | Keeps each team's `{project}/.beidou/tasks/{task_id}/teams/{team_id}/` directory bounded. Above this, agents should externalise (object storage, git LFS, etc.) rather than pile bytes into the team directory. |
| Enforced in | Checked at team start and periodically (cadence is an implementation detail; if formalised it becomes a new boundary in this file). Exceeding emits a `workspace_over_budget` event and posts a `send_message` to the team leader. |
| Change policy | Requires user approval to change. |

Agents may also write to the project workspace (`{project_workspace_path}`) via
SDK file tools using absolute paths. The project workspace is **user-supplied and
not Beidou-capped** — Beidou makes no claims about its size. Operators concerned
about disk usage should monitor the project workspace independently. (This is a
deliberate non-boundary; it is documented here to make the absence explicit.)

**Plan size is intentionally not bounded.** `declare_plan` accepts any number
of tasks in a single call; there is no upper limit on task count per plan.
Backpressure is enforced by the concurrent in-flight cap (#1) and the recursion
depth cap (#2). Operators concerned about huge plans should monitor disk usage
of `~/.beidou/runs/`. (This is a deliberate non-boundary; it is documented here
to make the absence explicit.)

## 7. Per-agent budget controls (SDK-native)

| | |
|---|---|
| **Value** | **Delegated to Claude SDK via `max_turns` and `max_budget_usd` on `ClaudeAgentOptions`** |
| Rationale | The SDK handles auto-compaction internally (keeping per-turn context within the model window). Cost and runaway protection are better enforced by the SDK's native `max_turns` (hard turn limit) and `max_budget_usd` (cost ceiling) than by a cumulative token sum that includes cache reads. Per-agent token totals are still tracked for observability but no longer trigger termination recommendations. |
| Enforced in | SDK-level enforcement via `ClaudeAgentOptions`. Orchestrator still accumulates `total_tokens` per agent for observability/stats. |
| Change policy | Requires user approval to change. |

## Summary table

| # | Boundary | Value | Enforced in |
|---|---|---|---|
| 1 | Concurrent in-flight members per team | 8 | `spawn_agent` / `create_team` (deprecated) primitive |
| 2 | Team nesting depth (depth 0 = teamless agent) | 5 | `spawn_agent` / `create_team` (deprecated) primitive |
| 3 | Per-agent inbox cap | 1000 | `send_message` primitive |
| 4 | Contract-violation strikes | 3 | Orchestrator recovery |
| 5 | Concurrent in-flight team-spawn (`create_team` or `spawn_agent`) per agent | 1 | Orchestrator `spawn_lock` |
| 6 | Workspace size per team | 500 MiB | Workspace monitor |
| 7 | Per-agent budget controls | SDK-native (max_turns, max_budget_usd) | ClaudeAgentOptions |

All seven require user approval to change.
