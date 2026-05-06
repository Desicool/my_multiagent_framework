# coding_v2 skill

## 1. Purpose

`coding_v2` is a two-phase software-build skill that delegates each phase to a
dedicated coordinator: `phase1_coordinator` owns the design committee and user
approval gate; `phase2_coordinator` owns the Dev↔QA alternation loop and final
user confirmation. The root orchestrator is a thin sequencer — it declares the
two-task DAG and reviews coordinator completion.

The existing `coding/` skill (v1) has three gaps: no user-approval gate between
design and implementation; no 3-round consensus mechanism for agent-vs-agent
disagreement; and overlapping `ask_user` authority between PM and architect.
`coding_v2` addresses all three without modifying v1.

---

## 2. Two-coordinator model (v2.0.0)

```
orchestrator (root, thin sequencer)
  |
  +-- phase1_coordinator (depth 1) -- owns Phase 1 design committee
  |     |
  |     +-- pm               -> requirements.md
  |     +-- arch             -> spec.md
  |     +-- ui_ux            -> ui_ux.md
  |     +-- test             -> test_plan.md
  |     +-- qa               -> qa_plan.md
  |     +-- engineer_advisor -> impl_plan.md
  |     |
  |     Members refine via send_message; contested points tracked
  |     in design_issues/issue-{n}.md ledger.
  |     Round=3 unresolved -> opener escalates to coordinator.
  |     |
  |     Convergence: all 6 signal_review AND no open issues
  |     AND freeze probe acknowledged.
  |     |
  |     phase1_coordinator -> ask_user(Approve / Request Changes)
  |     Approve: design_locked.md, terminate committee, signal_review
  |     Request Changes: broadcast feedback, bump iteration, continue
  |
  +-- phase2_coordinator (depth 1) -- owns Phase 2 Dev<->QA loop
        |
        +-- arch_post_approval (writes tasks.md, one-shot)
        |
        +-- dev_team_leader (persistent, depth 2)
        |     +-- [junior_engineer x N, integrator] (per-iteration)
        |
        +-- qa_team_leader (persistent, depth 2)
        |     +-- [test_engineer, deploy_engineer, qa_engineer] (per-iteration)
        |
        Loop: dev_active -> [DEV READY] -> qa_active -> [QA VERDICT]
              APPROVED -> await_user -> user confirm
              REJECTED -> dev_active (rework)
        |
        phase2_coordinator owns phase2_state.json state machine.
        5-iteration cap; on cap exceeded -> ask_user.
```

**Committee composition (Phase 1).** Six flat peers, all depth-2 members of
the phase1_coordinator's team, bound by the persistent-agent contract
(`docs/agent-runtime.md` §2):

| Role | Skill path | Loader name | Phase 1 deliverable |
|---|---|---|---|
| pm | `coding_v2/product_manager` | `product_manager_v2` | `requirements.md` |
| arch | `coding_v2/software_architect` | `software_architect_v2` | `spec.md` |
| ui_ux | `coding_v2/ui_ux_designer` | `ui_ux_designer_v2` | `ui_ux.md` |
| test | `coding_v2/test_engineer` | `test_engineer_v2` | `test_plan.md` |
| qa | `coding_v2/qa_engineer` | `qa_engineer_v2` | `qa_plan.md` |
| engineer_advisor | `coding_v2/engineer_advisor` | `engineer_advisor` | `impl_plan.md` |

All six committee members are forks of (or new beside) the v1 sub-skills under
`coding/`. The `_v2` suffix is required by the loader's name-uniqueness rule
(`beidou/skills/loader.py`). `engineer_advisor` has no v1 sibling.

**Phase 2 teams.** `dev_team_leader` and `qa_team_leader` are new skills that
persist across iterations. They use `signal_review` (not `report_status(done)`)
for per-iteration completion signaling. Leaf workers (`junior_engineer`,
`test_engineer`, `qa_engineer`, `deployment_engineer`, `integrator`) are reused
verbatim from v1 / existing v2 forks.

---

## 3. PM/architect boundary

Enforcement is **prompt-side** (persona instructions in each SKILL.md), not
primitive-level tool removal. Both skills retain `ask_user` in `allowed-tools`;
the restriction is a persona contract, auditable via `~/.beidou/events/{task_id}.jsonl`.

### Ownership table

| Domain | Owner | May call `ask_user` |
|---|---|---|
| Product features, user flows, success criteria, UX expectations, business constraints, scope decisions | pm | Yes — sole owner of all product/UX user-facing dialogue |
| Technical architecture, modules, interfaces, data models, dependencies, technical constraints, security boundaries | arch | Only for environment/library availability questions |
| Product or UX question that arrives in arch's context | pm (by routing) | arch MUST NOT ask user; MUST route to pm |

### Arch routing format

