# Harness contract drift: hook-synth, silent SDK blocks, and stale prompt claims

**Resolved:** 2026-05-05 · **Refs:** beads `my_simple_agent-oigt`, commits `4e2ffd0..d099e37` (7 atomic), plan `~/.claude/plans/skill-harness-skill-pair-bug-error-mess-merry-frost.md`

## Problem

Commit `82d5290` (`report_status: validate [REVIEW REQUIRED] envelope at primitive; strip synth from hook`) fixed one instance of a recurring bug class:

> A hook synthesises a degraded payload and overwrites the primitive's rich one; downstream logic acts on the degraded copy.

After that fix, an audit of the rest of the harness surfaced sibling failures of the same shape, plus two related drift modes:

1. **Same-class hook-synth bug elsewhere.** `on_ask_user_question` (PreToolUse, `beidou/agent/hooks.py:147-215`) intercepted raw `AskUserQuestion`, called `gateway_ask_user_structured` directly, and synthesised fake `tool_called`/`tool_result` events with the user's answer threaded back as a `permissionDecisionReason`. Two harms: (a) it was the same hook-synth pattern `82d5290` removed elsewhere, (b) it bypassed `gateway_ask_via_chain` (the leader chain), so every raw AskUserQuestion surfaced straight to the user even when a leader could have answered locally — the very bypass `2026-04-27-question-routing-bypasses-chain` had closed for the primitive path, still open at the SDK-builtin shim.
2. **Silent SDK suppression.** `disallowed_tools=["SendMessage", "TodoWrite"]` in `beidou/agent/loop.py:258` filtered shadow tools out of the model's tool list. When the model still attempted them (or future SDK regressions resurfaced them) it got nothing back — no `tool_result`, no error, no signal. Worse than the `82d5290` synthesised-payload bug because it left the agent guessing why a call vanished.
3. **Declared-but-unenforced contracts.** `_CONTRACT_BLOCK` in `beidou/agent/prompts.py` still claimed "Beidou forwards exactly that text [the final assistant message] to your leader as the completion report" — but `82d5290` made the runtime forward only the `[REVIEW REQUIRED]` envelope from `tool_input["detail"]`. The prompt now contradicted the runtime, the tests, and `docs/agent-runtime.md:182` ("there is no fallback to assistant text"). Reverse drift: the reply-obligation contract enforced by `on_reply_gate` was code-only, mentioned nowhere in `prompts.py` or any spec.
4. **Missing spec pointers in `PrimitiveError`.** Out of 49 `PrimitiveError` raises in `beidou/primitives/core.py`, only 4 cited a `docs/*.md#section`. An agent that hit `unknown_recipient` or `not_holder` saw a one-line message with no canonical reference to look up the contract.
5. **Inconsistent nudge text.** Loop / orchestrator-side text injection (completion-handoff repair nudge, reply-obligation post-turn nudge, contract-violation strike resume, leader escalation) was actionable but never pointed at the doc section that carries the canonical contract.
6. **`docs/skill-modules.md` infrastructure existed but had zero shipped users.** `HookRegistry` parsed `module.toml` and the loop merged its hooks into the SDK matchers; the loader copied sidecar files. But no bundled skill exercised the mechanism, so the "skill-specific contract → skill-local code" half of the design was prompt-only across the codebase.

## Root cause

One unifying root cause: **no enforcement that contracts have a single source AND a paired code validator.** The 82d5290 fix established the pattern (contract in `prompts.py`, validation in `primitives/core.py` via `PrimitiveError`, error text references the spec) but left the rest of the harness pre-pattern. Several feature points predated it; some others (skill-modules) had infra but no first user.

Specific roots, per failure:

- (1) `on_ask_user_question` was a side-channel that grew alongside the primitive, not in step with it. The chain-routing fix in `2026-04-27-question-routing-bypasses-chain` carved an explicit exception for "SDK-builtin shim", which then never closed.
- (2) `disallowed_tools` is an SDK-level filter that silently removes tools from the model's surface. There was no observable signal when it triggered; the comment chain in `loop.py:232-258` documented it as defense-in-depth but the visibility half was missing.
- (3) The "MUST emit a final assistant message" line was load-bearing pre-`82d5290` (the hook used to read assistant text). After 82d5290 it was a stale claim, not removed in the same commit.
- (4) Pure ergonomics drift — early `PrimitiveError` callsites omitted spec references; `82d5290`'s `envelope_missing` set the gold pattern but only for one site.
- (5) Same: nudges predated the pattern.
- (6) `docs/skill-modules.md` was specced before any concrete need; "two coexisting" (framework contracts in prompts.py + primitive vs skill contracts in `module.toml` + `gate.py`) was the design intent but the second track had no exemplar.

## Fix

Seven atomic commits (`4e2ffd0..d099e37`), implementation order chosen by ascending blast radius. Plan B was approved after a copilot-CLI review (gpt-5.4 --xhigh) that flagged five corrections folded back into the plan, and an advisor pass for sequencing + test coverage.

