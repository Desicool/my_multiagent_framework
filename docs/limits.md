# Limits

**EVERY LINE IN THIS FILE IS A BOUNDARY.** Changing any value in this file
requires explicit user approval via `AskUserQuestion` *before* code is
written. Bug fixes that preserve all values in this file proceed without
approval. See `README.md` for the approval rule.

## 1. Team fan-out cap

| | |
|---|---|
| **Value** | **8 members per single `create_team` call** |
| Rationale | Keeps per-team cognitive load manageable for the leader; bounds the inbox + spawn burst Beidou has to absorb at once. |
| Enforced in | `create_team` primitive (`beidou/primitives/core.py`). Returns structured error `fanout_exceeded` when exceeded. |
| Change policy | Requires user approval to change. |

## 2. Team recursion depth

| | |
|---|---|
| **Value** | **5 (team nesting depth; depth 0 = teamless agent)** |
| Rationale | Bounds the depth of the cascade termination and the liveness walk. 5 is plenty for orchestrator -> phase -> task -> spike -> probe; deeper usually indicates a missing aggregation step. |
| Enforced in | `create_team` primitive. A teamless agent (depth 0) spawning its first team creates a depth-1 team. Beidou reads the caller's current team depth from the registry, rejects with `depth_exceeded` when `caller_team_depth + 1 > 5`. |
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

## 5. Per-agent concurrent in-flight `create_team` calls

| | |
|---|---|
| **Value** | **1 (serialized; no simultaneous create_team from the same agent)** |
| Rationale | Removes a class of race conditions in leader assignment and depth accounting. The agent can still create teams sequentially without limit (subject to fan-out and depth caps). |
| Enforced in | Per-agent asyncio lock held by the orchestrator during `create_team`. Returns `concurrent_create_team` on contention. |
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

## 7. Per-agent token ceiling per run

| | |
|---|---|
| **Value** | **1,000,000 total tokens (input + output + cache) per agent run** |
| Rationale | Catches runaway loops and bounds worst-case cost per agent. Aggregated from `turn.usage` events during the run and cross-checked against `ResultMessage.usage` at completion. |
| Enforced in | Orchestrator observer. On overshoot, orchestrator signals the agent's **team leader** via `send_message` recommending `terminate_child`. Beidou does NOT directly terminate a non-root agent (termination authority is leader-only per `agent-runtime.md` section 2). |
| Change policy | Requires user approval to change. |

## Summary table

| # | Boundary | Value | Enforced in |
|---|---|---|---|
| 1 | Team fan-out per `create_team` | 8 | `create_team` primitive |
| 2 | Team nesting depth (depth 0 = teamless agent) | 5 | `create_team` primitive |
| 3 | Per-agent inbox cap | 1000 | `send_message` primitive |
| 4 | Contract-violation strikes | 3 | Orchestrator recovery |
| 5 | Concurrent in-flight `create_team` per agent | 1 | Orchestrator lock |
| 6 | Workspace size per team | 500 MiB | Workspace monitor |
| 7 | Per-agent token ceiling per run | 1,000,000 | Orchestrator observer |

All seven require user approval to change.
