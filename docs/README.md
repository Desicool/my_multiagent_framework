# Beidou Specification Index

This folder is the **source of truth** for Beidou behaviour. Every non-trivial change
MUST start by opening the relevant spec(s) below. A diff that changes behaviour
without touching the corresponding `docs/*.md` fails review by definition.

## Read-first mapping

Given a change kind, open these files FIRST (top-to-bottom), then code.

| You are changing... | Open FIRST |
|---|---|
| Agent tool surface (adding/removing/renaming a primitive, input schema, error shape) | `tool-surface.md`, `agent-runtime.md`, `limits.md` |
| A retry count, timeout, fan-out cap, recursion depth, token ceiling, inbox cap, workspace cap | `limits.md` (then the primitive spec in `tool-surface.md`) |
| A skill (new SKILL.md, new frontmatter field, template substitution rules, loader behaviour) | `skills.md`, `agent-runtime.md` |
| The orchestrator <-> SDK boundary, event drain, context propagation, process layout | `architecture.md`, `agent-runtime.md` |
| Team topology: leader/member semantics, cascade termination, liveness, message routing | `orchestration.md`, `tool-surface.md` (create_team, terminate_child, send_message) |
| Event schema, JSONL/SQLite fields, accounting, turn-level usage, cost rollups | `observability.md` |
| The web UI / frontend (Svelte components, reducer, panels, build) | `web-ui.md`, `observability.md` |
| Dev process, PR checklist, which specs gate which change | `workflows.md` |

## Approval rule for boundary changes

Any diff that:

1. Modifies a value in `limits.md`, OR
2. Adds/removes/renames a tool in `tool-surface.md`, OR
3. Changes a contract statement in `agent-runtime.md` or `orchestration.md`, OR
4. Changes the SKILL.md frontmatter schema in `skills.md`, OR
5. Changes the event schema in `observability.md`

...**requires explicit user approval before any code is written.** Raise the
specific boundary and proposed new value via `AskUserQuestion`. Bug fixes that
preserve all documented boundaries proceed without approval.

## The ten specs

1. `README.md` - this index.
2. `architecture.md` - orchestrator/SDK split, process layout, context propagation, event flow.
3. `agent-runtime.md` - contract between Beidou and each SDK agent (persistence, termination, prompt contract, recovery).
4. `skills.md` - SKILL.md frontmatter schema and loader behaviour.
5. `tool-surface.md` - canonical spec of every MCP tool an agent sees.
6. `orchestration.md` - team graph, self-lead invariant, message routing, termination cascade.
7. `observability.md` - event schema and accounting granularity.
8. `limits.md` - every hard limit; every line is a boundary.
9. `workflows.md` - per-change-kind checklist for developers.
10. `web-ui.md` - frontend layout, event-to-state reducer, build instructions, bundle policy.

## Working rules

- Before any non-trivial edit, open this README, follow to the relevant specs,
  and state in chat which specs were read.
- Bug fixes that preserve all documented boundaries and contracts proceed
  without approval.
- Behaviour changes land in the same commit as their spec update.
