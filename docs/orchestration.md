# Orchestration: team graph, routing, and lifecycle

## Team graph

- A **team** is a named group of agents with one leader and N members.
- The leader is the agent that called `create_team` (self-lead invariant).
- Every agent is both:
  - a **member** of the team it was spawned into, AND
  - a **potential leader** of any sub-team it creates via `create_team`.
- The root agent is a member of a synthetic "root team" of size 1; Beidou
  records itself as the degenerate leader of that root team (sole purpose:
  root-agent termination authority on user signal).

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
| `parent_team_id` | The team the leader is a member of. `None` for root. |
| `depth` | 0 for root. Validated against `limits.md` recursion cap on each `create_team`. |
| `workspace` | Team workspace path (`~/.beidou/workspaces/{task_id}/{team_id}/`). |

The registry is append-mostly: members are removed only on terminate-ack.

## Inboxes

Every agent has exactly one inbox: an `asyncio.Queue` bounded by
`limits.md` inbox cap. `send_message` writes; `read_messages` /
`wait_for_message` read. Terminate sentinels are pushed by `terminate_child`.

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

Cascade mechanics (top-down, agent-driven):

1. Beidou posts terminate sentinel to the root agent's inbox (on user
   signal).
2. Root agent's next `wait_for_message` returns the sentinel.
3. Root agent: for every team it leads, calls `terminate_child(agent_id)`
   on every member. Then waits on `wait_for_message` for final acks from
   each.
4. Each direct child receives its sentinel, does the same recursion, acks
   upward via `send_message`, ends its turn.
5. Root agent, after all acks received, writes final ack to Beidou and ends
   its turn.

Leaves end first; root ends last. The only legal `end_turn` is after
terminate-ack (see `agent-runtime.md` section 5).

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
3. C's next `wait_for_message` returns. C picks up the new work in the same
   session. No re-spawn; same `claude_agent_sdk.query` generator.

## Topology walkthrough (verification case)

```
root (R, template=orchestrator)
  leads team "impl"
    member A (junior_engineer)
      leads team "spike"
        member A1 (researcher)
        member A2 (researcher)
    member B (junior_engineer)
    member C (test_engineer)
```

- R called `create_team(name="impl", roles=[A, B, C])`. Registry: team
  "impl", leader=R, members=[A, B, C], parent=root, depth=1.
- A called `create_team(name="spike", roles=[A1, A2])`. Registry: team
  "spike", leader=A, members=[A1, A2], parent=impl, depth=2.
- A is both a member of "impl" and leader of "spike". Both facts hold.
- R cannot `terminate_child(A1)` - it does not lead "spike". A must.
- On root termination: R terminates A/B/C. A in turn terminates A1/A2.
  A1/A2 ack to A; A acks to R; R acks to Beidou.
