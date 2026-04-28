# `caller_id` popped from `emit_event` kwargs

**Resolved:** 2026-04-26 · **Refs:** beads `my_simple_agent-2as`

## Problem

JSONL events were missing `caller_id`, breaking downstream consumers that expected it (frontend, watchdog, debug tooling).

## Root cause

`emit_event` `pop`-ed `caller_id` out of `kwargs` to use it for routing/context, but never put it back into the JSONL payload. By the time the line was written, the field was gone.

## Fix

Either don't `pop` `caller_id`, or copy it so both `agent_id` and `caller_id` are present in the persisted record.

## Decision / lesson

- **`pop` from a parameter dict that you also persist is a footgun.** Use `dict.get()` for routing-only side reads, or take a separate keyword arg in the function signature so the persisted dict stays untouched.
- When data flows from Python kwargs → JSONL, treat the kwargs dict as **immutable input**. Make a working copy if you need to extract fields.

## References

- Producer: `beidou/orchestrator.py:emit_event`.
