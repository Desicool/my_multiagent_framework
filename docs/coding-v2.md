# coding_v2 skill

## 1. Purpose

`coding_v2` is a two-phase software-build skill that adds a structured design
committee and a mandatory user-approval gate to the software-build flow. The
existing `coding/` skill (v1) has three gaps: no user-approval gate between
design and implementation (v1 begins implementation immediately after the
architect writes `SPEC.md`); no 3-round consensus mechanism for agent-vs-agent
disagreement (the architect resolves conflicts unilaterally); and overlapping
`ask_user` authority between PM and architect (both invoke the user for
product and technical questions without a clear ownership boundary). `coding_v2`
addresses all three without modifying v1.

---

## 2. Two-phase model

```
Phase 1: Design committee (flat peers under orchestrator)
┌────────────────────────────────────────────────────────┐
│  orchestrator (root, arbiter)                          │
│    │                                                   │
│    ├─ pm               → requirements.md (FR/NFR/AC/Stories)│
│    ├─ arch             → spec.md (modules/interfaces/data)  │
│    ├─ ui_ux            → ui_ux.md (+ optional mockups)      │
│    ├─ test             → test_plan.md (strategy/coverage)   │
│    ├─ qa               → qa_plan.md (acceptance gate criteria)│
│    └─ engineer_advisor → impl_plan.md (feasibility/complexity│
│                           /tech-debt/effort estimate)       │
│                                                        │
│   Members refine via send_message; track contested     │
│   points in design_issues/issue-{n}.md ledger.          │
│   At round=3 unresolved → opener escalates to orch.    │
│                                                        │
│   Convergence: all 6 in state=done AND no open issues  │
│   AND quiescence broadcast acknowledged.                │
└────────────────────────────────────────────────────────┘
                        ▼
        orchestrator presents design package to user
        via ask_user(Approve / Request Changes [+text])
                        │
            Approve ────┴──── Request Changes (with feedback)
                │                       │
                ▼                       ▼
        Phase 2 spawns          orchestrator broadcasts feedback
        (arch writes              to committee, increments
         tasks.md first)          design.iteration counter,
                                  another round begins
                        ▼
Phase 2: Implementation (reuse coding/ verbatim flow)
   arch writes tasks.md (post-approval bridge artifact)
   → impl (parallel per task) → test ∥ deploy → qa (APPROVED gate)
```

**Committee composition.** Six flat peers, all depth-1 members of the
orchestrator's team, all bound by the persistent-agent contract
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
(`beidou/skills/loader.py` — `DuplicateSkill` raised when two SKILL.md files
share a `name`); v1 and v2 must coexist under `beidou/skills/`.
`engineer_advisor` has no v1 sibling and so needs no suffix. The committee
members are forks rather than verbatim reuses because Phase 1 requires
committee protocol (`[FREEZE PROBE]` ack, `design_issues/issue-{n}.md`
ledger contributions, round-scoped `done`) that is absent from the v1 SKILL.md
bodies. Phase 2 still uses the verbatim v1 sub-skills (`test_engineer`,
`qa_engineer`, `junior_engineer`, `deployment_engineer`) — see §7.

All deliverables are written to `{project_workspace_path}/`. Six committee
members fit within the FAN_OUT_CAP of 8 (`docs/limits.md` §1). The design
committee operates at team depth 1; no sub-teams are created in Phase 1, so
depth never exceeds 1 during this phase (`docs/limits.md` §2).

**Phase 2** reuses the `coding/` flow verbatim from the `arch writes tasks.md`
step onward. See §7 for the bridge step and reuse details.

---

## 3. PM/architect boundary

Enforcement is **prompt-side** (persona instructions in each SKILL.md), not
primitive-level tool removal. Both skills retain `ask_user` in `allowed-tools`;
the restriction is a persona contract, auditable via `~/.beidou/events/{task_id}.jsonl`.

### Ownership table

