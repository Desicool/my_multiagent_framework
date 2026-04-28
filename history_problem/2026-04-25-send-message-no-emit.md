# `send_message` primitive never emitted observability event

**Resolved:** 2026-04-25 · **Refs:** beads `my_simple_agent-ljt`

## Problem

The web UI's Inbox pane showed nothing during agent-to-agent communication, even though messages were being delivered (recipients clearly acted on them).

## Root cause

`beidou/primitives/core.py:send_message` routed the A2A message via `orch.inbox_put(...)` but never called `orch.emit_event(...)`. Delivery worked; observability did not.

## Fix

Added an `emit_event` call at the end of the happy path in `send_message`, with fields `(ts, agent_id=sender, to=recipient, content, message_id)`. Field names were chosen to match what the reducer's `case 'send_message'` reads.

The 2026-04-27 unified-`agent_input` change later moved authoritative delivery rendering to a new event type (see `2026-04-27-agent-input-unified.md` if you write one), but the producer-side emit in `send_message` is still the only signal the sender's outbound bubble.

## Decision / lesson

- **Every primitive that affects shared state must emit.** The observability layer is part of the contract, not a debug add-on. A primitive that mutates inboxes, agents, or teams without emitting is broken even if it "works".
- Code review checklist for new primitives: does it call `emit_event`? Does the event name and payload match a reducer branch?

## References

- Producer: `beidou/primitives/core.py:send_message`.
- Related: 2026-04-26-event-field-name-mismatch.
