# Termination & review-gate lifecycle (`completion_pending`)

**Resolved:** 2026-04-26 · **Refs:** beads `my_simple_agent-ql2`, `my_simple_agent-xq1`, `my_simple_agent-28m`, `my_simple_agent-be3`, `my_simple_agent-k1q`, `my_simple_agent-qj2`, commit `e96fe15`

## Problem

A cluster of related defects around the completion-review gate:

1. **`tsk_e92d672b` 10:09:55** — `terminate_child` succeeded, but at 10:09:58 the same leader's `create_team` was DENIED by the review gate. The orchestrator hung.
2. **`tsk_e92d672b` 10:10:29** — `SendMessage` was denied by the review gate even though the child being reviewed was already terminated.
3. **`tsk_30e790aa`** — root agent (claude-sonnet-4-6) hit the 1 M token ceiling, kept burning 620 K more tokens with degraded judgment, then called `terminate_child` against the **wrong** child id (claimed it was cleaning up "task-8" but passed the deployer's id), and proceeded to write the subordinates' deliverables itself. The terminated team's children kept running for 10+ minutes because terminate sentinels in their inbox were never consumed mid-tool-loop.

## Root cause

Three converging defects in the AgentRecord lifecycle:

- `terminate_child` did not clear `rec.completion_pending` / `completion_pending_ts` / `review_ping_count` / `idle_nudge_count` after emitting `completion.approved`. Records lingered with `completion_pending=True` forever, blocking the leader's `PreToolUse review_gate`.
- The review gate's `ALLOWED_DURING_PENDING_REVIEW` set was missing SDK-builtin classic names (`Write`, `Edit`, `SendMessage`, `AskUserQuestion`, `WebSearch`, `WebFetch`). When a skill's tool resolved to the SDK builtin spelling, the gate denied it.
- The review gate did not skip children with `rec.terminate_consumed=True`. Already-terminated children continued to count as "pending review", denying unrelated tools.
- Structurally: `terminate_child` only checked "caller leads target's team". Nothing enforced the documented contract from `docs/agent-runtime.md` that `terminate_child` is the leader's APPROVE verdict on a child's `report_status(state="done")`.

## Fix

Three changes, one cohesion commit:

1. **`terminate_child` clears `completion_pending`** after emitting `completion.approved`. Three regression tests in `tests/test_orchestrator_agent_record.py`.
2. **`ALLOWED_DURING_PENDING_REVIEW` expanded** to include `Write`, `Edit`, `SendMessage`, `AskUserQuestion`, `WebSearch`, `WebFetch`. Review gate skips `terminate_consumed=True` children. Six new tests in `tests/test_review_gate.py`.
3. **Completion gate on `terminate_child` (PRIMARY).** `terminate_child` now refuses with `PrimitiveError(code="child_not_pending_review")` when `target.completion_pending=False` AND `target.terminate_consumed=False` AND `force=False`. Explicit `force=True` escape hatch emits an audited `terminate.forced` event with `reason="leader_force"`.
4. **Watchdog backstop** (`Phase 2`): a 30 s asyncio task with two passes — Pass A escalates review-pending agents at 60 s / 120 s / 180 s (then to user gateway); Pass B nudges any agent idle for 120 s with no in-flight tool. Eleven tests.
5. **Defensive envelope guard:** if a child's `report_status` body does not start with `[REVIEW REQUIRED]`, `on_report_status` synthesizes the envelope and emits `completion.envelope_synthesized`. Stamps `last_progress_ts`. Seven tests.

## Decision / lesson

- **State that gates other state transitions must be explicitly cleared.** `completion_pending` was never cleared on terminate — the kind of bug you only catch when two operations happen in quick succession. Whenever a flag enables/disables behavior, write down (in the spec and the test) what clears it.
- **Allowlists for tool names need to enumerate every spelling.** Beidou's primitives have both `mcp__beidou__X` and SDK-builtin classic names. Tests need to cover both spellings for every gate.
- **Authority needs verifiable preconditions, not just permissions.** "Caller leads target's team" is a permission check; "target has actually requested termination" is a precondition. Both are required.
- **Don't trust a degraded-context agent's tool calls.** When the model has hit a token ceiling and continues working with degraded judgment, it will pass wrong IDs and rationalize them. The runtime must enforce contracts the model can't be trusted to honor.
- **Watchdog is a backstop, not a primary path.** It catches stalls but the primary correctness must come from synchronous gates and explicit state transitions.

## References

- Live code: `beidou/orchestrator.py` (`inbox_put` terminate path, `_watchdog_task`), `beidou/sdk_agent.py` (`on_review_gate`, `on_report_status`), `beidou/primitives/core.py:terminate_child`.
- Spec: `docs/agent-runtime.md` §3 (review framing) and §3.1 (watchdog).
- Related: 2026-04-27-root-completion-review-routing.
