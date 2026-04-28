# Orchestrator SKILL.md called nonexistent `invoke_*` tools

**Resolved:** 2026-04-25 · **Refs:** beads `my_simple_agent-3dv`

## Problem

The root agent's first turn always failed with `tool_called name='Skill' is_error=true`. The orchestrator skill's system prompt told the model to call `invoke_product_manager`, `invoke_software_architect`, `invoke_qa_engineer` — none of which existed.

## Root cause

`beidou/skills/coding/orchestrator/SKILL.md` lines 40, 44, 75 referenced `invoke_*` primitives that were never implemented. `loader.py:7` had a comment referencing a planned `beidou/skills/tool.py` that did not exist. The skill prompt was written ahead of the implementation and never reconciled.

## Fix

Rewrite the orchestrator skill prompt so Phases 1, 2, and 5 use `create_team` to spawn a 1-member team per role and `wait_for_message` for completion — consistent with the existing Phase 3 pattern. **No code change needed; only the SKILL.md prompt.**

(Later, `wait_for_message` itself was deleted — see commit `7d57bfa` "auto-park agents on per-agent queue". Skills now report status and the runtime delivers messages on the inbox queue.)

## Decision / lesson

- **A SKILL.md that references unimplemented tools is broken on every first turn.** Add a smoke test: load the skill, list the tools it mentions in fenced code or by name, intersect with the actual tool registry, fail on mismatch.
- When prompt and code drift, the prompt is the surface the model sees first — and silently wrong prompts produce silent agent stalls, not crashes.
- Naming guidance: avoid coining new "verbs" (`invoke_product_manager`) when a generic primitive exists (`create_team` with a `skill="product_manager"` member).

## References

- Live code: `beidou/skills/coding/orchestrator/SKILL.md`.
- Related: 2026-04-26-skills-guessed-binding-choices.