```
send_message(to=<pm_id>, content='need product clarification: <question>')
```

### PM routing format

```
send_message(to=<arch_id>, content='product context: <feature> -- please advise on tech approach')
```

---

## 4. Round/issue ledger

Contested design points are tracked in per-issue files rather than per-round
global state.

### File location and schema

```
{project_workspace_path}/design_issues/issue-{n}.md
```

Required YAML frontmatter:

```markdown
---
id: issue-{n}
opened_by: {agent_id}
parties: [{agent_a}, {agent_b}, ...]
topic: "<one-line summary>"
round: 1|2|3
status: open | resolved | escalated
---

## Round 1 -- opened by {opener}
**Position:** ...
**Counter ({party}):** ...

## Round 2
...

## Resolution
(filled when status=resolved)
```

### Ownership rule

The issue file is owned by the `opened_by` agent. **Only the opener writes
the file.** Other parties contribute arguments via message, not direct file
edits.

### Update protocol

1. Opener creates the file with `status: open`, `round: 1`, and their initial position.
2. Other parties contribute via: `send_message(to=opener, content="[issue-{n} round-{k}] <argument>")`
3. Opener appends the counter-argument to the round section, bumps `round`.
4. An issue closes when the opener and all listed parties each send: `send_message(to=opener, content="[issue-{n}] accept")`
5. Opener updates the `## Resolution` section and sets `status: resolved`.

### 3-round escalation

When `round` reaches 3 and the issue remains open, the opener:

1. Sets `status: escalated` in the file.
2. Sends: `send_message(to=coordinator, content="[ESCALATE] issue=issue-{n}")`

The phase1_coordinator reads the issue file, decides, writes the `## Resolution`
section, sets `status: resolved`, and broadcasts:

```
send_message(to=<all parties>, content="[issue-{n} ruling] <verdict>")
```

### "Done" is round-scoped, not permanent

A committee member calls `signal_review(detail="[REVIEW REQUIRED] ...")` when
their deliverable is stable for the current round. If a peer's critique forces
revision, the member updates their deliverable and re-calls `signal_review`.
The phase1_coordinator tracks the **latest** signal per member; a stale signal
from an earlier round does not count toward convergence.

---

## 5. Convergence and freeze probe

Phase 1 is ready for the user approval gate when ALL three conditions hold:

1. Every committee member has called `signal_review` (review_pending=True).
2. `design_issues/` contains no file with `status: open` or `status: escalated`.
3. The phase1_coordinator broadcasts a **freeze probe** to each member:
   `send_message(to=<each>, content="[FREEZE PROBE]")`
   and all six reply with `[FREEZE OK]`.

On any `[FREEZE NACK]` reply, the coordinator cancels the probe and returns to
monitoring.

---

## 6. User approval gate

Once Phase 1 converges, the **phase1_coordinator** (not the root orchestrator)
presents the design package to the user:

1. Reads all six design package files into a structured summary.
2. Calls `ask_user` presenting two options:
   - **Approve** -- design is locked; proceed to Phase 2.
   - **Request Changes** -- user provides feedback text.

**On Approve:**
- phase1_coordinator writes `{project_workspace_path}/design_locked.md` — a
  manifest containing the approved doc set (all six file paths), the current
  `design.iteration` count, and a timestamp.
- Terminates all six committee members.
- Calls `signal_review(detail="[REVIEW REQUIRED] ...")` — this signals the
  root orchestrator that Phase 1 is done.

**On Request Changes:**
- phase1_coordinator parses the feedback and broadcasts it via `send_message`
  to each affected committee member.
- `design.iteration` counter increments.
- Committee reverts to working state.
- Fresh `design_issues/` slate for the new iteration.

### 6.1 Phase1_coordinator inbox question contract

Member `ask_user` calls land in the coordinator's inbox first as
`[INBOX QUESTION]` system messages. **The phase1_coordinator ALWAYS escalates
to the user via `escalate_question`** — it never calls `answer_question`.
This contract is identical to the v1 orchestrator's contract (see `docs/coding-v2.md`
v1.x). Rationale: v2's value proposition is the user-approved design gate; a
coordinator who silently substitutes its own judgment short-circuits that gate.

---

## 7. Phase 2 transition

Phase 2 begins after `design_locked.md` exists. The root orchestrator terminates
phase1_coordinator and spawns phase2_coordinator. Phase 2 is fully delegated to
the coordinator — the root orchestrator only sees the final result.

### 7.1 Bridge step — arch writes tasks.md

phase2_coordinator spawns `software_architect_v2` as `arch_post_approval` (one-shot
task). Arch reads `design_locked.md` and `spec.md`, decomposes implementation
into a task DAG per the `tasks.md` manifest schema (§7.2), and calls
`signal_review`. Coordinator terminates arch on approval.

### 7.2 tasks.md schema (manifest-bearing)

