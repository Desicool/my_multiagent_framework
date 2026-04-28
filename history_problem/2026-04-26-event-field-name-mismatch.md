# Frontend/backend event field-name mismatch

**Resolved:** 2026-04-26 · **Refs:** beads `my_simple_agent-12e`, `my_simple_agent-0g4`, commit `6710a5a`

## Problem

Center-pane agent activity in the web UI was empty even though `~/.beidou/events/<task_id>.jsonl` showed the events were emitted. Eight different field names were spelled one way on the backend and a different way in the Svelte reducer. Any single mismatch silently dropped the event in the frontend reducer.

## Root cause

There was no enforced wire schema between `beidou/sdk_agent.py` (emitter) and `beidou/web/frontend/src/stores/events.svelte.ts` (consumer). Examples:

- `input_tokens` vs `in_tok`
- `output_tokens` vs `out_tok`
- `model` vs `model_requested`
- `caller_id` vs `agent_id`

Two-sided drift: each side renamed its half independently and the only place the rename surfaced was when a user noticed an empty pane.

## Fix

Two layers, one commit:

1. Backend (`beidou/sdk_agent.py`) emits **both** spellings — old + new — for every drifted field. Backwards-compatible additive change.
2. Frontend gains `normalizeEvent()` in `events.svelte.ts` to coerce inbound JSONL into one canonical shape before the reducer sees it.

## Decision / lesson

- **Single-source the wire schema.** When two layers communicate via JSONL, they must share a canonical field-name list (a TypeScript type derived from a Python dataclass, or vice versa). Hand-maintained correspondence rots silently.
- When you can't unify schemas immediately, **emit both spellings on the producer** rather than translating on the consumer. Producer-side compatibility is local; consumer-side translation lives at every consumer.
- Drop fields **only after every consumer is updated and a release has shipped**.

## References

- Live wire shape lives in `docs/observability.md`.
- Reducer entrypoint: `beidou/web/frontend/src/stores/events.svelte.ts:normalizeEvent`.
- Related: 2026-04-25-team-created-payload-and-root-emit (same root cause, different field).
