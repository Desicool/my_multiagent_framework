# `teams` table single-column PK collided across tasks

**Resolved:** 2026-04-25 · **Refs:** beads `my_simple_agent-hmy`

## Problem

Running a second task in the same install made the new task's frontend render an empty skeleton with no root team. Reverting to the first task showed teams correctly.

## Root cause

`docs/observability.md` describes SQLite as an aggregated cache. The `teams` table had `team_id` as a single-column `PRIMARY KEY`. The orchestrator used a constant `ROOT_TEAM_ID = 'tm_root'` for every task's root. So the second task's `INSERT OR IGNORE INTO teams ...` for `tm_root` silently dropped (PK collision with task 1's row). The snapshot endpoint then queried `WHERE task_id = ?` and got zero rows.

## Fix

Change the `teams` table primary key to composite `(team_id, task_id)`. Add a migration in `db._MIGRATIONS` that rebuilds the table with the new PK.

(`tm_root` itself was later removed by the teamless-root refactor, but the lesson about composite keys generalizes.)

## Decision / lesson

- **Constants used as IDs across distinct logical scopes need composite PKs.** If `tm_root` is the same string for every task, then `(team_id, task_id)` is the actual identity, not `team_id` alone.
- `INSERT OR IGNORE` masks PK collisions silently. When you write `OR IGNORE`, you are *promising* that the duplicate is identical to the existing row — verify that's still true after every refactor.
- Always include the scoping column in the PK from day one when an entity is task-scoped, team-scoped, or session-scoped.

## References

- Live code: `beidou/db.py` (`teams` table DDL + `_MIGRATIONS`).
- Related: 2026-04-25-team-created-payload-and-root-emit.
