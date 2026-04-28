# Svelte 5 `$state` proxy bypassed via Map-stored refs

**Resolved:** 2026-04-26 · **Refs:** beads `my_simple_agent-87l`, commit `df4a8c4`

## Problem

In the web UI's tool-call cards, every tool stayed visually "running …" forever even after `tool_result` had arrived. The 250 ms duration timer kept ticking.

## Root cause

`beidou/web/frontend/src/reducer/reduce.ts` `tool_result` handler did:

```ts
const item = _pendingTools.get(tool_use_id);
item.duration_ms = elapsed;     // mutation through a raw reference
item.result = ...;
```

`_pendingTools` was a plain `Map<string, ToolCall>`. **Svelte 5's deep `$state` proxy does not wrap `Map` values.** The `item` reference pulled out of the Map was the *raw* object, not the proxied one. Mutating `.duration_ms` on it never tripped the proxy's `set` trap, so reactivity never fired. `ToolCard.svelte:8` (`pending = item.duration_ms === null`) stayed true forever.

The existing `reduce.test.ts` did not catch it because the tests called `createInitialState()` directly (a plain object), never wrapping the result in `$state()`. Tests passed; UI was broken.

## Fix

Store the **stream-array index** in `_pendingTools` instead of the raw item ref. Patch via `agent._stream[idx].duration_ms = ...` so the proxy's `set` trap fires:

```ts
const idx = _pendingTools.get(tool_use_id);
agent._stream[idx].duration_ms = elapsed;
agent._stream[idx].result = ...;
```

Added a regression test that wraps state in `$state()` and asserts the patch is reactivity-tracked.

## Decision / lesson

- **Svelte 5's deep proxy does not penetrate `Map` or `Set` values.** Anything you `Map.get()` is the raw object, and mutating it bypasses reactivity. Either store an index/key into a proxied array, or keep working state inside the proxied tree.
- **Reducer tests must run against `$state(...)`-wrapped state**, not the bare initial object. Otherwise the test is a fiction — it verifies the reducer's *intent* but not its observable effect on the UI.
- General: when a framework provides "magic" reactivity, the boundary of where the magic stops is part of its API. Find it explicitly.

## References

- Live code: `beidou/web/frontend/src/reducer/reduce.ts` (`tool_result` case), `beidou/web/frontend/src/components/ToolCard.svelte`.
- Related: 2026-04-26-page-refresh-skips-history.
