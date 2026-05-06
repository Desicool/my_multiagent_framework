# CLAUDE.md — Beidou (北斗)

Autonomous multi-agent CLI system. Agents create teams; teams run in parallel; everything is observable.

## Development rules

- **Always use the venv** at `.venv/`. Run `source .venv/bin/activate` before any Python/beidou commands.
- **Ask before any functional change.** Do not modify behaviour, APIs, or data schemas without explicit approval. Bug fixes and doc updates are fine; anything that changes how the system works requires confirmation first.
- Before any non-trivial edit, open `docs/README.md`; any boundary change requires user approval (see `docs/limits.md`).

## Commit policy (overrides default "ask before commit")

For this project, **small atomic commits are preferred and pre-authorized** within the scope of an approved task. You do NOT need to ask before committing each logical slice:

- Each logically complete unit (a passing feature slice, a green refactor, a doc/spec sync, a removed dead file) is its own commit. Smaller is better than monolithic.
- Always include `Signed-off-by` tag in commit messages.
- Do NOT push to remote unless the user explicitly asks. Local commits are cheap; pushed commits are public and require explicit approval.
- Do NOT skip pre-commit hooks (`--no-verify`) or amend already-committed work without asking.
- Bundle the **same-commit doc update** required by the Cohesion rule into the commit that introduces the behaviour change — never split docs from code.

## Specs first

Before any non-trivial edit, open `docs/README.md` first and follow its pointers to the affected specs. Name which specs you read in chat.

**Approval rule:** If a proposed change would modify a line in `docs/limits.md`, or change a contract in any other spec, stop and ask the user for approval via `AskUserQuestion` before writing code.

**Cohesion rule:** Behaviour changes must land with a same-commit update to the relevant `docs/*.md`. A diff that changes behaviour without touching the specs fails review by definition.

**Exemption:** Bug fixes that preserve every documented boundary and contract proceed without approval.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
beidou init
```

## CLI

```bash
beidou run --model claude-opus-4-7 "Build a REST API with auth and tests"
beidou run --model claude-haiku-4-5-20251001 --skill orchestrator "Write a Python parser"
beidou status [task_id]
beidou teams <task_id>
beidou events --follow --task <task_id>
beidou stats <task_id>
```

## Architecture

```
beidou/
  orchestrator.py     # Concrete Orchestrator: registry, team graph, inboxes, contract-violation recovery, root-only Beidou termination
  sdk_agent.py        # Thin wrapper around claude_agent_sdk.query(...); drains message iterator; emits events
  primitives/
    core.py           # Pure-Python impls of the 8 agent tools
    mcp.py            # build_mcp_server_for(orch, caller_id) via create_sdk_mcp_server
  skills/
    loader.py         # SKILL.md → ClaudeAgentOptions (name-mapping: Beidou primitives → mcp__beidou__*; legacy classic names → SDK built-ins)
  events.py           # EventEmitter: JSONL (~/.beidou/events/{task_id}.jsonl) + SQLite upsert
  db.py               # SQLite: tasks, teams, agents, events tables
  workspace.py        # ~/.beidou/workspaces/{task_id}/{team_id}/ helpers
  gateways/           # Human gateway: TerminalGateway / WebGateway / TUIGateway / CompositeGateway
  templates/          # YAML with tools: list + system_prompt:, used by legacy CLI --template flag (resolves to a skill name)
  context.py          # Deprecated; kept only for legacy tool files that still import it; no longer on the hot path
  layers/             # Deprecated; kept only for legacy tool files that still import them; no longer on the hot path
  cli.py              # Click CLI
```

## Debugging & browser testing

- **Browser testing cleanup:** After using Playwright/browser MCP tools for debugging, delete the `.playwright-mcp/` directory and any `beidou-*.png` screenshots before committing. These are ephemeral debug artifacts, not project files.
- **Build artifact cleanup:** After frontend rebuilds, delete old stale assets (`beidou/web/static/assets/index-*.js`, `index-*.css`) that are no longer referenced by `index.html`. Only keep the two latest assets referenced by the current `index.html`.
- **Port cleanup:** Kill any `beidou run` or `beidou serve` processes started for testing before ending the session. Use `lsof -t -i:<port>` to find and kill stray processes.
- **Test workspaces:** Clean up `/tmp/beidou-test-workspace` and similar test directories after testing.

## Key concepts

**Persistent-agent contract:** agents never self-exit. Completion is a state (`report_status(state="done")`), not an exit. Termination authority is strictly leader→member; Beidou terminates only the root agent on user signal. See `docs/agent-runtime.md`.

**Team creation** happens via the `create_team` primitive. Beidou injects `leader_id = caller_id` (self-lead invariant), so the calling agent always leads the team it creates. Recursion depth is capped — see `docs/limits.md` for the exact value.

**Observability** data lives in two places:
- `~/.beidou/events/{task_id}.jsonl` — raw append-only event log
- `~/.beidou/stats.db` — SQLite aggregated stats (query with Grafana or CLI)

Events flow from the SDK message stream through the orchestrator. `turn.usage` is emitted per unique `message_id`; `run.cost` is terminal. See `docs/observability.md` for the full schema.

## Skills

Pass a skill name to `--skill` (e.g. `--skill orchestrator`). Skills live under `beidou/skills/<domain>/<name>/SKILL.md`. The `--template` flag is deprecated — it forwards the value as a skill name with a warning. The system prompt supports `{role}`, `{role_description}`, `{team_name}`, `{workspace_path}` substitutions defined in each SKILL.md body.

To extend the system, add a skill under `beidou/skills/` or a primitive in `beidou/primitives/core.py`. The old layer-hook protocol is no longer on the hot path and should not be extended.

## Data stores

- `~/.beidou/stats.db` — SQLite (WAL mode)
- `~/.beidou/events/` — JSONL files
- `~/.beidou/workspaces/` — team workspace directories

## Env

`ANTHROPIC_API_KEY` in `.env` (loaded automatically via python-dotenv).


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
