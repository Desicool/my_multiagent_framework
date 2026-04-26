# Tool Surface

Canonical spec of every MCP tool an agent sees. All tools are exposed under
the `beidou` server, so the agent-visible name is `mcp__beidou__<name>`.

Every agent sees the **same** tool list. There is no worker/leader tool-set
split. "Leader" is a relational fact Beidou tracks, not a permission tier.
Permission-style checks (e.g. `terminate_child` leadership check) are
enforced at call time by the primitive implementation.

All primitives receive an orchestrator-bound `caller_id` via the per-spawn
MCP closure. `caller_id` is NEVER read from the model's tool input.

## Index

| Tool (mcp__beidou__...) | Kind | Blocking? |
|---|---|---|
| `send_message` | A2A | No |
| `list_peers` | Agent-Beidou | No |
| `ask_user` | Agent-Beidou | Yes (human-bounded) |
| `report_status` | Agent-Beidou | No |
| `create_team` | Agent-Beidou | No |
| `terminate_child` | Agent-Beidou | No |
| `list_pending_reviews` | Agent-Beidou | No |

---

## send_message

**Kind:** A2A (agent to agent). The only true A2A primitive.

**Input schema**
| Field | Type | Required | Notes |
|---|---|---|---|
| `to` | string (agent_id) | yes | Recipient's agent id (must exist in the same task). |
| `content` | string | yes | Message body. No length cap in schema (but inbox cap applies per recipient, see `limits.md`). |

**Output schema**
```
{ "delivered": true, "message_id": "<uuid>" }
```

**Error cases**
- `unknown_recipient`: `to` does not resolve to a live agent in this task.
- `inbox_full`: recipient's inbox has reached the cap in `limits.md`. Returned
  as a structured tool error; sender decides how to react.
- `task_mismatch`: `to` resolves but belongs to a different task.

---

## list_peers

**Kind:** Agent-Beidou. Reads the team graph.

**Input schema**
| Field | Type | Required | Notes |
|---|---|---|---|
| `scope` | string enum | no | One of: `team` (default; direct teammates), `children` (direct reports of teams the caller leads), `all` (entire task graph). |

**Output schema**
```
{ "peers": [ {"agent_id": "...", "role": "...", "team_id": "...", "status": "...", "is_leader_of": ["<team_id>", ...]}, ... ] }
```

**Error cases:** none under normal operation.

---

## ask_user

**Kind:** Agent-Beidou. Routes the question to Beidou's human gateway
(`beidou/gateway/*`).

**Input schema**
| Field | Type | Required | Notes |
|---|---|---|---|
| `question` | string | yes | The question. |
| `context` | string | no | Optional background. |

**Output schema**
```
{ "answer": "<user response>" }
```

**Error cases**
- `gateway_unavailable`: no human gateway registered. Returns structured
  error; agent decides whether to fall back or block.
- `user_declined`: user explicitly refused. Returns the refusal as a
  structured error.

ask_user blocks indefinitely until the user (or an escalating leader via the inbox question broker) supplies an answer. There is no timeout.

---

## report_status

**Kind:** Agent-Beidou. Pushes a state update into observability and may
trigger liveness evaluation.

**Input schema**
| Field | Type | Required | Notes |
|---|---|---|---|
| `state` | string enum | yes | One of: `working`, `idle`, `blocked`, `done`. |
| `detail` | string | no | Free-text summary. Required when `state="done"` (in practice). |

**Output schema**
```
{ "recorded": true }
```

**Error cases**
- `invalid_state`: `state` not in the enum above.

**Side effects**
- Emits a `status` event to the observability sinks.
- If `state="done"`, triggers a liveness re-evaluation on the caller's
  parent leader (see `orchestration.md`).

---

## create_team

**Kind:** Agent-Beidou. Spawns a sub-team. The caller becomes its leader by
construction.

