# Skills: SKILL.md format and loader

Skills live at `beidou/skills/<domain>/<name>/SKILL.md` (path unchanged from
today). Each SKILL.md is a Markdown file with YAML frontmatter defining the
agent's identity and tool access, followed by a body that becomes the agent's
system prompt.

Canonical example:
`beidou/skills/coding/orchestrator/SKILL.md`

## Frontmatter schema

The frontmatter is parsed as YAML. The following keys are recognised. Field
names use **dashed** form for back-compat with the existing on-disk schema.

| Field | Type | Required | Purpose |
|---|---|---|---|
| `name` | string | yes | Unique skill id. Used in directory path and when referenced by `skills:` of another SKILL. Example: `orchestrator`. |
| `version` | string | yes | Semver. Bumped on any behaviour change. Example: `1.0.0`. |
| `description` | string (block scalar allowed) | yes | One-paragraph summary the loader passes through. Used for UI listings and by invokers deciding which skill to use. |
| `allowed-tools` | list[string] | yes | MCP primitive names the agent may call. Each entry is a Beidou primitive name **without** the `mcp__beidou__` prefix (the loader namespaces it). Example: `[bash, file_read, file_write, create_team]`. |
| `skills` | list[string] | no | Nested skills. Each entry is the `name` of another SKILL. The loader exposes each as an additional MCP tool named `invoke_<name>` (e.g. `invoke_product_manager`). |
| `triggers` | list[string] | no | Human-language phrases that suggest this skill. Used by the orchestrator skill to pick a matching skill. Example: `["build this", "implement this"]`. |

Unknown frontmatter keys are preserved by the parser but ignored by the
loader. Schema changes (adding a field, changing a type, renaming
`allowed-tools` -> `allowed_tools`) require explicit user approval per
`README.md`.

## Body: system prompt

Everything after the closing `---` of the frontmatter is the **system
prompt** delivered to the SDK agent via `ClaudeAgentOptions.system_prompt`.

### Template substitutions

Before being passed to the SDK, the loader substitutes these tokens:

| Token | Replaced with |
|---|---|
| `{role}` | The agent's role name in its team (from `create_team(roles=...)`). |
| `{role_description}` | The `description` supplied for this role at spawn time. |
| `{team_name}` | The name of the team the agent was spawned into. |
| `{workspace_path}` | Absolute path of the agent's workspace directory. For team members, this is the team workspace (`{project}/.beidou/tasks/{task_id}/teams/{team_id}/`). For the root agent (which starts teamless), this is an agent-scoped scratch path (`{project}/.beidou/tasks/{task_id}/agents/{agent_id}/`) used for orchestrator-internal storage. The root agent's cwd is the project workspace, not this path. |
| `{project_workspace_path}` | Absolute path of the project workspace (user-supplied via `beidou run --workspace`). Same on every agent in the task; used by agents to read/write cross-team shared files via absolute paths. |

Substitution is literal string replace on `{key}` -> value. Missing keys
leave the token untouched (a warning is logged, not an error).

### Task assignment injection

When an agent is spawned via `spawn_agent` with a `plan_task_id`, the runtime
prepends a delimited `[TASK ASSIGNMENT]...[/TASK ASSIGNMENT]` block to the
agent's first user message (the task text from `declare_plan`). This is NOT
part of the system prompt — it preserves cross-agent KV cache on the skill body.

| Field | Value |
|---|---|
| `plan_task_id` | The task ID from `declare_plan` (e.g., `task-1`) |
| `artifacts_path` | `{project_workspace_path}/artifacts/{plan_task_id}` |

Agents should write output to their `artifacts_path` and reference their
`plan_task_id` when reporting completion. The header is only present for
plan-spawned agents; agents spawned via the deprecated `create_team` do not
receive it.

## Loader behaviour (`beidou/skills/loader.py`)

The loader has three distinct responsibilities:

### 1. Build-time / startup validation

Validates every SKILL.md: frontmatter shape, `allowed-tools` legality (all
entries must be recognised Beidou primitives or SDK builtins), `skills:`
references resolve. `load_skill(skill_root, name)` and `load_skill_file(path)`
are the entry points.

### 2. Per-team workspace provisioning (`provision_skills`)

```python
provision_skills(workspace_path: Path, skill_root: Path | None = None) -> list[Path]
```

Called once per team workspace creation (in `spawn_team` and `run_root`).
Copies every bundled SKILL.md into `<workspace>/.claude/skills/<name>/SKILL.md`.

Rules:
- The workspace copies are **canonical/raw** — no `{role}` / `{team_name}`
  substitution on disk. Substitution lives only in `build_system_prompt`.
- Atomic write: temp file + `os.replace`. Idempotent: skips if content is
  byte-identical.