| Domain | Owner | May call `ask_user` |
|---|---|---|
| Product features, user flows, success criteria, UX expectations, business constraints, scope decisions | pm | Yes — sole owner of all product/UX user-facing dialogue |
| Technical architecture, modules, interfaces, data models, dependencies, technical constraints, security boundaries | arch | Only for environment/library availability questions (e.g. "is library X available on this OS?") |
| Product or UX question that arrives in arch's context | pm (by routing) | arch MUST NOT ask user; MUST route to pm |

### Arch routing format (normative)

When arch encounters a product or UX question:

```
send_message(to=<pm_id>, content='need product clarification: <question>')
```

Arch then ends its turn and does not call `ask_user`. This is a prompt-side
invariant; violation is observable as an `ask_user` event from arch in the
event log.

### PM routing format

When pm needs a technical approach opinion from arch:

```
send_message(to=<arch_id>, content='product context: <feature> — please advise on tech approach')
```

---

## 4. Round/issue ledger

Contested design points are tracked in per-issue files rather than per-round
global state, because convergence on most points must not be blocked by one
unresolved disagreement.

### File location and schema

Each contested point lives at:

```
{project_workspace_path}/design_issues/issue-{n}.md
```

Required YAML frontmatter and section structure (reproduce verbatim in each
file):

```markdown
---
id: issue-{n}
opened_by: {agent_id}
parties: [{agent_a}, {agent_b}, ...]
topic: "<one-line summary>"
round: 1|2|3
status: open | resolved | escalated
---

## Round 1 — opened by {opener}
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
edits. This eliminates concurrent-write races.

### Update protocol

1. Opener creates the file with `status: open`, `round: 1`, and their
   initial position.
2. Other parties contribute via:
   `send_message(to=opener, content="[issue-{n} round-{k}] <argument>")`
3. Opener appends the counter-argument to the round section, bumps `round`.
4. An issue closes (`status: resolved`) when the opener and all listed parties
   each send:
   `send_message(to=opener, content="[issue-{n}] accept")`
5. Opener updates the `## Resolution` section and sets `status: resolved`.

### 3-round escalation

When `round` reaches 3 and the issue remains open, the opener:

1. Sets `status: escalated` in the file.
2. Sends: `send_message(to=orchestrator, content="[ESCALATE] issue=issue-{n}")`

Orchestrator reads the issue file, decides, writes the `## Resolution`
section, sets `status: resolved`, and broadcasts:

```
send_message(to=<all parties>, content="[issue-{n} ruling] <verdict>")
```

### "Done" is round-scoped, not permanent

A committee member calls `report_status(state="done", detail=...)` when their
deliverable is stable for the current round. If a peer's critique (arriving as
an inbox message in a subsequent round) forces revision, the member MUST revert
to `state="working"`, update their deliverable, and re-call
`report_status(state="done")`. The orchestrator tracks the **latest** state per
member; a stale `done` from an earlier round does not count toward convergence.

---

## 5. Convergence and freeze probe

Phase 1 is ready for the user approval gate when ALL three conditions hold
simultaneously:

1. Every committee member's latest `report_status` is `state="done"`.
2. `design_issues/` contains no file with `status: open` or `status: escalated`.
3. Orchestrator broadcasts a **freeze probe** to each member:
   `send_message(to=<each>, content="[FREEZE PROBE]")`
   and all six reply with `[FREEZE OK]` within a bounded quiescence window
   (implementation constant in `beidou/skills/coding_v2/orchestrator/SKILL.md`;
   not a `docs/limits.md` boundary).

On any `[FREEZE NACK]` reply, orchestrator cancels the probe and returns to
monitoring. The probe protects against a race where member A has reported
`done` but member B is mid-flight on a new critique that would un-converge A.

If a member is idle and fails to reply to the freeze probe within the window,
the orchestrator's liveness watchdog (Pass B, `docs/agent-runtime.md` §3.1)
will nudge it.

---

## 6. User approval gate

Once Phase 1 converges, orchestrator:

1. Reads all six design package files into a structured summary.
2. Calls `ask_user` presenting two options:
   - **Approve** — design is locked; proceed to Phase 2.
   - **Request Changes** — user provides feedback text.

