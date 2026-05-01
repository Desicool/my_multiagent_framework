# Skill Modules: gate.py, eval.py, and module.toml

Skills can optionally include code-level extension modules alongside `SKILL.md`.
These modules allow users to plug gate (blocking) and eval (observational)
handlers into the agent lifecycle at well-defined hook points.

## Module file layout

```
beidou/skills/<domain>/<name>/
  SKILL.md              # Prompt body (required — existing format, unchanged)
  module.toml           # Hook declarations (optional)
  gate.py               # Gate handler implementations (optional)
  eval.py               # Eval handler implementations (optional)
```

All three module files must be in the same directory as `SKILL.md`. If `module.toml`
is absent, no hooks are registered — the skill behaves exactly as it does today.

## `module.toml` schema

```toml
[module]
name = "my-skill"              # must match SKILL.md frontmatter name
version = "1.0.0"              # semver, independent of SKILL.md version
description = "Optional description of the module's purpose"

[hooks.<hook_point>]
handlers = ["<module>.<function_name>", ...]
mode = "all_must_pass | first_block_wins | all_run"
events = ["event_name", ...]   # only for on_event hook point
```

### Hook points

| Hook Point | Type | Context Class | SDK Integration |
|-----------|------|--------------|-----------------|
| `before_agent_start` | Eval | `AgentStartContext` | Called directly before first SDK message |
| `filter_input` | Gate | `InputContext` | Called before message enters agent input stream |
| `validate_tool_call` | Gate | `ToolCallContext` | PreToolUse HookMatcher |
| `validate_tool_result` | Gate | `ToolResultContext` | PostToolUse HookMatcher |
| `filter_output` | Gate | `OutputContext` | Called before agent output is delivered |
| `evaluate_turn` | Eval | `TurnEvalContext` | Called from drain loop at turn end |
| `on_event` | Eval | `EventContext` | Called from event dispatch side-channel |

### Mode semantics

| Mode | Behavior | Applies to |
|------|----------|-----------|
| `all_must_pass` | Every handler must return `Pass`; first `Block` short-circuits remaining handlers | Gate hooks |
| `first_block_wins` | Handlers run in declaration order; first `Block` stops the chain; all `Pass` = pass | Gate hooks |
| `all_run` | All handlers run concurrently; exceptions logged; no return value collected | Eval hooks |

### The `events` field (on_event only)

The `events` list specifies which Beidou event types the handler subscribes to.
Valid event names are the full catalogue from `docs/observability.md`:

`agent_started`, `agent_input`, `turn.usage`, `tool_called`, `tool_result`,
`assistant_text`, `run.cost`, `agent_completed`, `agent_error`, `agent_crashed`,
`status`, `message_sent`, `question_asked`, `question_answered`, `question_escalated`,
`plan_declared`, `plan_removed`, `task_ready`, `task_spawned`, `task_done`,
`task_failed`, `completion.reported`, `completion.approved`, `completion.rework`,
`contract_violation`, `review_gate.denied`, `liveness.nudge`, `liveness.escalated_to_user`,
`terminate.forced`.

## Handler signatures

### Gate handlers

Gate handlers are async functions that receive a typed context and return `Pass()`
or `Block(reason=...)`.

```python
from beidou.agent.context import Pass, Block, ToolCallContext, InputContext, OutputContext, ToolResultContext

async def my_gate(ctx: ToolCallContext) -> Pass | Block:
    if dangerous_pattern(ctx.tool_input):
        return Block(reason="dangerous pattern detected")
    return Pass()
```

