<h1 align="center">Beidou (北斗)</h1>

<p align="center">
  <strong>Hard-harness multi-agent orchestrator for the Anthropic Agent SDK.</strong>
</p>

<p align="center">
  <img alt="Status" src="https://img.shields.io/badge/status-early%20prototype-orange?style=for-the-badge">
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="SDK" src="https://img.shields.io/badge/built%20on-claude--agent--sdk-d97757?style=for-the-badge">
  <img alt="License" src="https://img.shields.io/badge/license-TBD-lightgrey?style=for-the-badge">
</p>

<p align="center">
  <a href="docs/README.md">Specs</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="docs/limits.md">Limits</a> ·
  <a href="docs/agent-runtime.md">Agent runtime</a> ·
  <a href="README.zh-CN.md">中文</a>
</p>

**Beidou** is a Python orchestrator on top of `claude-agent-sdk` that spawns
multi-agent teams per project, observes them, and enforces hard harness
boundaries that prompts cannot. The harness — not the prompt — guarantees
the team graph stays sane: caller identity is bound at spawn time,
leadership is the act of spawning, only a leader can terminate a child,
depth and fan-out are hard-capped in code.

If you want to verify your own multi-agent ideas with a working harness
that has tight observability and enforced invariants, but you do not yet
need a generic plugin framework, this is for you.

---

## Table of contents