- User skills under `~/.claude/skills/` are discovered by `setting_sources=["user"]`
  in place — never copied.

This provisioning makes the SDK's `setting_sources=["project"]` discovery work
correctly: the agent's `Skill` tool can then list all bundled Beidou skills.

### 3. Per-agent-spawn system prompt assembly (`build_system_prompt`)

```python
# Lives in beidou/agent/prompts.py (moved from beidou/skills/loader.py)
build_system_prompt(skill: LoadedSkill, spawn_ctx: dict) -> str
```

Assembles the five-section system prompt. Substitution of `{role}`,
`{role_description}`, `{team_name}`, `{workspace_path}`, `{project_workspace_path}`
happens here, in memory only, never on disk.

#### System prompt structure (section order locked)

Putting the skill body **first** is required for cross-agent prompt cache
reuse: two agents using the same skill share a common byte-prefix (the multi-KB
skill block), while per-agent identity (IDENTITY block) comes after.

```
[ASSIGNED SKILL — authoritative instructions for your role]
──── BEGIN SKILL: {skill_name} v{skill_version} ────
{skill_body_with_substitutions}
──── END SKILL ────

[IDENTITY]
You are {role} in team {team_name}.
Workspace: {workspace_path}.
Project workspace: {project_workspace_path}.
Leader: {leader_id}.

[PERSISTENT-AGENT CONTRACT]
(verbatim — see agent-runtime.md)

[OTHER SKILLS]
Other available skills are listed via the `Skill` tool. You MAY invoke them
when they would genuinely improve the work, but the [ASSIGNED SKILL] above is
authoritative for your role and approach.
```

### 4. SDK builtin allowlist extraction (`sdk_builtins_allowlist`)

```python
sdk_builtins_allowlist(allowed_tools: list[str]) -> list[str]
```

Given the raw `allowed-tools` list from a SKILL.md frontmatter, returns only
the entries that map to SDK built-in tool names (`Bash`, `Read`, `Write`,
`WebFetch`, `WebSearch`). Needed because `skills="all"` does NOT auto-add SDK
builtins — those must be passed explicitly in `allowed_tools`.

### 5. SDK builtin suppression (two-layer pattern)

Beidou implements its own multi-agent topology via the `mcp__beidou__*`
primitives (`create_team`, `spawn_agent`, `terminate_child`,
`send_message`, `list_peers`, ...). The Claude Agent SDK ships several
default-on builtins that compete with or shadow these primitives. The
contract: **removing a tool from a skill's `allowed-tools` is NOT
sufficient** — the SDK runtime exposes its default builtins regardless
of the per-skill allowlist. Suppression requires an explicit entry at
spawn time.

Two independent reasons a builtin needs suppression have shown up so
far:

