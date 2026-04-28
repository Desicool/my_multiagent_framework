# Spawned members never received the user task

**Resolved:** 2026-04-27 · **Refs:** commit `18e81df`

## Problem

In `tsk_ade6422a` (a real "calculator with React" run) spawned team members ran blind. The PM agent asked **generic discovery questions** ("what type of system?") even though the user had stated the actual ask in their initial message.

## Root cause

Three converging defects:

1. The orchestrator's `create_team` example in its SKILL.md hardcoded a meta-description like `"Gather requirements for the task..."` rather than embedding the user task. The role's `description` field never carried the user's actual request.
2. Worker SKILL.md bodies didn't reference `{role_description}` anywhere. Even if a meaningful description had been passed, it would never appear in the system prompt.
3. `spec.task` (the agent's first user message) was set to the team-level `task` arg from `create_team`, which the orchestrator wrote in its own words, not the user's.

Result: every spawned member started with zero context about what the user actually asked for.

## Fix

- Orchestrator gains `self._user_task`, captured at `run_root` time. Every `spawn_team` call now uses `self._user_task` as `spec.task` (the agent's first user-role message), so members always see the originating user request. The team-level `task` arg is still recorded on `TeamRecord` for orchestrator-internal coordination but is no longer the agent's first user message.
- Root's `template_vars["role_description"]` is now `""` (was `root_task` — that conflation was the original bug). Root has no role-specific scope; its scope **is** the user task.

## Decision / lesson

- **The user's words must propagate verbatim to spawned agents.** Paraphrasing through an orchestrator is lossy and feels uncanny when the spawned role asks about something the user already said.
- **Distinguish three task channels:** (a) the user's original request — verbatim, propagated to every agent's first user message; (b) the team's internal task description — for orchestrator coordination; (c) the role's `role_description` — for "you are the architect on this team" framing. Conflating any two breaks something.
- A field named `role_description` containing the entire user task is a category error — that's `user_task`, and the conflation will surface as a bug eventually.

## References

- Live code: `beidou/orchestrator.py:run_root`, `beidou/orchestrator.py:spawn_team`, `beidou/skills/loader.py` template vars.
- Related: 2026-04-27-question-routing-bypasses-chain, 2026-04-27-worker-skills-reask-upstream.
