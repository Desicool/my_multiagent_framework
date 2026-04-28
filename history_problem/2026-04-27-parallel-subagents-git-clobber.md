# Parallel sub-agents clobber each other with git mutations

**Resolved:** 2026-04-27 · **Source:** `bd remember beidou-parallel-subagent-risk-agents-may-run-git`

## Problem

On 2026-04-27, two parallel Beidou sub-agents shared a working tree. The Step 2 backend agent ran `git status; git diff; git reset` to clean its own diff and **wiped a Step 1 SKILL.md edit done by a sibling agent in the same commit window**. The wiped agent had not yet committed its work.

## Root cause

Sub-agents dispatched via the Agent tool (or Beidou's own `create_team`) share a working directory by default. Any agent that runs `git stash`, `git reset`, `git checkout --`, or `git restore .` while a sibling has uncommitted changes will obliterate those changes silently. Git itself reports nothing because as far as git is concerned, the cleanup was authorized — there is no "sibling agent" concept.

## Fix

No code change. Mitigations are operational:

- **Serialize agents that share a working tree.** Don't dispatch multiple write-capable agents into the same checkout in parallel.
- **OR explicitly forbid git mutations in the agent prompt** — no `git stash`, `git reset`, `git checkout`, `git restore`. Agents that need a clean diff should make a backup and revert manually, not rely on git's destructive verbs.
- **Use git worktrees for true parallelism** — each agent gets its own working tree, sharing only the bare object store.

## Decision / lesson

- **Git's mutation verbs are not safe for concurrent use within one working tree.** They were designed for one human at one terminal.
- A prompt-level "do not run destructive git" rule is cheaper than process-level isolation but is only as good as the model's adherence. For high-stakes work, isolate via worktrees.
- When you discover this happened, look at `git fsck --lost-found` and `git reflog` of every branch the sibling might have touched. The reflog can sometimes recover commits even if the working tree is gone.

## References

- Memory: `bd memories beidou-parallel-subagent-risk-agents-may-run-git`.
- Live mitigation: every agent prompt that runs in a shared checkout should ban destructive git verbs.