- [Status](#status)
- [Highlights](#highlights)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [Repo layout](#repo-layout)
- [Core concepts](#core-concepts)
- [How Beidou plugs into the SDK](#how-beidou-plugs-into-the-sdk)
- [How Beidou differs from OpenClaw](#how-beidou-differs-from-openclaw)
- [Why the Anthropic SDK](#why-the-anthropic-sdk)
- [Why a coding-agent team as the first domain](#why-a-coding-agent-team-as-the-first-domain)
- [Philosophy: MVP first](#philosophy-mvp-first)
- [How it was built — a brief history](#how-it-was-built--a-brief-history)
- [Docs by goal](#docs-by-goal)
- [Contributing & development](#contributing--development)
- [License](#license)

---

## Status

Early prototype. The coding-agent skill set is the first and only domain
Beidou has been exercised against. APIs, the event schema, and the wire
shape of every primitive may still shift. Spec changes require explicit
approval per [`CLAUDE.md`](CLAUDE.md). There is **no public extension API
yet** — see [Philosophy: MVP first](#philosophy-mvp-first).

---

## Highlights

- **Per-spawn in-process MCP server.** Each agent gets its own
  `create_sdk_mcp_server` instance whose tool closures bind `caller_id`
  and an orchestrator handle at spawn time. The model never reads its own
  identity from tool input — it cannot spoof.
- **Persistent agents.** No agent ever self-exits. Completion is a state
  (`report_status(state="done")`), not a process exit. Re-assignment
  arrives as the next user-role turn in the same SDK session.
- **Self-lead invariant.** Whoever calls `create_team` becomes that team's
  leader. Leadership is acquired by spawning, not by skill or role label.
- **Leader-only termination.** Only a leader can terminate a direct child.
  Beidou itself can only terminate the root. Cascade is handled by the
  runtime, depth-first, with a watchdog backstop.
- **`[REVIEW REQUIRED]` envelope contract.** When a child reports done, a
  PostToolUse hook reads the reporting turn's last assistant text and
  delivers it to the leader's inbox; the leader must `terminate_child`
  (approve) or `send_message` (rework) before doing anything else.
- **Liveness watchdog.** Review-pending pings, idle nudges, and a
  three-strike escalation to the user gateway, all running on a separate
  asyncio task.
- **Hard limits in code.** Fan-out 8, depth 5, inbox cap 1000, three
  contract-violation strikes — every line of [`docs/limits.md`](docs/limits.md)
  is a boundary; changing any value requires user approval.
- **First-class observability.** Every tool call, turn, completion review,
  and cost rollup is appended to `~/.beidou/events/{task_id}.jsonl`
  (authoritative) and rolled up to `~/.beidou/stats.db`. The Svelte 5 web
  UI replays the stream live.
- **Pluggable human gateway.** Web, terminal, and TUI gateways behind one
  interface. Structured `AskUserQuestion` with answer-as-bubble; free-text
  `approve`/`yes` accepted at the terminal.
- **Three-tier workspace.** Project (user-supplied via `--workspace`,
  shared across the run), team (one per `create_team`), and an agent-
  scoped scratch path for the teamless root.

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
beidou init
```

Run a task:

```bash
beidou run --model claude-opus-4-7 "Build a REST API with auth and tests"
beidou run --model claude-haiku-4-5-20251001 --skill orchestrator "Write a Python parser"
```

Inspect a run:

```bash
beidou status [task_id]
beidou teams <task_id>
beidou events --follow --task <task_id>
beidou stats <task_id>
```

`ANTHROPIC_API_KEY` belongs in `.env` (loaded automatically via
`python-dotenv`).

> **Activate the venv** before any `beidou` or Python command. Beidou is
> tested with **Python 3.12+**.

---

## CLI reference

| Command | Purpose |
|---|---|
| `beidou init` | Initialize `~/.beidou/` (events dir, stats DB, default config). Run once after install. |
| `beidou run [OPTIONS] TASK` | Spawn a root agent and execute `TASK`. Key flags below. |
| `beidou status [task_id]` | Show task / team / agent status snapshot. |
| `beidou teams <task_id>` | Print the team graph for a task. |
| `beidou events --follow --task <task_id>` | Tail the JSONL event stream live. |
| `beidou stats <task_id>` | Aggregated cost / usage / turn rollups. |
| `beidou web` | Launch the live observability UI (Svelte 5 frontend). |

### `beidou run` flags (most-used)

| Flag | Meaning |
|---|---|
| `--model <id>` | Hint to the local Claude Code CLI. Authoritative model is what shows up on `AssistantMessage.model`. See [`docs/agent-runtime.md`](docs/agent-runtime.md) §6. |
| `--skill <name>` | Root skill. Defaults to `orchestrator`. Any skill under `beidou/skills/` is valid. |
| `--workspace <path>` | The project workspace, shared across the run. If omitted, a temp dir is created. |
| `--template <name>` | Deprecated — forwards to `--skill <name>` with a warning. |

For the full flag list run `beidou run --help`.

---

## Configuration

Beidou reads two things from the environment, both via `.env` at the cwd:

```dotenv
# Required. Loaded automatically.
ANTHROPIC_API_KEY=sk-ant-...
```

Everything else lives under `~/.beidou/`:

| Path | Role |
|---|---|
| `~/.beidou/events/{task_id}.jsonl` | Authoritative event log (append-only). |
| `~/.beidou/stats.db` | SQLite (WAL); aggregated rollup cache. |
| `~/.beidou/workspaces/` | Legacy workspace dir (per-run dirs now live under `--workspace`). |

There is no `beidou.toml` yet. Boundary values (fan-out, depth, inbox cap,
hook timeout, etc.) are in `beidou/orchestrator.py` and
[`docs/limits.md`](docs/limits.md) — they are deliberately not user-tunable
at runtime.

---

## Repo layout

| Path | Role |
|---|---|
| `beidou/orchestrator.py` | Team registry, inbox routing, liveness watchdog, root-only termination, completion-review wiring. |
| `beidou/sdk_agent.py` | Thin wrapper around `claude_agent_sdk.query(...)`; drains the async iterator; emits Beidou events; owns the three SDK hooks. |
| `beidou/primitives/` | Seven agent-facing tools: `core.py` (pure-Python impl) + `mcp.py` (MCP wrappers). |
| `beidou/skills/coding/` | First skill set: orchestrator, product_manager, software_architect, junior_engineer, test_engineer, qa_engineer, deployment_engineer. |
| `beidou/web/` | Svelte 5 frontend bundle for the observability UI. |
| `beidou/gateways/` | Pluggable human gateway: terminal, web, TUI, composite. |
| `docs/` | **Source of truth.** Every behaviour boundary is specified here. |
| `proto_01_long_tool.py`, `proto_02_token_granularity.py` | Empirical probes that motivated the SDK adoption. Kept around as primary sources. |

---

## Core concepts

- **Agent** — one persistent SDK session under one skill. Never self-exits;
  completion is a state, not a process exit. Spec:
  [`docs/agent-runtime.md`](docs/agent-runtime.md).
- **Team** — a leader plus N members, spawned by a single `create_team`
  call. The caller becomes the leader (self-lead invariant). Spec:
  [`docs/orchestration.md`](docs/orchestration.md).
- **Workspace** — three tiers. *Project* is user-supplied via
  `--workspace` and shared across the run; *team* is created per
  `create_team` and owned by the team; the *root agent* gets a scoped
  scratch path because it is teamless (depth 0). Spec:
  [`docs/architecture.md`](docs/architecture.md).
- **Primitive** — one of the seven MCP tools every agent sees:
  `send_message`, `list_peers`, `ask_user`, `report_status`,
  `create_team`, `terminate_child`, `list_pending_reviews`. Identity
  (`caller_id`) is bound in the per-spawn MCP closure — the model
  cannot spoof it. Spec: [`docs/tool-surface.md`](docs/tool-surface.md).
- **Skill** — a `SKILL.md` file with YAML frontmatter (`allowed-tools`,
  `description`, `triggers`, etc.) and a body that becomes the system
  prompt. Declarative; no Python required to add one. Spec:
  [`docs/skills.md`](docs/skills.md).

---

## How Beidou plugs into the SDK

Six integration points, all in two files (`beidou/sdk_agent.py`,
`beidou/primitives/mcp.py`).

- **`ClaudeAgentOptions`** carries everything per-spawn: the assembled
  five-section system prompt, `setting_sources=["user", "project"]`,
  `skills="all"`, the MCP server, `allowed_tools`, and `hooks`.
- **Per-spawn in-process MCP server** (`create_sdk_mcp_server`). One
  instance per agent. Each `@tool` closure binds `caller_id` and an
  orchestrator handle at spawn time. That is what makes the
  identity-spoofing invariants real: the model never reads `caller_id`
  from its own tool input.
- **Custom tools** — the seven Beidou primitives, exposed as
  `mcp__beidou__<name>` (see `beidou/primitives/mcp.py`).
- **Three SDK hooks** in `beidou/sdk_agent.py`:
  - `on_ask_user_question` (PreToolUse) — forwards the SDK-builtin
    `AskUserQuestion` to Beidou's human gateway, unchanged. Both this
    path and the MCP `ask_user` path produce the same `Question` object.
  - `on_review_gate` (PreToolUse) — blocks a leader from calling any
    non-allowlisted tool while one of its direct children has
    `completion_pending=True`.
  - `on_report_status` (PostToolUse) — owns the completion handoff:
    reads the agent's last assistant text from the same turn,
    synthesizes a `[REVIEW REQUIRED]` envelope if missing, and either
    delivers the body to the leader's inbox or routes the root agent's
    review through the user gateway. Hook timeout overridden to 1800 s
    so a real human review is not silently truncated by claude-code's
    60 s default.
- **Drain loop** consumes the SDK's async message iterator
  (streaming-input mode), translates SDK messages to Beidou events, and
  parks each agent on a per-agent `asyncio.Queue` between turns.
  Re-assignment, peer messages, and terminate sentinels all arrive
  through that queue.
- **No per-LLM-call interception.** Beidou observes; the SDK owns
  retries, caching, and tool dispatch.

**Porting cost.** The seven primitives and the team graph are
SDK-agnostic (pure Python in `beidou/primitives/core.py` and
`beidou/orchestrator.py`). The SDK-coupled surface is
`beidou/sdk_agent.py` (drain loop, message-shape mapping, hook
registration) and `beidou/primitives/mcp.py` (MCP wrappers). A
`pi-core` backend would replace those two files; everything else is
portable. On the table for a future iteration, possibly as a
user-selectable backend rather than a forced replacement.

---

## How Beidou differs from OpenClaw

(*OpenClaw is a personal AI assistant where pre-defined personas serve
the user across long-lived channels and sessions.*)

- **Project-based.** Each `beidou run` is bound to a `--workspace` and
  spawns a fresh agent graph. There is no global agent registry; agents
  do not outlive a task.
- **Per-project agents.** Agents are spawned for the task at hand from a
  small set of role-skills, not selected from a roster of pre-defined
  personas re-used across runs.
- **First-class observability.** Every tool call, turn, completion
  review, and cost rollup lands in
  `~/.beidou/events/{task_id}.jsonl`; the web UI replays them live.
  The user can watch what each agent did and why.
- **Hard harness over soft prompts.** The persistent-agent contract,
  the `create_team` self-lead invariant, leader-only termination,
  depth and fan-out caps, the `[REVIEW REQUIRED]` completion envelope,
  the PostToolUse review hook, and the liveness watchdog are all
  enforced in code. Prompts can drift turn-to-turn; the harness can't.
  See [`docs/agent-runtime.md`](docs/agent-runtime.md) and
  [`docs/limits.md`](docs/limits.md).

---

## Why the Anthropic SDK

Three reasons, in order:

1. When this project began, Anthropic models were the strongest at
   agentic coding workloads.
2. `claude-agent-sdk` already shipped a cache-friendly, retryable
   single-agent loop and an in-process MCP server
   (`create_sdk_mcp_server`). Re-implementing either is wasted work; the
   original hand-rolled loop in Beidou was a source of drift and is now
   retired (commit `5f267c2`).
3. Beidou stays an *orchestrator* on top. It does not own the
   per-LLM-call loop — that belongs to the SDK. All Beidou-specific
   behaviour (team graph, A2A routing, observability, termination
   authority) is external to the single-agent loop and lives in the
   orchestrator boundary.

For a provider-neutral version, the right route is `pi-core`, not a
hand-rolled harness or `pi-agent`. The mechanism for what porting would
actually entail is in
[How Beidou plugs into the SDK](#how-beidou-plugs-into-the-sdk).

---

## Why a coding-agent team as the first domain

Coding has the densest success signal for an agentic harness — tests
pass, code runs, the build succeeds — so the framework gets the densest
UX feedback per run. It is also the domain I know best, so I can
recognize when the harness is helping vs. when it is getting in the way.

The skill set under `beidou/skills/coding/` is the first instance of the
skill schema:

- `orchestrator` — the default root skill; plans phases and spawns teams.
- `product_manager` — turns a vague task into a concrete `requirements.md`.
- `software_architect` — produces a `SPEC.md` and a `tasks.md`.
- `junior_engineer` — implements a single task.
- `test_engineer` — writes the tests for a task.
- `qa_engineer` — exercises the result against the spec.
- `deployment_engineer` — wires the deploy / release path.

Future domains plug in by adding another directory under `beidou/skills/`.
Today, *coding is the only domain Beidou has been exercised against.*

---

## Philosophy: MVP first

Beidou is intentionally not a generic, plugin-friendly, event-bus-driven
framework yet.

- No public extension API. New skills land by adding a `SKILL.md` under
  `beidou/skills/`. New primitives land by editing
  `beidou/primitives/core.py`.
- The JSONL event stream is exposed but there is no public subscriber
  API and no stable schema versioning beyond
  [`docs/observability.md`](docs/observability.md).
- The harness/skill pair is co-designed for the Anthropic SDK; I have
  not generalized over harnesses.

This is deliberate. I want to verify the framework on one real workload
(coding) and harvest the UX feedback before generalizing. Generalizing
before the MVP works tends to bake in the wrong abstractions. When the
MVP feels right, the next step is to extract the harness/skill pair as
a stable contract so other people can plug in their own.

---

## How it was built — a brief history

Roughly four phases over ~50 commits.

**Bootstrap.** The first cut was a hand-rolled agent loop in pure Python,
with a user-inbox escalation chain, an LLM/tool resilience layer (retry,
backoff, error normalization), and an early web UI that consumed an
event-bus. A pluggable question gateway shipped early — web, TUI, and
composite channels — because human-in-the-loop was always the point.

**SDK pivot — the inflection point** (`5f267c2`, *cutover: retire manual
agent loop, rewire CLI to Orchestrator*). Two empirical probes,
`proto_01_long_tool.py` and `proto_02_token_granularity.py`, verified
that `claude-agent-sdk` had no per-tool timeout and exposed the usage
and cost accounting Beidou needed. Spec-driven workflow scaffolding
under `docs/` went in alongside. The hand-rolled loop was deleted;
Beidou stopped owning the per-LLM-call path. After this commit, Beidou
is an orchestrator only.

**Observability and UI maturity.** JSONL was declared authoritative;
SQLite was demoted from event of record to an aggregated rollup cache.
Tool spans got pair-tracking. The web UI was rebuilt around a
cursor-based event stream and later rewritten in Svelte 5 with foldable
tool cards and a markdown stream. Agents auto-park on a per-agent queue
(`wait_for_message` and `read_messages` were deleted — the runtime now
delivers messages, agents do not poll).

**Harness hardening** (`38e3cd1`, *feat(orchestration): unified
teamless-root model + create_team consensus guardrail*). The three-tier
project / team workspace went in along with the `--workspace` flag.
Leader-mediated completion review landed: a `[REVIEW REQUIRED]` envelope
contract, a PostToolUse hook that reads the reporting turn's last
assistant text and routes it to the leader's inbox (or to the user
gateway if the reporter is the root), a liveness watchdog with
review-pending pings and idle nudges, `terminate_child` gated on
completion-pending, and structured `AskUserQuestion` with
answer-as-bubble. Human-readable agent names came in. The synthetic
`tm_root` team-of-one was removed: the root agent is genuinely teamless
at depth 0; if a task is too big for one agent, the root calls
`create_team` and becomes a leader. `create_team` gained a
`consensus=True` guardrail that rejects N>1 members sharing the same
`(skill, description)` pair, to block the "all juniors implement the
whole feature" footgun.

**Task propagation and prompt hygiene** (`18e81df`, *feat(spawn):
propagate user task as first user message; clean role_description*).
Diagnosing a real "calculator with React" run surfaced that spawned
team members never received the user's actual request — the
orchestrator's `create_team` example hardcoded a meta-description, the
worker `SKILL.md` bodies never referenced `{role_description}`, and the
agent's first user message was the team-level coordination string the
orchestrator wrote in its own words. Fix: the orchestrator captures the
originating user task at `run_root` time and propagates it as the first
user-role message into every spawn, including transitively-spawned
sub-team members. `{role_description}` is now strictly the role-specific
scope (the root's substitutes to empty); each worker `SKILL.md` got a
`## Your role-specific scope` block surfacing the placeholder. The
team-level `task` arg from `create_team` stays on `TeamRecord` for
orchestrator-internal coordination but is no longer the agent's first
user message.

---

## Docs by goal

| You want to... | Read |
|---|---|
| Understand the orchestrator/SDK split, process layout, event flow | [`docs/architecture.md`](docs/architecture.md) |
| Understand the persistent-agent contract, completion review, watchdog | [`docs/agent-runtime.md`](docs/agent-runtime.md) |
| Understand the seven agent-facing primitives and their wire shapes | [`docs/tool-surface.md`](docs/tool-surface.md) |
| Add or modify a skill (SKILL.md frontmatter, system prompt assembly) | [`docs/skills.md`](docs/skills.md) |
| Understand the team graph, self-lead invariant, termination cascade | [`docs/orchestration.md`](docs/orchestration.md) |
| Understand the JSONL event schema and accounting granularity | [`docs/observability.md`](docs/observability.md) |
| Look up a hard limit (fan-out, depth, inbox cap, etc.) | [`docs/limits.md`](docs/limits.md) |
| Touch the web UI (Svelte components, reducer, panels, build) | [`docs/web-ui.md`](docs/web-ui.md) |
| Find the per-change-kind checklist for developers | [`docs/workflows.md`](docs/workflows.md) |

The full read-first mapping for any non-trivial edit lives at
[`docs/README.md`](docs/README.md).

---

## Contributing & development

Beidou is a personal-scale prototype today; PRs are welcome but expect
slow review.

- Read [`AGENTS.md`](AGENTS.md) and [`CLAUDE.md`](CLAUDE.md) — they
  encode the project's working rules. Both human and agent contributors
  follow the same rules.
- Read [`docs/README.md`](docs/README.md) before any non-trivial edit.
  State which specs you read in the PR description.
- Spec changes (modifying [`docs/limits.md`](docs/limits.md), changing
  any contract in [`docs/agent-runtime.md`](docs/agent-runtime.md) or
  [`docs/orchestration.md`](docs/orchestration.md), changing the SKILL
  schema in [`docs/skills.md`](docs/skills.md), or changing the event
  schema in [`docs/observability.md`](docs/observability.md)) require
  explicit user approval before code is written.
- Behaviour changes land in the same commit as their spec update
  (cohesion rule).
- Issue tracking uses [`bd`](https://github.com/beads-software/bd) —
  run `bd prime` for the workflow.

```bash
# Dev loop
source .venv/bin/activate
pip install -e .
pytest
beidou run --skill orchestrator "<your task>"
```

---

## License

To be decided. Until a license is added, this repository is **all rights
reserved**.
