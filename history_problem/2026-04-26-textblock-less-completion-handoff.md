# Models without `TextBlock` strand the completion handoff

**Resolved:** 2026-04-26 · **Refs:** beads `my_simple_agent-1yw`, commit `8e0843e`

## Problem

Running with MiniMax-M2.7, the PM agent's `report_status(state="done")` call landed but the orchestrator never received a `completion_report`. The orchestrator stalled waiting for a delivery that never came; the next phase never spawned.

## Root cause

The `on_report_status` `PostToolUse` hook used the assistant's preceding `TextBlock` content as the body of the completion report. MiniMax-M2.7 emits a `ToolUseBlock` with **no preceding `TextBlock`** — the model goes straight from thought to tool call. The hook had no text to forward, so it forwarded nothing, and the leader's inbox stayed empty.

## Fix

Multi-level fallback in a new `beidou/harness.py:repair_completion_handoff`:

| Level | Strategy | Cost |
|---|---|---|
| L1 | `assistant_text` in same turn → deliver it | free |
| L2 | `detail` arg from `report_status` → deliver it | free |
| L3 | Inject a nudge into the agent inbox | extra API turn |
| L4 | Already nudged once, don't loop | free |

The `PostToolUse` hook handles L1+L2. A new post-turn harness checkpoint runs after all tool results are processed and injects an L3 nudge when both text and detail are missing. The nudge is gated per-agent so it cannot loop.

A `[COMPLETION HANDOFF CONTRACT]` block was added to the system prompt so agents see the contract before their first turn — making L3 nudges rare in practice.

## Decision / lesson

- **Don't depend on assistant `TextBlock` being present alongside a tool call.** Some providers omit it; some users prompt models into terse modes that drop it. Always have a non-text channel for the same information (here, the tool's own `detail` argument).
- **Multi-level fallback with a hard loop guard is the right shape** when the primary path is "model emits the data correctly." L4 ("don't loop") is the most important step — without it, the nudge becomes an infinite recovery loop.
- Treat the system prompt as the *primary* contract and the runtime fallback as defense in depth, not the other way around.

## References

- Live code: `beidou/harness.py:repair_completion_handoff`, `beidou/sdk_agent.py:on_report_status`.
- Related: 2026-04-25-non-claude-askuserquestion (sibling provider-leakage class).