Each task entry MUST conform to:

```markdown
## task-{id}: {short name}
- What: one-sentence deliverable
- Inputs: logical paths produced by upstream tasks (or "none")
- Outputs:
    - <logical/path/from/project/root.ext>
- Generated: <list of files this task regenerates each run, or "none">
- Deletes: <list of files this task removes from integration, or "none">
- Runs_before: [task-id-1, task-id-2]
- Verify: <bash command exiting 0 if the task's artifacts are complete>
```

**Outputs are logical paths**, relative to the project root. The implementer
for `task-1` writes `src/foo.py` to `artifacts/task-1/src/foo.py`. The integrator
later moves it to `integration/src/foo.py`. No two tasks may claim the same logical
path. `Generated` annotates byproducts (lockfiles, compiled assets). `Deletes`
annotates files the task removes from `integration/`. `Runs_before` declares
ordering edges for topological sort. `Verify` is a post-integration self-check
executed against `integration/`.

### 7.3 task-deps convention

Shared utility modules (types, config, common interfaces) belong to a
designated `task-deps` task that runs first. Downstream tasks reference
`task-deps` Outputs in their `Inputs` and list `task-deps` in `Runs_before`.

### 7.4 The integrator agent

`coding_v2/integrator` runs inside dev_team_leader's sub-plan after all
implementation tasks complete. Its workflow:

1. Read `tasks.md`. Parse manifest map + DAG.
2. **Validate**: no path overlap, no Runs_before cycles.
3. **Build fresh `integration/`** — delete prior tree, recreate.
4. **Place artifacts** in topological order per Runs_before.
5. **Write `integration_report.md`** — audit trail ending with `STATUS: COMPLETE`.
6. On error: write diagnostic, send `[INT-CONFLICT]` to dev_team_leader.

The integrator is **structural**: it never reads file contents.

### 7.5 Phase 2 Dev↔QA loop

```
                 phase2_coordinator
                       |
         +-------------+-------------+
         |                           |
   dev_team_leader             qa_team_leader
   (persistent)                (persistent)
         |                           |
    [DEV START]                  [QA START]
         |                           |
    impl tasks (N)              test_engineer
    integrator                  deploy_engineer
         |                           |
    [DEV READY]  ------>>     [QA VERDICT]
    signal_review              signal_review
         ^                           |
         |                           v
         +---- [REWORK] ---- REJECTED
                                  |
                               APPROVED
                                  |
                              ask_user(final confirm)
                                  |
                              terminate both leaders
```

**State machine** owned by phase2_coordinator in `phase2_state.json`:

```
bootstrapping -> dev_active -> qa_active -> await_user -> approved | aborted
```

**cycle_id**: Each iteration increment generates a new `cycle_id`. All messages
must carry it. Stale messages (mismatched cycle_id) are silently dropped.

**ACK protocol**: `[DEV START]`, `[QA START]`, and `[REWORK]` messages all
require an ACK from the receiving leader before the coordinator advances state.

**Iteration cap**: 5 iterations max. On cap exceeded, phase2_coordinator calls
`ask_user` with options: Continue (5 more), Abort (partial delivery), or
Provide guidance.

### 7.6 Reused sub-skills in Phase 2

| Sub-skill | Phase 2 role |
|---|---|
| `coding/junior_engineer` | Implementer per task; writes to `artifacts/task-{n}/` |
| `coding/test_engineer` | Test runner against `integration/`; writes `test_report.md` |
| `coding/deployment_engineer` | Deployment plan referencing `integration/` |
| `coding/qa_engineer` | Final APPROVED/REJECTED sign-off; writes `qa_report.md` |
| `coding_v2/integrator` | Manifest validator + structural assembler |

The Phase-1 v2 forks (`test_engineer_v2`, `qa_engineer_v2`, `ui_ux_designer_v2`)
are committee-only and not used in Phase 2.

### 7.7 On QA REJECTED — re-run policy

Rejection triggers a fresh Dev cycle without re-spawning arch_post_approval:

1. phase2_coordinator reads `qa_report.md` for rejection reasons.
2. Increments iteration, generates new cycle_id, updates `phase2_state.json` to
   `dev_active`.
3. Sends `[REWORK] iteration=N+1 cycle_id=NEW` to dev_team_leader with QA feedback.
4. dev_team_leader calls `remove_plan`, declares fresh sub-plan, re-runs
   affected implementation tasks + integrator.
5. Integrator deletes and rebuilds `integration/` from scratch (fresh-tree rule).
6. Coordinator sends `[QA START]` to qa_team_leader when `[DEV READY]` arrives.

For integrator conflicts (path overlap / cycle), dev_team_leader escalates
`[INT-CONFLICT]` to the coordinator, which may re-spawn arch_post_approval or
consult the user.

---

