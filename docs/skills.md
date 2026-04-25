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
| `{workspace_path}` | Absolute path of the team's workspace directory. |

Substitution is literal string replace on `{key}` -> value. Missing keys
leave the token untouched (a warning is logged, not an error).

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
build_system_prompt(skill: LoadedSkill, spawn_ctx: dict) -> str
```

Assembles the four-section system prompt. Substitution of `{role}`,
`{role_description}`, `{team_name}`, `{workspace_path}` happens here, in
memory only, never on disk.

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
