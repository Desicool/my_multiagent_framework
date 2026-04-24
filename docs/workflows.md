# Workflows: per-change-kind checklist

This is the operational procedure for Claude Code (and humans) making
changes to Beidou. It binds the specs in this folder to concrete
development steps.

**Global rule:** a diff that changes behaviour without touching the
relevant `docs/*.md` fails review by definition.

## Change-kind table

| Change kind | Specs to read FIRST | Approval required? |
|---|---|---|
| Add / remove / rename an agent-visible tool | `tool-surface.md`, `agent-runtime.md`, `limits.md` | **Yes** |
| Change the input/output schema of an existing tool | `tool-surface.md`, `agent-runtime.md` | **Yes** |
| Change a validation rule (e.g. self-lead invariant scope) | `tool-surface.md`, `orchestration.md` | **Yes** |
| Change any numeric value in `limits.md` | `limits.md`, plus the primitive doc for that limit | **Yes** |
| Change the SKILL.md frontmatter schema (new field, type change, rename) | `skills.md` | **Yes** |
| Change template substitution keys (`{role}`, `{team_name}`, ...) | `skills.md` | **Yes** |
| Change the contract with SDK agents (section 3 of `agent-runtime.md`) | `agent-runtime.md` | **Yes** |
| Change termination authority or cascade semantics | `agent-runtime.md`, `orchestration.md` | **Yes** |
| Change the event schema (add/remove fields, rename, change dedup rule) | `observability.md` | **Yes** |
| Change the sink location or DB schema | `observability.md`, `architecture.md` | **Yes** |
| Change retry policy for `contract_violation` | `agent-runtime.md`, `limits.md` | **Yes** |
| Change the model-routing story | `agent-runtime.md`, `observability.md` | **Yes** |
| Add a new SKILL.md (no schema change, no new primitive) | `skills.md` | No (skill author's discretion) |
| Bug fix that preserves every documented boundary and contract | The relevant spec (for context) | No |
| Refactor with no behaviour change | The spec for the area being refactored | No |
| Documentation-only edit (typos, clarifications that do not change meaning) | The file being edited | No |
| Dependency bump that preserves all SDK contracts | `architecture.md`, `agent-runtime.md` | No (unless SDK surface changes) |

## Before-you-code checklist

For any change touching Beidou code:

1. **Identify change kind** from the table above.
2. **Open the "Specs to read FIRST" column** for that kind. Read them,
   not scan them.
3. **State in chat which specs were read.** (Required by the working
   rules in `README.md`.)
4. **If approval required** (column 3 = Yes): ask the user via
   `AskUserQuestion` with the specific boundary and proposed new value.
   Do NOT write code first.
5. **Plan the spec edit and the code edit together**, so they land in the
   same commit.
6. **Code it.** Update the doc and the code in lockstep.
7. **Tests**: run existing tests AND rebuild/restart any running services
   per project CLAUDE.md.
8. **Review gate**: if `git diff` shows code changes but no corresponding
   `docs/*.md` change for a behaviour change, the diff fails review.

## Review-time red flags

- A PR that touches `beidou/primitives/` but not `docs/tool-surface.md`.
- A PR that changes any number in `beidou/orchestrator.py` lookalike-to a
  boundary (retry count, cap, ceiling) but does not touch `docs/limits.md`.
- A PR that adds a SKILL.md but uses a frontmatter field not documented in
  `docs/skills.md`.
- A PR that emits a new event type but does not add it to
  `docs/observability.md`.
- A PR that changes termination logic but does not update
  `docs/agent-runtime.md` or `docs/orchestration.md`.

## Pre-implementation protocol

For substantial design work (e.g. the initial SDK migration):

1. Sketch the design in a scratch doc.
2. Write or update the affected `docs/*.md` FIRST.
3. Build a throw-away prototype where the SDK behaviour is uncertain. See
   `proto_01_long_tool.py` and `proto_02_token_granularity.py` for the
   template. Record findings in `limits.md` / `observability.md`.
4. Only then start on production code.

## Session-end protocol

Per project CLAUDE.md: run quality gates, update issue tracker (`bd`),
`git push`. Work is not complete until `git push` succeeds. The docs in
this folder are part of the deliverable; changes to them MUST be pushed
along with the corresponding code.
