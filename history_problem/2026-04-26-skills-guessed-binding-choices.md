# Coding skills silently guessed binding choices

**Resolved:** 2026-04-26 · **Refs:** commit `9c1f1e0`

## Problem

In `tsk_30e790aa` the user said "用 react" (use React). The architect silently chose **single-file React 18 calculator (CDN, no build step)** and wrote it into `SPEC.md`. The PM sidetracked into the `frontend-design` skill instead of writing `requirements.md`. The user diagnosed: *"must let all agent ask about the unclear thing, don't let them guess anything."*

## Root cause

Decision-making roles' SKILL.md prompts had no explicit "ask before binding" rule. The model defaulted to plausible-looking choices (CDN, JS, no build) because that's what minimal example code looks like in training data. Once written into `SPEC.md`, the choice became binding — downstream roles built on it without questioning.

## Fix

Tighten every decision-making role's prompt with an Ambiguity escalation rule. Leaders use `ask_user(question, context_hint)` and **block**; members `send_message` to the leader chain. Writing an artifact (`requirements.md`, `SPEC.md`, `tasks.md`, code, deploy plan, qa verdict) that bakes in an unverified binding choice is a **contract violation**.

Per-skill changes:

- `software_architect`: new STEP 0 with concrete web-frontend examples (real scaffold vs CDN, TS vs JS, styling, build target).
- `product_manager`: strengthen step 5 — never write `Assumptions` for binding choices; explicitly forbid invoking other skills via the `Skill` tool (the original `frontend-design` detour).
- `junior_engineer` / `test_engineer` / `deployment_engineer` / `qa_engineer`: add Ambiguity escalation section. `qa` marks unresolved-ambiguity requirements as `BLOCKED`, not silently `PASS`.
- `orchestrator`: add Ambiguity routing section — when a member escalates, route through the team chain.

## Decision / lesson

- **The default behavior of an LLM is to plausibly fill gaps, not to ask.** Asking is a learned behavior that has to be installed in the prompt. "Don't guess" is the single most repeated guidance across coding skills for a reason.
- **Distinguish binding from non-binding choices.** Telling the model "ask about everything" produces noise. Telling it "ask about choices that go into a written artifact other roles will build on" produces useful asks.
- **`qa` marking unresolved ambiguity as `BLOCKED` (not silent `PASS`)** is a critical recovery channel. If discovery falls through, verification has to catch it.
- This change is prompt-only. Tightening prompts is the cheapest, fastest correction loop available; reach for it before changing primitives or runtime behavior.

## References

- Live code: `beidou/skills/coding/*/SKILL.md` (multiple files).
- Related: 2026-04-27-worker-skills-reask-upstream (the same lesson applied at a different layer).
