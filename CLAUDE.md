# CLAUDE.md — Beidou (北斗)

Autonomous multi-agent CLI system. Agents create teams; teams run in parallel; everything is observable.

## Development rules

- **Always use the venv** at `.venv/`. Run `source .venv/bin/activate` before any Python/beidou commands.
- **Ask before any functional change.** Do not modify behaviour, APIs, or data schemas without explicit approval. Bug fixes and doc updates are fine; anything that changes how the system works requires confirmation first.

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
beidou run --model claude-haiku-4-5-20251001 --template coder "Write a Python parser"
beidou status [task_id]
beidou teams <task_id>
beidou events --follow --task <task_id>
beidou stats <task_id>
```

## Architecture

```
beidou/
  context.py          # AgentContext — Go-style linked parent chain + interceptor layers
  agent.py            # Agent: (model, role, ctx) + Anthropic tool-use loop
  team.py             # Team: asyncio.TaskGroup parallel members, recursive sub-teams
  workspace.py        # ~/.beidou/workspaces/{task_id}/{team_id}/ helpers
  tools/              # BaseTool ABC + bash, file_read/write, web_search/fetch, team tools
  layers/             # ContextLayer impls: ToolsLayer, WorkspaceLayer, ObservabilityLayer
  events.py           # EventEmitter: JSONL (~/.beidou/events/{task_id}.jsonl) + SQLite upsert
  db.py               # SQLite: tasks, teams, agents, events tables
  templates/          # YAML: default, coder, researcher
  cli.py              # Click CLI
```

## Key concepts

**AgentContext** is the central abstraction — a linked parent chain where each level adds
`ContextLayer` instances. Layers intercept `on_llm_call`, `on_tool_call`, `on_agent_start`,
`on_agent_stop`. Observability is just a layer; no special-casing in agent.py.

```python
ctx = AgentContext.root(task_id="tsk_abc", layers=[ToolsLayer(tools), ObservabilityLayer(emitter)])
ctx._kv["model"] = model
ctx._kv["emitter"] = emitter
child_ctx = ctx.child(WorkspaceLayer(path), ObservabilityLayer(emitter))  # for sub-agents
```

**Team creation** happens via `CreateTeamTool`. The calling agent's context becomes the parent;
child contexts inherit `task_id`, `model`, and `emitter` via the `_kv` chain. Members run in
`asyncio.TaskGroup` (parallel).

**Observability** data lives in two places:
- `~/.beidou/events/{task_id}.jsonl` — raw append-only event log
- `~/.beidou/stats.db` — SQLite aggregated stats (query with Grafana or CLI)

## Templates

`beidou/templates/*.yaml` — defines `tools` list and `system_prompt` for each agent type.
The system prompt supports `{role}`, `{role_description}`, `{team_name}`, `{workspace_path}`.

Add new templates by dropping a YAML file in `beidou/templates/` — no code changes needed.

## Adding a custom ContextLayer

```python
from beidou.context import BaseLayer

class RateLimitLayer(BaseLayer):
    async def on_llm_call(self, ctx, req, next):
        await self._check_budget(ctx)
        return await next(req)

ctx = root_ctx.child(RateLimitLayer(budget=10.0))
```

## Data stores

- `~/.beidou/stats.db` — SQLite (WAL mode)
- `~/.beidou/events/` — JSONL files
- `~/.beidou/workspaces/` — team workspace directories

## Env

`ANTHROPIC_API_KEY` in `.env` (loaded automatically via python-dotenv).
