# Question routing bypassed the leader chain

**Resolved:** 2026-04-27 · **Refs:** commit `dd2a3f7`, beads `my_simple_agent-ca4`

## Problem

Even after the "read upstream first" prompt fix (`2026-04-27-worker-skills-reask-upstream`), users still saw duplicate questions across **respawned cross-team cases**: a fresh PM in a new team workspace can't see the prior team's `requirements.md` (different filesystem scope) and re-discovered from scratch.

## Root cause

The chain logic in `QuestionBroker.ask()` was always intact — it routed based on `ctx.parent.agent_id`. The drift was at the **bridge**: `cli.py:_GatewayAdapter.ask_structured()` hardcoded `parent=None` with a comment explicitly acknowledging it bypassed the chain. So every agent's `ask_user` surfaced straight to the user, even when the leader already had the answer in context.

Two layers:

- Layer 1 (prompt): "read upstream first" — see `2026-04-27-worker-skills-reask-upstream`.
- Layer 3 (routing): restore leader-chain routing.

## Fix

Routing:

- New `cli.py:_GatewayAdapter.ask_via_chain()`: looks up the asker's team leader via the orchestrator handle and constructs a broker `ctx` with `parent` set, so the question lands in the leader's `_agent_inbox`. Falls back to direct-to-user when the asker has no team (root) or when its team leader is the user sentinel.
- New `Orchestrator.gateway_ask_via_chain()`: the orchestrator-side hook the new path attaches to.
- `core.py:ask_user` now calls `gateway_ask_via_chain` instead of `gateway_ask_user_structured`. **System paths** (watchdog, root completion review, on_ask_user_question hook for SDK-builtin AskUserQuestion) keep the direct path.
- `inbox.py:QuestionBroker.ask()` and `.escalate()` now post a wake-up system message into the holder's `asyncio.Queue` (via `ctx.get("orchestrator").inbox_put`) so the leader's SDK session resumes with the new question visible. Without this, the question would sit silently in `_agent_inbox` forever.

New primitives:

- `answer_question(qid, answers)`: leader resolves the asker's question directly. Validates only the current holder may answer. Errors: `invalid_input`, `unknown_qid`, `not_holder`, `answer_count_mismatch`.
- `escalate_question(qid, reason)`: leader pushes the question one hop up the chain.

## Decision / lesson

- **A correct implementation behind a broken adapter is invisible.** `QuestionBroker.ask()` had been routing chain-wise the whole time; the `_GatewayAdapter.ask_structured()` shim just refused to pass `parent`. Always grep for `parent=None` on adapter boundaries.
- **A "TODO bypass" with a comment that says it bypasses is technical debt with a half-life of months.** Ship the real path or open an issue and pin a deadline; don't both bypass and document the bypass.
- **Posting a wake-up message** into the holder's queue is required when an inbox is otherwise drained only by SDK turns. Without it, the question lives in a side-channel the SDK doesn't poll.
- **Distinguish agent-originated vs system-originated asks.** Watchdog / root review / SDK-builtin shim should bypass the chain; agent-originated questions should walk it. Routing on the **caller**, not the **target**, makes this clean.

## References

- Live code: `beidou/cli.py:_GatewayAdapter.ask_via_chain`, `beidou/orchestrator.py:gateway_ask_via_chain`, `beidou/primitives/core.py:ask_user`, `beidou/primitives/core.py:answer_question`, `beidou/primitives/core.py:escalate_question`, `beidou/inbox.py:QuestionBroker`.
- Related: 2026-04-27-worker-skills-reask-upstream, 2026-04-27-spawned-members-no-user-task.