**Fail-closed:** if a gate handler raises an exception, the gate treats it as
`Block(reason="gate_handler_error: <exception>")`. A gate handler that times out
(600 s, see `docs/limits.md` #9) returns `Block(reason="gate_handler_timeout")`.

### Eval handlers

Eval handlers are async functions that receive a typed context and return `None`.
They are fire-and-forget — results are emitted as events via `ctx.emit()`.

```python
from beidou.agent.context import TurnEvalContext, EventContext, AgentStartContext

async def my_eval(ctx: TurnEvalContext) -> None:
    score = compute_score(ctx)
    ctx.emit("eval.score", {"handler": "my_eval", "score": score})
```

**Fail-open:** if an eval handler raises an exception, the error is logged and the
eval continues. If it times out (600 s), the timeout is logged and eval continues.

## Context types

All context dataclasses live in `beidou.agent.context`. Every context provides:

- `agent_id: str` — the agent this hook fires for
- `emit(name: str, payload: dict) -> None` — emit an event to the Beidou JSONL log

### AgentStartContext (`before_agent_start`)

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Agent identifier |
| `skill_name` | `str` | Skill name from SKILL.md |
| `team_id` | `str \| None` | Team ID, None for root agent |
| `leader_id` | `str` | Leader's agent ID or `__user__` |
| `cwd` | `str \| None` | Agent working directory |
| `emit` | `Callable` | Event emission callback |

### InputContext (`filter_input`)

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Agent receiving the message |
| `from_id` | `str` | Sender agent ID or `"user"` |
| `message_kind` | `str` | `"task"`, `"message"`, `"completion_report"`, or `"rework"` |
| `content` | `str` | Message body text |
| `emit` | `Callable` | Event emission callback |

### ToolCallContext (`validate_tool_call`)

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Agent making the call |
| `tool_name` | `str` | Full tool name (e.g. `"Bash"`, `"mcp__beidou__send_message"`) |
| `tool_input` | `dict` | Tool input arguments |
| `tool_use_id` | `str` | Unique tool use identifier |
| `emit` | `Callable` | Event emission callback |

### ToolResultContext (`validate_tool_result`)

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Agent that made the call |
| `tool_name` | `str` | Full tool name |
| `tool_use_id` | `str` | Matches the ToolCallContext tool_use_id |
| `result` | `Any` | Raw tool result value |
| `is_error` | `bool` | Whether the tool returned an error |
| `duration_ms` | `int` | Tool execution duration |
| `emit` | `Callable` | Event emission callback |

### OutputContext (`filter_output`)

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Agent producing the output |
| `output_text` | `str` | Text about to be delivered |
| `output_kind` | `str` | `"assistant_text"`, `"completion_report"`, or `"message"` |
| `emit` | `Callable` | Event emission callback |

### TurnEvalContext (`evaluate_turn`)

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Agent that completed the turn |
| `turn_index` | `int` | 1-based turn counter for this agent |
| `assistant_text` | `str \| None` | Agent's response text, if any |
| `tool_calls` | `list[str]` | Tool names called this turn |
| `token_usage` | `dict \| None` | Token counts from turn.usage event (input_tokens, output_tokens, etc.) |
| `emit` | `Callable` | Event emission callback |

### EventContext (`on_event`)

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Agent this event relates to |
| `event_type` | `str` | Beidou event name (e.g. `"tool_result"`, `"run.cost"`) |
| `event_payload` | `dict` | Full event dictionary |
| `emit` | `Callable` | Event emission callback |

## Discovery and loading

### Discovery order

Module files follow the same precedence as SKILL.md discovery:

1. Bundled `beidou/skills/` (shipped with binary)
2. `~/.claude/skills/` (user-level skills)
3. `{cwd}/.beidou/skills/` (project-local overrides)

Later paths override earlier ones. If a module.toml exists in a higher-priority
directory, its handlers replace those from lower-priority directories for the
conflicting hook points.

### Provisioning

At team creation time, `provision_skills()` copies bundled SKILL.md files into
`<workspace>/.claude/skills/<name>/`. When a skill directory contains module files
(`module.toml`, `gate.py`, `eval.py`), those are also copied. The provisioned
copies are canonical/raw — no template substitution on disk.

### Frozen builds

`beidou.spec` and `pyproject.toml` package `skills/**/module.toml`,
`skills/**/gate.py`, and `skills/**/eval.py` alongside `skills/**/SKILL.md`.

### Handler resolution

Handler names in `module.toml` use the format `<module>.<function_name>`:

- `gate.security_check` → function `security_check` in `gate.py`
- `eval.quality_scorer` → function `quality_scorer` in `eval.py`

Module files are imported via `importlib` at spawn time. The module namespace is
scoped to the agent spawn (unique module name per spawn) to prevent cross-agent
state leakage.

## Handler ordering with built-in hooks

Beidou registers three built-in SDK hooks on every agent spawn:

| Built-in Hook | SDK Hook Type | Purpose |
|--------------|--------------|---------|
| `on_ask_user_question` | PreToolUse | Intercept raw AskUserQuestion, route to human gateway |
| `on_review_gate` | PreToolUse | Block leader tool calls while children await review |
| `on_report_status` | PostToolUse | Completion handoff: read assistant text, route to leader/user |

These built-in hooks **always run first**. User gate handlers from `module.toml`
are appended after them. This guarantees system integrity — a user gate cannot
override or bypass the review gate, AskUserQuestion bridge, or completion routing.

## Trust and security

Adding `gate.py`/`eval.py` turns skill directories into code-execution surfaces.
See `docs/limits.md` #8 for the trust boundary.

- **Gate handlers are fail-closed**: exception or timeout → `Block`. A broken
  gate.py prevents tool execution but does not crash the agent.
- **Eval handlers are fail-open**: exception or timeout → logged, eval skipped.
  A broken eval.py never blocks the agent.
- **Import errors are surfaced**: if `gate.py` cannot be imported, the spawn
  logs a warning and all gate hooks for that skill return `Block`. If `eval.py`
  cannot be imported, a warning is logged and eval hooks are skipped.

## Example

See `examples/custom-skill/` for a complete working example with SKILL.md +
module.toml + gate.py + eval.py.