## 8. Failure modes and recovery

Beidou's base recovery mechanisms (`docs/agent-runtime.md` §5, §5.1) apply to
all agents. This section covers coding_v2-specific failure modes.

### Phase1_coordinator crash

The coordinator process dying terminates the entire run (no hot standby).
`design_issues/` files on disk provide an audit trail. A re-run is required;
the root orchestrator re-spawns phase1_coordinator, which can read existing
deliverable files from `{project_workspace_path}`.

### Phase2_coordinator crash

Same as above. `phase2_state.json` and `qa_report.md` on disk provide partial
state. Recovery: re-run from root orchestrator, re-spawn phase2_coordinator.
The phase2_coordinator reads `design_locked.md` and existing artifacts to
determine whether to bootstrap fresh or resume a partial loop.

### Committee member crashes mid-round

The `agent-runtime.md` §5.1 crash recovery ladder applies. On restart, the
member reads its deliverable file and pending inbox messages, re-entering at
the latest round count.

### Dev_team_leader / qa_team_leader crash mid-iteration

The phase2_coordinator's watchdog detects a stalled leader (not responding to
pings). Coordinator sends a liveness probe. On timeout: `ask_user` consultation
— re-spawn the leader (losing iteration context) or abort.

### Integrator detects manifest violation

Same as v1.x: diagnostic in `integration_report.md`, `[INT-CONFLICT]` sent to
dev_team_leader. dev_team_leader tries to fix missing outputs locally; structural
issues (path overlap, cycle) escalate to phase2_coordinator.

### User approval gate timeout

`ask_user` has no timeout (`docs/agent-runtime.md` §7). Both coordinators park
indefinitely until the user responds.

---

## 9. Relationship to coding/v1

### What is reused verbatim

| Artifact | Reuse type |
|---|---|
| `coding/junior_engineer/SKILL.md` | Verbatim; Phase 2 leaf worker |
| `coding/test_engineer/SKILL.md` | Verbatim; Phase 2 test runner |
| `coding/qa_engineer/SKILL.md` | Verbatim; Phase 2 sign-off |
| `coding/deployment_engineer/SKILL.md` | Verbatim; Phase 2 only |
| `beidou/primitives/core.py` | Extended (added `signal_review`, `request_termination`) |
| `beidou/skills/loader.py` | Unchanged; discovers new skill paths automatically |

### What is forked (v2-specific)

| Artifact | Why forked |
|---|---|
| `coding_v2/orchestrator/SKILL.md` | v2.0.0: thin sequencer delegating to coordinators |
| `coding_v2/phase1_coordinator/SKILL.md` | **New.** Phase 1 committee manager + user approval gate |
| `coding_v2/phase2_coordinator/SKILL.md` | **New.** Phase 2 Dev↔QA loop + state machine |
| `coding_v2/dev_team_leader/SKILL.md` | **New.** Persistent impl+integrator leader |
| `coding_v2/qa_team_leader/SKILL.md` | **New.** Persistent test+deploy+qa leader |
| `coding_v2/product_manager/SKILL.md` | v2 PM persona; sole owner of `ask_user` for product/UX |
| `coding_v2/software_architect/SKILL.md` | `ask_user` restricted; Phase 1 deliverable `spec.md` only |
| `coding_v2/test_engineer/SKILL.md` | Phase-1 deliverable `test_plan.md`; committee protocol |
| `coding_v2/qa_engineer/SKILL.md` | Phase-1 deliverable `qa_plan.md`; committee protocol |
| `coding_v2/ui_ux_designer/SKILL.md` | Phase-1 deliverable `ui_ux.md`; committee protocol |
| `coding_v2/engineer_advisor/SKILL.md` | New role; not present in v1 |
| `coding_v2/integrator/SKILL.md` | New role; Phase-2 manifest assembler |
| `coding_v2/junior_engineer/SKILL.md` | Phase-2 impl-leader (delegates, never codes) |

### New primitives (used by v2 coordinators)

| Primitive | Description |
|---|---|
| `signal_review` | Signals review-ready; reentrant, no plan check. Replaces `report_status(done)`. |
| `request_termination` | Requests lifecycle end; checks plan completion. |

`signal_review(detail=...)` is deprecated and mapped to `signal_review` with
backward-compatible semantics via a compatibility shim.

### Why no shared base

Forking is cheaper than mutating. v2 is a parallel option, not a replacement.
v1 remains available for small bug fixes, single-feature changes, and prototypes
where the design committee overhead is unwarranted.

### Depth budget

v2.0.0 topology: `orch(0) -> coordinator(1) -> team_leader(2) -> worker(3) -> sub(4)`.
MAX_DEPTH raised from 5 to 8 to accommodate this plus occasional deep sub-delegation.
Depth > 6 emits a `depth_warning` event for monitoring.