| Commit | Touches | What |
|---|---|---|
| `4e2ffd0` | `prompts.py`, `examples/custom-skill/SKILL.md`, `tests/test_skills_loader.py` | Delete the stale "Beidou forwards exactly that text" paragraph; envelope-in-detail is the single source. |
| `b6d4fc2` | `primitives/core.py` | Append `Spec: docs/*.md#section` (and actionable suggestions like "use list_peers") to actionable PrimitiveErrors. Skip mechanical schema-validation variants whose message is self-explanatory. |
| `2fae5ef` | `prompts.py`, `docs/agent-runtime.md` | Add `[REPLY OBLIGATION]` block to `_CONTRACT_BLOCK`; document the gate, allowlist, and `reply_gate.denied` event in `agent-runtime.md` §3.1. |
| `3bf3ecb` | `agent/hooks.py`, `tests/test_sdk_agent_hooks.py`, `docs/{agent-runtime,tool-surface,observability,skill-modules}.md` | `on_ask_user_question` becomes redirect-only deny; no gateway call, no synth tool pair. New `ask_user_question.redirected` event for observability. 7 old synth-behaviour tests deleted, 4 new redirect-behaviour tests added. |
| `8a241d1` | `agent/hooks.py`, `agent/prompts.py`, `tests/test_disallowed_alias_hook.py` (new), `docs/{tool-surface,skills,observability,skill-modules}.md` | New `on_disallowed_alias` PreToolUse hook (per-tool matchers for `SendMessage`/`TodoWrite`). SDK `disallowed_tools` left intact; the hook is a backstop that converts the would-be silent SDK drop into a model-visible deny+reason. New `[FORBIDDEN TOOLS]` prompt block. New `disallowed_alias.denied` event. |
| `8be664c` | `harness.py`, `agent/loop.py`, `orchestrator.py` | Wording updates to nudge/repair/strike injection texts. Each now points at the canonical spec section. |
| `d099e37` | `beidou/skills/coding_v2/product_manager/{module.toml,gate.py}` (new), `tests/skills/test_pm_v2_gate.py` (new), `tests/test_skills_loader.py` | First skill-modules pilot. `validate_tool_call` gate enforces the §1 Core DOs "AT LEAST 2 ask_user rounds before report_status(done)" contract for `product_manager_v2`. Three loader tests scoped marker assertions to SKILL.md only (sidecars are byte-copied, no marker). |

End state: 326 unit tests passing (was 312 at start). End-to-end smoke deferred until a model API session is available.

## Decision / lesson

- **One feature point = one prompt-side contract declaration + one code-side validator that emits the right error message.** The two halves must ship together. A contract without a validator is decorative; a validator without a contract leaves the agent reverse-engineering errors at runtime. `82d5290` `envelope_missing` is the canonical pair — every new harness feature should match its shape.
- **Hook-synthesised payloads are an antipattern in the same family across the surface.** `82d5290` killed one instance, this audit killed the second (AskUserQuestion). Whenever a hook returns content the model will read, the *primitive* should be the only place that content is composed; the hook either passes it through or denies. Synthesising in the hook re-introduces the bug class from a different angle.
- **Silent SDK filters are worse than synthesised payloads.** A wrong payload at least gives the agent something to react to; a silently-dropped tool call gives nothing. If you must drop, also surface the drop — defense-in-depth at the *visibility* layer matters as much as at the enforcement layer.
- **Spec references in error messages are pure win.** Marginal cost is one line per PrimitiveError; benefit is the agent (and any future debugger) immediately knows where to read the contract instead of grepping.
- **"Skill-specific" vs "framework-wide" contracts deserve different homes.** Framework contracts (envelope, persistent-agent, reply obligation) belong in `prompts.py` + a primitive. Skill-specific contracts (e.g. PM's interview discipline) belong in `module.toml` + `gate.py` next to the SKILL.md, so the contract and its enforcement live in the same directory and travel together. Both halves of `docs/skill-modules.md` should ship at the same cadence as the prompts.
- **Defer scope-expanding spec changes to their own plan.** The audit found a third hook-synth instance — the v2 design-committee FREEZE protocol (`loop.py:839-866`). Fixing it requires a new primitive (or new `send_message` `kind`) which crosses the `docs/README.md` Approval rule #2 boundary. Splitting it into bd `my_simple_agent-13sp` kept the audit shippable; the FREEZE work waits for its own plan + design discussion. Same call for the four un-wired skill-modules hook points (bd `my_simple_agent-ywxp`).
- **Run a 2nd-AI review (copilot CLI gpt-5.4 --xhigh) before approving non-trivial plans.** Copilot caught five concrete plan errors that a single-pass review missed: Phase 2 was a regression, Phase 4 needed a different decision, Phase 6 misstated infra status, and most importantly Phase 6.5.1 (FREEZE — same bug class) was missing entirely from the audit. The advisor pass after that caught a sequencing gap and a test-coverage gap.

## References

- Live code: `beidou/agent/hooks.py:on_ask_user_question`, `beidou/agent/hooks.py:on_disallowed_alias`, `beidou/agent/prompts.py:_CONTRACT_BLOCK`, `beidou/primitives/core.py` (PrimitiveError sites), `beidou/skills/coding_v2/product_manager/{module.toml,gate.py}`, `beidou/harness.py:COMPLETION_HANDOFF_NUDGE`.
- Specs aligned in same commits: `docs/tool-surface.md#ask_user`, `docs/tool-surface.md#forbidden-tools`, `docs/agent-runtime.md` §3.1 (Reply obligation), `docs/observability.md` (`ask_user_question.redirected`, `disallowed_alias.denied`), `docs/skills.md` (third defense layer), `docs/skill-modules.md` (event list).
- Plan: `~/.claude/plans/skill-harness-skill-pair-bug-error-mess-merry-frost.md`.
- Deferred: bd `my_simple_agent-13sp` (FREEZE primitive refactor, tool-surface approval needed), bd `my_simple_agent-ywxp` (wire remaining 4 skill-modules hook points).
- Related: `2026-04-27-question-routing-bypasses-chain` (closed the primitive-path bypass; this audit closed the SDK-builtin-shim bypass), `2026-04-26-textblock-less-completion-handoff` (introduced the harness nudge that this audit polished), `2026-04-25-non-claude-askuserquestion` (sibling provider-leakage bug class).