**On Approve:**
- Orchestrator writes `{project_workspace_path}/design_locked.md` — a manifest
  containing the approved doc set (all six file paths), the current
  `design.iteration` count, and a timestamp.
- Phase 2 reads from this manifest, not from raw files, so any post-approval
  edits are detectable.

**On Request Changes:**
- Orchestrator parses the feedback and broadcasts it via `send_message` to each
  affected committee member.
- `design.iteration` counter increments.
- Committee members revert to `state="working"`.
- The design committee re-enters round 1 of the new iteration with a fresh
  `design_issues/` slate (prior issue files are not deleted but are no longer
  counted toward convergence).
- There is no cap on user-driven iteration rounds. The 3-round escalation rule
  applies only to agent-vs-agent disagreement, not to user feedback cycles.

`ask_user` has no timeout in Beidou (`docs/agent-runtime.md` §7). The
orchestrator parks on the gateway response and resumes when the user answers.

---

## 7. Phase 2 transition

Phase 2 begins after `design_locked.md` exists. Orchestrator executes a
bridge step before spawning the v1 implementation flow:

**Bridge step.** Orchestrator re-spawns `coding_v2/software_architect` with
task: "Write `tasks.md` from approved `spec.md` per `design_locked.md`."
Arch reads the approved `spec.md`, decomposes implementation into a task DAG,
and writes `tasks.md`. Arch reports done; orchestrator approves via
`terminate_child`. Only after this does the v1 impl flow begin.

`tasks.md` is a **post-approval bridge artifact**. Arch MUST NOT write
`tasks.md` during Phase 1 — that phase's sole arch deliverable is `spec.md`.

**Phase 2 task sequence (verbatim v1):**

```
arch (post-approval, writes tasks.md)
   ↓
impl (parallel spawn from tasks.md, junior_engineer per task)
   ↓
test ∥ deploy
   ↓
qa (APPROVED gate against full design package)
   ↓
orchestrator → user delivery
```

**Reused v1 sub-skills in Phase 2 (verbatim, no modifications):**

| Sub-skill | Phase 2 role |
|---|---|
| `coding/junior_engineer` | Implementer per task |
| `coding/test_engineer` | Test runner (writes test_report.md) |
| `coding/deployment_engineer` | Deployment plan |
| `coding/qa_engineer` | Final APPROVED/REJECTED sign-off (writes qa_report.md) |

The Phase-1 v2 forks (`test_engineer_v2`, `qa_engineer_v2`, `ui_ux_designer_v2`)
are committee-only and not used in Phase 2; their v1 counterparts above are the
Phase-2 actors.

**qa scope expansion.** In Phase 2, the qa_engineer's task description
includes all six design package paths:

```
requirements.md, spec.md, ui_ux.md, test_plan.md, qa_plan.md, impl_plan.md
```

The `coding/qa_engineer` skill body already handles "verify against the
package I'm given" — no SKILL.md modification is required. The v2 orchestrator
passes all six paths in the task field; the v1 qa skill consumes them.

---

## 8. Failure modes and recovery

Beidou's base recovery mechanisms (`docs/agent-runtime.md` §5, §5.1) apply
to all agents including committee members. This section states expected behavior
for failure modes specific to the Phase 1 design flow.

### Committee member crashes mid-round

The `agent-runtime.md` §5.1 crash recovery ladder applies: resume
(`session_id`) → fresh restart with recovery prompt referencing workspace →
escalate to orchestrator (the team leader). On restart, the member reads its
deliverable file from `{project_workspace_path}` and its pending inbox messages
(inbox persists in memory while the orchestrator process is alive; a fresh
restart after a process-level crash re-delivers messages from the orchestrator's
durable event log). The member re-enters the round at the latest round count in
its issue files.

### Opener crashes while an issue is open

