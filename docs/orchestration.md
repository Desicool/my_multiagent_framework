# Orchestration: team graph, routing, and lifecycle

## Team graph

- A **team** is a named group of agents with one leader and N members.
- The leader is the agent that called `create_team` (self-lead invariant).
- Every agent is both:
  - a **member** of the team it was spawned into (if any), AND
  - a **potential leader** of any sub-team it creates via `create_team`.
- The **root agent** is the first agent in the task. It starts teamless
  (no team membership, no team record). It may proceed solo or call
  `create_team` to spawn its first team and become that team's leader.
  There is no synthetic "root team": no `team_created` event fires for the
  root agent, and no team record exists for it until it explicitly calls
  `create_team`. See `agent-runtime.md` §2.
- The conceptual depth of the teamless root is **0**. The first team any
  agent spawns has depth 1; sub-teams of that team have depth 2, and so on.
  The depth cap in `limits.md` applies to team nesting depth (depth ≥ 1),
  not to the root agent itself. See `limits.md` §2.

## Self-lead invariant

Enforced at the `create_team` primitive boundary:

1. The MCP tool closure binds `caller_id` at spawn time.
2. On call, Beidou injects `leader_id = caller_id` into the team record.
3. If the model's tool input contains a `leader_id` field, the primitive
   rejects with `leader_override_attempted` and does NOT spawn.

This is why the per-spawn MCP server (`architecture.md`) is built with
`caller_id` in its closure rather than read from tool input: the model
cannot spoof it.

## Team registry (Beidou-owned)

Beidou maintains an in-memory registry keyed by `team_id`:

| Field | Meaning |
|---|---|
| `team_id` | Stable id, printed in events. |
| `name` | Human-readable name. |
| `leader_id` | The agent that called `create_team`. |
| `members` | List of `{agent_id, role, status}`. |
| `parent_team_id` | The team the leader is a member of. `None` for a team directly spawned by the root agent (or by any teamless agent). |
| `depth` | 1 for the first team spawned from the root agent. Validated against `limits.md` recursion cap on each `create_team`. |
| `workspace` | Team workspace path (`{project}/.beidou/tasks/{task_id}/teams/{team_id}/`). No workspace entry exists for the root agent; it uses its own agent-scoped scratch path. |

The registry is append-mostly: members are removed only on terminate-ack.

## Inboxes

Every agent has exactly one inbox: an `asyncio.Queue` bounded by
`limits.md` inbox cap. `send_message` writes; the runtime drains the queue
between SDK turns, delivering each message as a user-role input. Terminate
sentinels are pushed by `terminate_child` (or by the runtime for the root
agent) and are consumed by the runtime — agents never see them.

## Message routing

`send_message(to, content)` executes inside the sender's MCP closure:

1. Look up `to` in the team registry. Reject with `unknown_recipient` or
   `task_mismatch` on miss.
2. Check recipient inbox length against cap. Reject with `inbox_full` on
   overflow.
3. Enqueue `{from: caller_id, content, ts, message_id}`.
4. Return `{delivered: true, message_id}`.

Cross-team sending is permitted (send_message is unrestricted on topology).
Cross-team *terminate*, however, is not (see below).

## Termination cascade

Two authorities, strictly scoped:

1. **Leader over direct-team-member**: via `terminate_child` (the tool).
   Validated against the team registry: caller must be the `leader_id` of
   the team containing `agent_id`.
2. **Beidou over root agent**: internal Python path, NOT exposed as an
   agent-callable tool. Triggered on user signal (Ctrl-C, CLI "task done",
   gateway close). Writes a terminate sentinel to the root agent's inbox.

Cascade mechanics (runtime-driven, depth-first):

When a leader receives a terminate sentinel via `inbox_put`, the orchestrator
automatically posts terminate sentinels to every member of every team led by
that agent (depth-first cascade). This continues recursively until leaves are
reached. Agents never see terminate sentinels — the runtime's outer loop in
`beidou/sdk_agent.py` consumes each sentinel, ends the SDK session for that
agent, and emits `agent_completed` with `terminate_consumed=True`.

1. Beidou posts a terminate sentinel to the root agent's inbox (on user
   signal).
2. The runtime cascade walks the team graph depth-first, posting terminate
   sentinels to all descendants.
3. Each agent's SDK session is ended by the runtime as its sentinel is
   consumed. Leaves terminate first; root terminates last.

**Backstop: terminate-grace watchdog cancel.** When a terminate sentinel is
enqueued for an agent (`inbox_put` with `kind="terminate"`), the runtime
stamps a deadline at `now + TERMINATE_GRACE_S` (in-code constant in
`beidou/orchestrator.py`, alongside the other watchdog constants per
`agent-runtime.md` §3.1). If the deadline elapses with `terminate_consumed`
still false (e.g., the SDK is mid-agentic-tool-loop and never re-awaits its
input queue), the watchdog cancels the agent's `run_task`. The drain loop's
cancellation path sets `terminate_consumed=true`, and a `terminate.forced`
event is emitted with `reason="watchdog_grace"`. This is a runtime backstop
for genuinely livelocked children — it does not replace the leader's
authority via `terminate_child`; it only guarantees an enqueued terminate
actually unwinds within bounded time.

## Liveness checks

**Goal:** a leader knows when all its direct children report `done` so it
can in turn report `done` upward.

Two mechanisms, BOTH active:

1. **Triggered**: every `report_status(state="done", ...)` call wakes up
   Beidou's liveness evaluator for the reporter's **parent leader**. The
   evaluator walks the parent leader's direct children; if all are `done`
   and the parent leader's own state is `done`, Beidou injects a
   `send_message` to the parent leader's parent leader saying "your report
   is complete" (or, more simply, pokes the parent leader's own inbox so
   it can observe via `list_peers`).
2. **Periodic**: Beidou can run the same evaluator on a cadence for
   robustness. Cadence is not a boundary; it's a configuration. If added
   to `limits.md`, it becomes one.

**Liveness does NOT terminate.** It only surfaces completion state.
Termination is still leader-driven.

## Re-assignment

1. Parent P observes child C reported `done` (via a message or via
   `list_peers`).
2. P decides to assign more work: `send_message(to=C.agent_id, content=...)`.
3. The runtime delivers the message as C's next user-role input. C picks up
   the new work in the same session. No re-spawn; same
   `claude_agent_sdk.query` generator.

## Topology walkthrough (verification case)

```
root (R, skill=orchestrator)   ← teamless; depth=0
  leads team "impl"             ← depth=1; parent_team_id=null
    member A (junior_engineer)
      leads team "spike"        ← depth=2; parent_team_id=impl
        member A1 (researcher)
        member A2 (researcher)
    member B (junior_engineer)
    member C (test_engineer)
```

- R starts teamless. No team record, no `team_created` event for R itself.
- R called `create_team(name="impl", roles=[A, B, C])`. Registry: team
  "impl", leader=R, members=[A, B, C], parent_team_id=null, depth=1.
- A called `create_team(name="spike", roles=[A1, A2])`. Registry: team
  "spike", leader=A, members=[A1, A2], parent_team_id=impl, depth=2.
- A is both a member of "impl" and leader of "spike". Both facts hold.
- R cannot `terminate_child(A1)` — it does not lead "spike". A must.
- On root termination: Beidou posts a terminate sentinel to R's inbox. The
  runtime cascade automatically propagates sentinels depth-first to A, B, C,
  then from A's subtree to A1 and A2. Each agent's SDK session ends as its
  sentinel is consumed; leaves terminate first, root last.
