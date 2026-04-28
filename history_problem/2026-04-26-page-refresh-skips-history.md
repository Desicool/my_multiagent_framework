# Page refresh skipped historical events on reconnect

**Resolved:** 2026-04-26 · **Refs:** beads `my_simple_agent-26b`, commit `df4a8c4`

## Problem

Refreshing the dashboard during a running task caused the user to see only events that happened **after** the page load. All prior agent activity disappeared.

## Root cause

`beidou/web/frontend/src/lib/streamService.ts:openTask`:

1. `snapshot` pre-populates teams/agents/stats and sets `initialCursor = snap.cursor` — the timestamp of the **latest** event.
2. The WebSocket is opened with `since = <latest>`, so the server skips every event before that point.

Result: the snapshot gives you "what is true now" but no event stream history, so per-agent timelines and tool-call streams render empty.

## Fix

After snapshot hydration but before opening the WebSocket: paginate `fetchEvents(taskId, backfillCursor, 500)` from `0` until exhausted, `applyEvent` on each, and use the final cursor as `initialCursor` for the WS. Reducer must be idempotent on `agent_started`/`team_created` (verified). Added a "loading history …" status indicator and 6 vitest tests for the snapshot+backfill flow.

## Decision / lesson

- **A snapshot is "current state", not "everything that happened to get here".** If your UI cares about per-event history (timelines, streams), you need both: snapshot for fast initial paint + backfill for the event stream.
- **The reducer must be idempotent before you can backfill.** Replaying old events must not double-count. Test this explicitly with a `[event, event]` replay.
- The reverse anti-pattern: don't replay everything from `since=0` if a snapshot already covers it — you'll double-count rollups. Use snapshot for what it's good for (counts, current set membership), backfill for what it's good for (per-item history).

## References

- Live code: `beidou/web/frontend/src/lib/streamService.ts:openTask`.
- Related: 2026-04-26-svelte-state-proxy-bypass.
