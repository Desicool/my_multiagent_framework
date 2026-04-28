# Worker skills re-asked questions answered upstream

**Resolved:** 2026-04-27 · **Refs:** commit `6839527`

## Problem

Diagnosing `tsk_638793b7` (calculator-with-React run) showed the user had to answer the **same Vite/scaffold/TypeScript/scope question three or four times**: PM #1 asked it, the architect asked it again, the `deploy_advisor` asked overlapping scope questions, then a respawned PM #2 asked the scaffold question yet again. The user got tired of typing the same answer.

## Root cause

Every downstream worker skill ran its `## Ambiguity escalation (mandatory)` block **before** reading the upstream artifact (`requirements.md` / `SPEC.md` / `tasks.md` / `artifacts/`) that already contained the answer. Each role independently re-discovered the same choices because their own prompt told them to.

This is the dual of `2026-04-26-skills-guessed-binding-choices`: that fix made roles ask. This fix made them ask **only after reading what the previous role already wrote**.

## Fix

Prompt-only — no contracts, no primitive changes:

- `software_architect/SKILL.md`: re-order STEP 0 (resolve ambiguity) and STEP 1 (read requirements). The new STEP 1 reads `requirements.md` first; the new STEP 2 only escalates for choices `requirements.md` did not pin down. The "ALWAYS require a question if unspecified" list became a "MIGHT require a question — check `requirements.md` first" list.
- `deployment_engineer` / `test_engineer` / `qa_engineer` / `junior_engineer` `/SKILL.md`: each gained a `## Read upstream artifacts FIRST` block before its existing `## Ambiguity escalation` block, naming the exact artifacts to read for each role variant.

## Decision / lesson

- **The order of prompt sections matters.** "Read first, then ask" vs "Ask first, then read" produces wildly different agent behavior. Sections execute top-to-bottom in the model's reasoning.
- **The "ask" instruction needs an "after reading what is already written" qualifier**, otherwise it becomes "ask everything every time you wake up", which is annoying enough that users disable agents.
- This pairs with `2026-04-27-question-routing-bypasses-chain.md`: the prompt fix helps within a single phase; the routing fix helps across cross-team respawns where artifacts aren't in scope.

## References

- Live code: `beidou/skills/coding/*/SKILL.md` (multiple files).
- Related: 2026-04-26-skills-guessed-binding-choices, 2026-04-27-question-routing-bypasses-chain.
