# Non-Claude models emit raw `AskUserQuestion`

**Resolved:** 2026-04-25 · **Refs:** beads `my_simple_agent-i7i`, commit `b3c5449`

## Problem

When Beidou ran against a non-Claude provider (MiniMax via the Anthropic-compatible proxy at `api.minimaxi.com/anthropic`), or even against real Claude when `~/.claude/skills/` had stray gstack docs in `setting_sources`, the model would emit a raw `AskUserQuestion` tool call instead of the Beidou-namespaced `mcp__beidou__ask_user`. The SDK rejected the unknown tool name with `is_error=true` after ~30 ms and the agent stalled.

Repro from `~/.beidou/events/tsk_d2858059.jsonl`:

```
CALL AskUserQuestion → RESULT 30 ms is_error=true → run.cost end_turn → parked
```

## Root cause

The model was pattern-matching on Claude training-data conventions. `AskUserQuestion` is a real tool in Claude Code's standard surface, so any model whose training has seen Claude transcripts will reach for it before reading the actual tool list.

## Fix

A `PreToolUse` hook on `AskUserQuestion` intercepts the call before the SDK's unknown-tool reject fires. The hook flattens the structured `{questions:[...], context}` schema into one composite prompt + `context_hint` string and routes it through the same `orch.gateway_ask_user(caller_id, question, context_hint)` plumbing that `mcp__beidou__ask_user` uses. The user's answer comes back via `permissionDecision="deny"` + `permissionDecisionReason=<answer>`, which the SDK delivers to the model as the tool's effective response — so the model sees a successful tool result and proceeds.

## Decision / lesson

- **Models leak their training corpus into tool calls.** Any tool name from the training data of a popular foundation model is a name your runtime should recognize, even if you don't expose it. This is true for `AskUserQuestion`, `WebSearch`, `WebFetch`, `Edit`, `Write`, `Bash`, etc.
- The `PreToolUse` permission-decision channel (`deny + reason`) is a useful general escape hatch for routing "wrong tool name" calls to the right backend without exposing a duplicate primitive.
- When supporting a non-canonical model provider, expect more leakage, not less. Test the "common tool names" surface explicitly.

## References

- Live code: `beidou/sdk_agent.py:on_ask_user_question`, `beidou/orchestrator.py:gateway_ask_user`.
- Related: 2026-04-26-textblock-less-completion-handoff (another non-Claude leakage class).
