# `HookMatcher` silent 60 s timeout truncated human round-trips

**Resolved:** 2026-04-27 · **Refs:** commit `fe7dbce`

## Problem

When the user took longer than 60 seconds to respond to either an `AskUserQuestion` (PreToolUse) or a root-completion review (PostToolUse on `report_status`), the hook silently dropped its result and the agent saw a synthetic timeout. The session then either deadlocked or escalated for the wrong reason.

## Root cause

`claude-code` applies a **60-second default timeout to `HookMatcher` when `timeout` is `None`** (see `HookMatcher.timeout` docstring + `_internal/query.py:191-192`). Beidou previously left both matchers at `None`. There was no error, no log line — the hook just returned early.

## Fix

Both matchers are now constructed with `timeout=HOOK_REVIEW_TIMEOUT_S = 1800.0` (30 minutes), which is long enough for any reasonable human turnaround.

## Decision / lesson

- **Read the SDK defaults for any time-bounded extension point you use.** The `claude-agent-sdk` defaults are tuned for autonomous fast-path tool calls; anything that bridges to a human needs an explicit longer timeout.
- **Silent timeouts are a particularly hostile failure mode.** If you can't avoid them at the SDK layer, wrap the hook in your own `asyncio.wait_for` with a deliberate timeout and log explicitly when you hit it.
- General: hunt for `None`-valued timeout defaults at every layer between user input and your code. Each one is a potential silent truncation.

## References

- Live code: `beidou/sdk_agent.py` (search for `HOOK_REVIEW_TIMEOUT_S`).
- SDK source for verification: `claude_agent_sdk/_internal/query.py` `HookMatcher.timeout`.
