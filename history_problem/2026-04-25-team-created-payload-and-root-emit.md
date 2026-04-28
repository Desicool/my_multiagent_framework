# `team_created` payload field names + missing root emit

**Resolved:** 2026-04-25 · **Refs:** beads `my_simple_agent-u6v`, `my_simple_agent-1wu`

## Problem

During trial run `tsk_b3415340` the frontend rendered a pane skeleton with **no team data at all**. The snapshot endpoint returned `teams: []` for the running task.

## Root cause

Two bugs converged:

1. `orchestrator.py:run_root` registered the root team in memory (`self._teams[ROOT_TEAM_ID]`) but never emitted a `team_created` event nor called `db.upsert_team`. The frontend (which had just dropped its synthetic-root code path — see commit `2026-04-27-teamless-root`) had nothing to render.
2. The sub-team `team_created` emit at `orchestrator.py:345` used payload field names `(team_id, name, leader_id)` that did not match what `events.py:90-99` reads `(new_team_id, team_name, leader_agent_id)`. So sub-teams would also be invisible — the root bug just hid the sub-team bug.

A third closely related bug: `events.py:94` set `parent_team_id = team_id` where `team_id` was the *emitter context* positional (the emitter's own team), not the payload's `parent_team_id` field. Sub-teams ended up with `parent_team_id=None`, breaking the tree.

## Fix

- Rename sub-team `team_created` payload fields to `new_team_id` / `team_name` / `leader_agent_id` to match the consumer.
- After registering the root team in `run_root`, emit a `team_created` event with `new_team_id='tm_root'`, `team_name='root'`, `leader_agent_id=root_agent_id`, `parent_team_id=None`.
- `events.py`: `parent_team_id = p.get('parent_team_id', team_id)` — prefer explicit payload, fall back to emitter context.

(The `tm_root` constant has since been retired by the teamless-root refactor; see `2026-04-27-...` entries. The lesson stands.)

## Decision / lesson

- **Every persisted entity needs an emit at the moment of registration**, including the "obvious" root case. Code paths that look like setup ("just put this in a dict") still need to fire observability events if downstream consumers expect them.
- **Don't conflate two namespaces in one variable name.** When the function signature has both `team_id` (emitter) and `parent_team_id` (payload), do not write `team_id` and hope the right one wins.

## References

- Producer: `beidou/orchestrator.py:run_root`, `beidou/events.py`.
- Related: 2026-04-26-event-field-name-mismatch, 2026-04-27-teamless-root (in commit `38e3cd1`).