**Input schema**
| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Human-readable team name. |
| `task` | string | yes | Task description propagated to each member. |
| `roles` | list[object] | yes | One per member. Each has `role` (string), `skill` (skill name, e.g. `junior_engineer`), `model` (optional string), `description` (string). |
| `rules` | list[string] | no | Coordination rules visible to each member. |

**Output schema**
```
{ "team_id": "<id>", "members": [{"agent_id": "...", "role": "..."}, ...] }
```

**Error cases**
- `fanout_exceeded`: `len(roles)` exceeds cap in `limits.md`.
- `depth_exceeded`: caller's team is already at max recursion depth.
- `leader_override_attempted`: the call included a `leader_id` field in the
  input. (Beidou's validator rejects before spawn.)
- `unknown_skill`: a `roles[i].skill` does not resolve to a loadable
  SKILL.md.
- `concurrent_create_team`: caller already has an in-flight `create_team`
  (serialization lock).

**Validation rules**
- **Self-lead invariant**: Beidou sets `leader_id = caller_id` for the new
  team. Any `leader_id` in the tool input is rejected with
  `leader_override_attempted`.
- Fan-out cap, recursion depth cap: see `limits.md`.
- One in-flight `create_team` per caller at a time (`limits.md`).

---

## terminate_child

**Kind:** Agent-Beidou. Posts a terminate sentinel to the target agent's
inbox.

**Input schema**
| Field      | Type   | Required | Notes |
|------------|--------|----------|-------|
| `agent_id` | string | yes      | Target agent. Must be a member of a team the caller leads. |
| `force`    | bool   | no       | Default `false`. When `true`, bypass the completion-review gate. Audited via a `terminate.forced` event with `reason="leader_force"`. |

**Output schema**
```
{ "sentinel_posted": true }
```

**Error cases**
- `not_leader`: caller does not lead the team that contains `agent_id`.
- `unknown_agent`: `agent_id` not resolvable.
- `already_terminating`: a terminate sentinel is already in the target's
  inbox (idempotent; structured error indicating no-op).
- `child_not_pending_review`: `target.completion_pending == false` AND
  `target.terminate_consumed == false` AND `force != true`. The leader
  must wait for the child to call `report_status(state="done")`, send a
  rework message via `send_message`, or pass `force=true` to override.

**Validation rules**
- `terminate_child` is the leader's APPROVE verdict on a child's
  `report_status(state="done")` request. The plain (`force=false`) call
  requires the child to be in completion-review state.
- `force=true` is the explicit override. It emits a `terminate.forced`
  audit event with `reason="leader_force"`. Use sparingly.
- `terminate_child` is only valid if the caller leads the **parent team**
  of the target. Crossing team boundaries is not allowed, even for
  ancestor leaders — termination is always leader -> direct-child-team-member.
- Beidou does NOT itself call this tool. Beidou's ONE termination
  privilege applies only to the root agent, via an internal path
  (see `orchestration.md`).

---

## list_pending_reviews

**Kind:** Agent-Beidou. Read-only registry walk; no mutations.

**Input schema**

No input fields.

**Output schema**
```
[
  {
    "agent_id": "<string>",
    "role": "<skill_name string>",
    "completion_pending_ts": <float | null>,
    "age_s": <float | null>,
    "summary": "<last_status_detail string>"
  },
  ...
]
```

Returns the list of the caller's direct child agents (members of any team the
caller leads) that currently have `completion_pending=True` — meaning they have
called `report_status(state="done")` and the caller has not yet responded with
`terminate_child` or a rework `send_message`.

`age_s` is `now - completion_pending_ts` (positive float) or `null` if the
timestamp is absent. Results are sorted ascending by `completion_pending_ts`
(oldest pending review first); entries with no timestamp sort last.

**Error cases:** none. Returns `[]` when there are no pending reviews.

**Validation rules**
- Read-only: no state is mutated. Safe to call as many times as needed.
- "Direct children" means members of teams the caller directly leads (one hop).
  Grandchildren and deeper descendants are not included.
- The caller itself is excluded from results even if it somehow appears as a
  member of one of its own teams.
