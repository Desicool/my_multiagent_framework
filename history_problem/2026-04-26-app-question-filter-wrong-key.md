# `app.py` filtered events on `type` instead of `event` key

**Resolved:** 2026-04-26 · **Refs:** beads `my_simple_agent-av0`

## Problem

The `/api/.../questions` endpoint returned an empty list even when there were pending `ask_user` questions in the JSONL.

## Root cause

`beidou/web/app.py` line 231 used `evt.get("type")` for filtering, but the JSONL schema uses `"event"` as the discriminator key. The filter silently matched zero rows.

## Fix

Changed `evt.get("type")` → `evt.get("event")`.

## Decision / lesson

- **One canonical key name for the event discriminator across the whole codebase.** When a project uses `event` in some places and `type` in others, you will eventually write one and mean the other.
- This is a special case of the general "wire-shape drift" lesson — see `2026-04-26-event-field-name-mismatch.md`.

## References

- Consumer: `beidou/web/app.py` (search for `/questions` endpoint).
