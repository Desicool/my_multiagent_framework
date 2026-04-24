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

The loader is a pure function that maps SKILL.md + spawn context to
`ClaudeAgentOptions`:

```
load_skill(path, spawn_ctx) -> ClaudeAgentOptions
```

Steps:

1. Read the file. Split frontmatter (YAML between two `---` lines) from body.
2. Parse frontmatter with `yaml.safe_load`. Validate required fields.
3. Apply template substitutions to the body using `spawn_ctx` fields
   (`role`, `role_description`, `team_name`, `workspace_path`).
4. Translate `allowed-tools`:
   - Each entry `X` becomes `mcp__beidou__X` in `allowed_tools`.
   - If `skills` is non-empty, each entry `Y` additionally becomes
     `mcp__beidou__invoke_Y` in `allowed_tools`.
5. Build the per-spawn MCP server (see `architecture.md`) with the subset of
   primitives listed in `allowed-tools`, plus the `invoke_<name>` tools for
   nested skills.
6. Return:
   ```
   ClaudeAgentOptions(
       system_prompt=<substituted body>,
       mcp_servers={"beidou": server},
       allowed_tools=<list from step 4>,
       permission_mode="bypassPermissions",
       ...
   )
   ```

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
