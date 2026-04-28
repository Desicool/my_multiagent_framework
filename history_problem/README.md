# history_problem/ — Beidou postmortem archive

A local, **gitignored** archive of bugs, root causes, fixes, and decisions made during Beidou development. Curated from git history, beads issues, and trial-run JSONLs.

## Hard rule (also in `CLAUDE.md`)

> **Do NOT use code snippets from this directory as templates, examples, or reference implementations.** Snippets here describe what was wrong, what was almost shipped, or what got rewritten. Copying them re-introduces the bug they document.
>
> The current source of truth is the live code under `beidou/` and the specs under `docs/`. Always prefer those.
>
> You MAY read these files to understand prior context when investigating a related bug. Cite the file you read.

## Why gitignored, not a submodule

Three options were considered:

| Option | Verdict |
|---|---|
| In-tree, committed | Pollutes `git log`/`git blame`/grep results with stale code. Rejected. |
| In-tree, **gitignored** | Local to one checkout, zero pollution of the tracked codebase, instantly accessible to humans and agents. **Chosen.** |
| Git submodule (separate repo) | Justified only if multiple repos need to share these notes. Overkill today. Reconsider if the archive grows past ~50 entries or if a second project starts contributing. |

If you ever do promote this to a submodule, the layout here is already submodule-shaped: drop `README.md` and the `*.md` entries into a new repo, add it as a submodule at the same path, and the gitignore line keeps working as a fallback.

## Layout

Each entry is a single Markdown file named `YYYY-MM-DD-<slug>.md`. The date is when the problem was *resolved* (commit/close date), not when it was first observed.

Sections inside each entry:

```
## Problem
What broke, how it was observed, the smoking-gun trace if any.

## Root cause
The actual defect — usually one or two sentences once you know it.

## Fix
What was changed, with file paths to the live code (verify before reusing — the live code is canonical).

## Decision / lesson
The general principle worth remembering. This is the part that survives renames and refactors.

## References
Commit SHAs, beads IDs, related entries.
```

## Index

Grouped by theme. Newest first within each group.

### Wire-shape & field-name drift between layers
- [2026-04-26 — Frontend/backend event field-name mismatch](2026-04-26-event-field-name-mismatch.md)
- [2026-04-25 — `team_created` payload field names + missing root emit](2026-04-25-team-created-payload-and-root-emit.md)
- [2026-04-25 — `send_message` primitive never emitted observability event](2026-04-25-send-message-no-emit.md)
- [2026-04-26 — `caller_id` popped from emit_event kwargs](2026-04-26-caller-id-popped-from-kwargs.md)
- [2026-04-26 — `app.py` filtered on `type` instead of `event` key](2026-04-26-app-question-filter-wrong-key.md)

### SQLite migrations & primary keys
- [2026-04-25 — `_ensure_db` skipped migrations on existing DBs](2026-04-25-ensure-db-skips-migrations.md)
- [2026-04-25 — `teams` table single-column PK collided across tasks](2026-04-25-teams-pk-collision.md)

### Frontend reactivity & state hydration
- [2026-04-26 — Svelte 5 `$state` proxy bypassed via Map-stored refs](2026-04-26-svelte-state-proxy-bypass.md)
- [2026-04-26 — Page refresh skipped historical events on reconnect](2026-04-26-page-refresh-skips-history.md)

### Hooks, non-Claude models, and SDK quirks
- [2026-04-25 — Non-Claude models emit raw `AskUserQuestion`](2026-04-25-non-claude-askuserquestion.md)
- [2026-04-26 — Models without `TextBlock` strand the completion handoff](2026-04-26-textblock-less-completion-handoff.md)
- [2026-04-27 — `HookMatcher` silent 60 s timeout truncated human round-trips](2026-04-27-hookmatcher-60s-timeout.md)

### Completion review, termination, and watchdog
- [2026-04-26 — Termination & review-gate lifecycle (completion_pending)](2026-04-26-termination-review-gate-lifecycle.md)
- [2026-04-27 — Root agent completion routed to `/dev/null` and free-text "approve"](2026-04-27-root-completion-review-routing.md)

### Skills, prompts, and ask-don't-guess
- [2026-04-25 — Orchestrator SKILL.md called nonexistent `invoke_*` tools](2026-04-25-orchestrator-invoke-tools-missing.md)
- [2026-04-26 — Coding skills silently guessed binding choices](2026-04-26-skills-guessed-binding-choices.md)
- [2026-04-27 — Worker skills re-asked questions answered upstream](2026-04-27-worker-skills-reask-upstream.md)
- [2026-04-27 — Spawned members never received the user task](2026-04-27-spawned-members-no-user-task.md)
- [2026-04-27 — Question routing bypassed the leader chain](2026-04-27-question-routing-bypasses-chain.md)

### Operational footguns (from `bd memories`)
- [2026-04-27 — Parallel sub-agents clobber each other with git mutations](2026-04-27-parallel-subagents-git-clobber.md)
- [2026-04-27 — PyInstaller binary symlink rot after worktree changes](2026-04-27-pyinstaller-symlink-rot.md)

## Adding new entries

1. Pick the resolution date and a slug. Filename: `YYYY-MM-DD-<slug>.md`.
2. Use the section template above.
3. Add a one-line entry to the right group in this README.
4. The directory is gitignored, so nothing to commit — but copying this folder to a new checkout does not bring the entries with it. If you want true persistence, rsync `~/code/my_simple_agent/history_problem/` to a backup location periodically.