- **Env-flag-gated tools** (e.g. `SendMessage`). When
  `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is set in the environment —
  inherited from a user shell profile — the SDK's CLI binary exposes
  competing inter-agent tools (currently `SendMessage`; future:
  `Spawn`, `Task`). Those tools know nothing about Beidou's agent
  registry; calls return empty content with `is_error=False` (silent
  no-op), and models often misread the silence as "agents are offline"
  (see `tsk_658f44b6` evidence in commit history).

- **Always-on default builtins** (e.g. `TodoWrite`). These are exposed
  by the SDK runtime with no env gate. `TodoWrite` competes with
  `mcp__beidou__report_status` as a completion signal — in
  `tsk_658f44b6` the impl-leader (junior_engineer) terminated its last
  child via `terminate_child`, then called `TodoWrite` to mark its
  todos complete in lieu of emitting `[REVIEW REQUIRED]` +
  `report_status`. The orchestrator hung waiting for the report
  forever. Beidou's task tracking uses `bd` (cross-session) and
  `report_status` (in-conversation); `TodoWrite` has no role.

`build_options` (`beidou/agent/loop.py`) defends in two layers:

1. **Env scrub** (`env={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": ""}`).
   The SDK transport merges `options.env` on top of inherited process
   env, so this empty value overrides whatever a user's profile sets
   globally. Disables the experimental team-mode tool surface
   wholesale; kills future SDK team tools as they ship. **This layer
   only applies to env-flag-gated tools.** Always-on builtins
   (`TodoWrite`) ignore it.

2. **disallowed_tools** (`disallowed_tools=["SendMessage", "TodoWrite"]`).
   The SDK respects this list regardless of env state or default-on
   status. This is the layer that catches both classes:
   - `SendMessage` — defense-in-depth in case env scrub leaks.
   - `TodoWrite` — primary suppression (no env layer applies).
   Expand if new SDK shadow-tools surface in events.

A third defense applies at the prompt layer:

3. **Skill prompt-level NEVER DOs.** The coding_v2 skill bodies
   instruct models to use Beidou primitives and forbid the SDK
   aliases (`SendMessage`, `TodoWrite`). This is the layer the model
   internalizes; it is correctness commentary, not a runtime barrier.

All inter-agent messaging in Beidou MUST go through
`mcp__beidou__send_message`. All in-conversation completion signals
MUST go through `mcp__beidou__report_status`. Skills MUST NOT list
`SendMessage`, `TodoWrite`, or any other SDK shadow-tool name in their
`allowed-tools`; the disallow takes precedence regardless. Removing
either entry from the disallow list, restoring the env var in
`build_options`, or adding a SKILL.md NEVER DO exception requires
explicit user approval per the docs/README.md cohesion rule.

## Leadership and delegation

Any skill MAY include `create_team` in its `allowed-tools` list and thereby
act as a leader. The orchestrator skill is one specialization of this pattern —
it is not the only one. A junior_engineer or test_engineer skill may equally
include `create_team` and spawn a sub-team if its task warrants it.

Leadership is acquired by spawning (see `agent-runtime.md` §2, "Delegation is
a right, not a role"), not by skill label.

### Recommended `## Delegation policy` prompt section

Skills that expose `create_team` should include a `## Delegation policy`
section in their prompt body (before any ambiguity / completion rules). The
recommended pattern:

```
## Delegation policy

Default is solo; delegation has overhead. Delegate only when:
- The work splits cleanly into distinct sub-tasks.
- A sub-task requires a distinct skill you do not hold.
- The combined work exceeds one agent's practical context.

Once you create a team, you inherit leader duties:
- Inspect every [REVIEW REQUIRED] handoff.
- Resolve with terminate_child (approve) or send_message (rework).
- Do not advance while any child review is pending.

Spawned teammates are simple agents and may themselves call create_team.
Depth and fan-out remain bounded by docs/limits.md.

Each teammate gets the description YOU write for them — do NOT clone the
parent task across all members. Each role entry must have a description
capturing that member's specific sub-task; otherwise all members will
redundantly implement the entire task in parallel.
```

Adapt wording to the role but preserve the no-clone, leader-duties, and
depth-bounded clauses.

### Runtime caveat on `skills:` rosters

The loader parses and validates `skills:` entries into `LoadedSkill.sub_skills`
at load time. However, **runtime spawn is not currently restricted by that
roster**: the SDK always runs with `skills="all"` and `setting_sources=["user",
"project"]`, and `spawn_team()` resolves any loadable skill name under the
configured `skill_root`. The shared `skills:` roster is therefore:

1. Prompt-level metadata and documentation for skill authors.
2. A future enforcement surface — if per-skill runtime authorization is added
   later, the roster is the candidate list.

It is **not** a runtime authorization boundary today. A separate code change in
`sdk_agent.py` and `spawn_team()` would be required to enforce it.

## Validation rules

- `allowed-tools` entries that are not registered Beidou primitives cause a
  load-time error. This prevents silent typos like `create-team` vs
  `create_team`.
- `skills` entries that do not resolve to a loadable SKILL.md cause a
  load-time error.
- The body MUST include the persistent-agent lifecycle instructions from
  `agent-runtime.md` section 3 (or inherit them from a base template - TBD).
  The loader does NOT enforce this automatically; it is a review-time gate.

## Adding a new skill

1. Create `beidou/skills/<domain>/<name>/SKILL.md` with the frontmatter above.
2. Body: role-specific instructions + lifecycle contract (section 3 of
   `agent-runtime.md`).
3. No code changes needed. The loader discovers by path.

The `name` field must be unique across the entire `beidou/skills/` tree —
`load_skill(skill_root, name)` raises `DuplicateSkill` (loader.py:302) when two
SKILL.md files share a name, and `provision_skills` uses `name` as the
destination directory (loader.py:365), so duplicates also collide on disk. When
forking an existing skill (e.g. `coding/product_manager` to `coding_v2/product_manager`)
the new file's `name` MUST differ from the original — by convention the new
copy uses a `_v2` (or similar) suffix; see `coding-v2.md` for an example. A
brand-new skill with no fork relationship can keep its directory name as its
`name` directly.

## Skill-pack examples

Multi-agent skill packs (collections of SKILL.md files that orchestrate a
flow together) are documented under `docs/<pack-name>.md`. See
`docs/coding-v2.md` for the canonical example.

### Optional: adding code modules

A skill directory may also contain code-level extension modules (`module.toml`,
`gate.py`, `eval.py`) that plug gate (blocking) and eval (observational)
handlers into the agent lifecycle. When these files are absent, the skill
behaves exactly as it does today — code modules are purely additive.

See `docs/skill-modules.md` for the complete specification.