On restart, the opener resumes as the file owner. The issue file remains at
`status: open` on disk; parties' in-flight arguments (if in the crasher's inbox
at crash time) are re-delivered on session resume if the orchestrator process is
still alive, or are lost on orchestrator crash. On orchestrator crash (see
below), parties re-send their arguments after the run is re-examined. Orchestrator
does not assign a new opener; issue ownership is immutable to the `opened_by`
field.

### Orchestrator crash mid-arbitration

The orchestrator process dying terminates the entire run (there is no hot
standby). Plan files persist under `~/.beidou/runs/` (`docs/orchestration.md`
§"Plan persistence") but live team graphs, inboxes, and SDK sessions are lost.
Issue resolution in flight is non-atomic across restarts. A re-run is required;
`design_issues/` files on disk provide an audit trail for the human to assess
what was resolved before the crash.

### User approval gate timeout

`ask_user` has no timeout (`docs/agent-runtime.md` §7). The orchestrator
parks indefinitely until the user responds. The watchdog does not fire on the
orchestrator itself while it is awaiting a gateway response (the agent is
not idle in the liveness sense — it is parked on an I/O future).

### Contract violation (member ends turn without progress)

The three-strike resume-not-terminate policy (`docs/agent-runtime.md` §5)
applies. After three consecutive violations by a committee member, Beidou
posts to the orchestrator recommending `terminate_child`. The orchestrator
decides whether to terminate and re-spawn or send a rework message.

---

## 9. Relationship to coding/v1

### What is reused

| Artifact | Reuse type |
|---|---|
| `coding/junior_engineer/SKILL.md` | Verbatim; Phase 2 only |
| `coding/test_engineer/SKILL.md` | Verbatim; Phase 2 test runner |
| `coding/qa_engineer/SKILL.md` | Verbatim; Phase 2 sign-off; v2 orchestrator expands task description to include all six design doc paths |
| `coding/deployment_engineer/SKILL.md` | Verbatim; Phase 2 only |
| `coding/ui_ux_designer/SKILL.md` | Verbatim; remains available as `ux_advisor` under v1's `coding/orchestrator` flow |
| `beidou/primitives/core.py` | Unchanged; no new primitives required |
| `beidou/skills/loader.py` | Unchanged; discovers new skill paths automatically |
| `docs/limits.md` | No boundary changes; 6 committee members < FAN_OUT_CAP=8, depth=1 |

### What is forked

| Artifact | Why forked |
|---|---|
| `coding_v2/orchestrator/SKILL.md` | Two-phase DAG, design-package approval gate, issue arbitration handler — structurally incompatible with v1 orchestrator |
| `coding_v2/product_manager/SKILL.md` | New "curious interviewer" persona; sole owner of `ask_user` for product/UX; v1 PM persona allows arch to also call `ask_user` |
| `coding_v2/software_architect/SKILL.md` | `ask_user` restricted to environment queries; product questions routed to PM; Phase 1 deliverable is `spec.md` only (no `tasks.md`) |
| `coding_v2/test_engineer/SKILL.md` | Phase-1 deliverable is `test_plan.md` (not test_report.md); committee-protocol participation (`[FREEZE PROBE]` ack, `design_issues/issue-{n}.md` ledger, round-scoped `done`) absent from v1 body |
| `coding_v2/qa_engineer/SKILL.md` | Phase-1 deliverable is `qa_plan.md` defining the acceptance gate (not the verdict); committee-protocol participation absent from v1 body |
| `coding_v2/ui_ux_designer/SKILL.md` | Phase-1 deliverable is `ui_ux.md` (not `UX_CONCERNS.md`); committee-protocol participation absent from v1 body; `huashu-design` mockup capability preserved |
| `coding_v2/engineer_advisor/SKILL.md` | New role; not present in v1 |

### Why no shared base

Forking is cheaper than mutating. A shared base skill combining v1 and v2
orchestrator logic would require conditional branching on the phase flag
throughout the body and would mutate v1 behavior under a common path — a
violation of the cohesion rule (`docs/README.md` §"Approval rule"). v2 is a
parallel option, not a replacement. v1 remains available for tasks where the
design committee overhead is unwarranted (small bug fixes, single-feature
changes, prototypes).
