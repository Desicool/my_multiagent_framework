# Root agent completion routed to /dev/null + free-text "approve" misread

**Resolved:** 2026-04-27 · **Refs:** commits `fe7dbce`, `a12c34e`, beads `my_simple_agent-xw4`

## Problem

Two related defects on the root agent's completion path:

1. The `on_report_status` `PostToolUse` hook returned early when `leader_id == USER_SENTINEL` and emitted `completion.empty(root_no_leader)`. The root agent's `[REVIEW REQUIRED]` envelope was dropped on the floor — the user had no way to approve or rework, and the run hung until the generic liveness watchdog escalated for an unrelated reason.
2. After fixing (1), the user's typed `approve` answer in the terminal still produced a `rework: approve` message instead of terminating root. The web banner's structured Approve/Rework buttons worked; typed text did not.

## Root cause

1. The early-return path treated "no leader" as "no one to review" and silently dropped the envelope. The user gateway was right there but never invoked.
2. `TerminalGateway` and `TUIGateway` round-trip user input as `selected_labels=[]` with the typed string in `text`. The root review hook only matched on `selected_values` / `selected_labels`, so a typed `approve` fell through to the rework branch and produced `"rework: approve"` as the next root inbox message.

## Fix

1. The hook now routes the root's review through the human gateway via `Orchestrator.gateway_ask_user_structured`, presenting Approve / Rework. Approve awaits `Orchestrator.terminate_root()` and the run unwinds via the existing terminate-sentinel path. Rework delivers a `from_id="user"`, `body="rework: …"` message to the root's own inbox so the next turn continues. A gateway exception falls back to `completion.empty(reason="gateway_failure: <ExcType>")` so the tool call does not deadlock.
2. Added a defensive text-keyword fallback (`approve`/`approved`/`yes`/`y`/`ok`/`lgtm`) in the **root branch only** when no machine discriminator is present, so the web banner's structured Approve/Rework still wins and terminal free-text approval also works. Test: `test_root_terminal_freetext_approve_still_terminates`.

Note also the `HookMatcher` 60 s default timeout had to be lifted to 30 minutes for both `AskUserQuestion` (PreToolUse) and `report_status` (PostToolUse) to allow human round-trips — see `2026-04-27-hookmatcher-60s-timeout.md`.

## Decision / lesson

- **"No leader" doesn't mean "no reviewer".** The user is the implicit reviewer of root; route there. Generalize: any code path that handles an "ownerless" entity needs to identify the implicit owner instead of returning early.
- **Multi-channel input needs multi-channel parsing.** When the same logical answer can arrive as a structured field, an array, or free text, every parser branch must handle every channel. The web banner happens to populate one field; the terminal happens to populate another. The root review code only handled one and silently routed the other to "rework".
- **Use machine discriminators when present, fall back to keywords only when absent.** Don't keyword-match on top of structured input — that creates ambiguity (the user's typed `"approve this PR"` would otherwise match approval keywords inside an unrelated rework note).

## References

- Live code: `beidou/sdk_agent.py:on_report_status` (root branch), `beidou/orchestrator.py:gateway_ask_user_structured`, `beidou/orchestrator.py:terminate_root`.
- Related: 2026-04-27-hookmatcher-60s-timeout, 2026-04-26-termination-review-gate-lifecycle.
