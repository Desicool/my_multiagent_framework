# `_ensure_db` skipped migrations on existing DBs

**Resolved:** 2026-04-25 · **Refs:** beads `my_simple_agent-5c7`, commit `37bd5c1`

## Problem

After the `template` → `skill` rename refactor (`6da84cc`), every existing user got a hard crash on `beidou run`:

```
sqlite3.OperationalError: table tasks has no column named skill
```

## Root cause

`beidou/cli.py:23-27` had:

```python
def _ensure_db():
    if not DB_PATH.exists():
        init_db()
```

`init_db()` is idempotent (`CREATE TABLE IF NOT EXISTS` everywhere, plus `ALTER TABLE` only when columns are missing) and contains the `template` → `skill` column-rename migration. But the existence guard meant the migration never ran on a DB that had been created before the refactor — exactly every existing user.

## Fix

Drop the existence guard. Call `init_db()` unconditionally on every `beidou run`. Cost is one `PRAGMA` plus a few `CREATE TABLE IF NOT EXISTS` no-ops per invocation — negligible.

## Decision / lesson

- **Idempotent migrations must run unconditionally.** "It already exists, skip setup" is wrong if setup also includes migrations.
- The only correct guard is on the *individual* `ALTER TABLE` (which `init_db` already had), not on the whole `init_db` call.
- Touch every code path that wraps a "first run" check after a schema change, or write a migration test that boots from a stale DB image.

## References

- Live code: `beidou/cli.py:_ensure_db`, `beidou/db.py:init_db`.
