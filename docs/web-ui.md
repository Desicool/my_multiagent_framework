# Web UI

The Beidou web UI is a Svelte 5 + Vite + TypeScript single-page application that lives under
`beidou/web/frontend/`. It is a **pure consumer** of the JSONL event log: it reads events
delivered by the WebSocket relay and derives all state by reduction; it never writes to
the JSONL file or to the SQLite database.

## Spine + Workspace layout (PR 1+)

PR 1 introduced a persistent left **spine** (240px) and a shared **topbar** (44px). The
remaining viewport is occupied by a **workspace** chosen via the spine. Routes are
bookmarkable:

| Route | Workspace | Status |
|---|---|---|
| `/` | task list (home) | shipped |
| `/tasks/{id}/agents` | preserved 3-pane (`TaskOverview` / `PinnedAgentPanel` / `TeamTree`) | shipped (PR 1) |
| `/tasks/{id}/timeline` | milestone event timeline | placeholder; PR 3 |
| `/tasks/{id}/tools` | primitive card explorer | shipped (PR 2) |
| `/tasks/{id}/stats` | dashboard | disabled; P1 deferred |
| `/tasks/{id}/overview` | summary tiles | disabled; P1 deferred |
| `/tasks/{id}/questions` | Q&A history | disabled; P1 deferred |
| `/tasks/{id}` (legacy) | 302 → `/tasks/{id}/agents` | redirect |

```
┌─ TopBar ─ ◆ BEIDOU · tsk_abc123 · elapsed 14:25 · 7 agents · cost $0.42 · ⚠ 1 question ─┐
│ ┌─ Spine (240px) ──┐  ┌────────────── Workspace area ─────────────────────────────────┐ │
│ │ TASKS            │  │  /agents (preserved 3-pane):                                  │ │
│ │  ○ tsk_abc123    │  │  ┌──────────────┬──────────────────────────┬──────────────┐  │ │
│ │  ○ tsk_def456    │  │  │ TaskOverview │  Question Banner +       │ TeamTree     │  │ │
│ │                  │  │  │  + Activity  │  PinnedAgent stream      │              │  │ │
│ │ WORKSPACE        │  │  │              │  + Composer              │              │  │ │
│ │  ▤ Agents  ●     │  │  └──────────────┴──────────────────────────┴──────────────┘  │ │
│ │  ≡ Timeline      │  │                                                               │ │
│ │  ▦ Tools         │  │  /tools = primitive card explorer (PR 2)                      │ │
│ │  ◔ Stats   ·P1   │  │                                                               │ │
│ │  ○ Overview·P1   │  │                                                               │ │
│ │  ○ Q&A     ·P1   │  │                                                               │ │
│ └──────────────────┘  └───────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

### Shell components

- **`components/TopBar.svelte`** — brand glyph (◆ in `review` cyan), task_id (mono),
  elapsed (HH:MM:SS, live-updating), agents-alive count, cost (`tabular-nums`), amber
  pending-question pulse chip (clicking the chip jumps to `/agents` for that task).
- **`components/spine/Spine.svelte`** — 240px left rail composing
  `SpineTaskList` (top group) and `WorkspaceNav` (bottom group). The active workspace
  gets a cyan left border + tinted background; disabled `·P1` items are visually muted
  and non-interactive.
- **`components/Layout.svelte`** — 2-column shell (spine | workspace area). Used only
  while a task is selected. The home view (`/`) renders the task list directly under
  the topbar without the spine.
- **`components/workspaces/AgentsWorkspace.svelte`** — preserved 3-pane verbatim
  (`280px` `TaskOverviewPanel` / fluid `PinnedAgentPanel` / `320px` `TeamTreePanel`).
  No internal change to `PinnedAgentPanel`, `TeamTree`, `QuestionBanner`,
  `TaskOverview`, or `GlobalActivityFeed` in PR 1.
- **`components/workspaces/ToolsWorkspace.svelte`** — `/tools` workspace (PR 2). Aggregates
  every `tool` stream item across all agents whose name resolves to a known primitive
  (`lib/primitives.ts`), sorts by ts, and renders each via `components/primitives/PrimitiveCard.svelte`.
  Filter pills cover the five categories (coordination / lifecycle / plan / review / human),
  plus an agent dropdown and an errors-only toggle. A category legend strip below the
  filters documents the classification rule for each.
- **`components/primitives/`** — one Svelte component per primitive (13 total) sharing
  `PCardShell.svelte` (top accent stripe, head with category label + badges + ts/duration,
  body grid). `FlowStrip.svelte` renders the prominent FROM/TO band on `send_message`,
  `spawn_agent`, `terminate_child`, and `ask_user`. `AgentChip.svelte` renders the
  monogram + id + role chip used in caller fields. `PrimitiveCard.svelte` is a thin
  dispatcher that routes a `ToolStreamItem` to the matching component by bare tool name.
  The same components are reused inline by `components/middle/stream/ToolCard.svelte`,
  which delegates to `PrimitiveCard` whenever `lib/primitives.ts:isPrimitive()` matches
  and falls back to its compact collapsible card for non-primitive tools (Bash, Read, …).
- **`components/workspaces/PlaceholderWorkspace.svelte`** — parameterized stub used by
  `/timeline` (PR 3) and any P1-deferred workspace reachable by URL.

### Router

`lib/router.svelte.ts` parses hash routes:

- `/tasks/{id}/{workspace}` → `task_workspace` (App dispatches by workspace name).
- `/tasks/{id}` → `task` (App `$effect` rewrites to `/tasks/{id}/agents`).
- `/agents/{id}` → `agent` (legacy info screen — agent-only deep links are no longer
  top-level; pin via the team tree inside `/agents`).
- `/` → `home`.

### Density & typography

The shell pulls in JetBrains Mono for IDs, durations, and counts (`font-mono` / `.tabnum`).
Density CSS variables (`--row-h`, `--pad-x`, `--pad-y`, `--lh`) default to comfortable
(`/agents`); future workspaces (`/timeline`, `/tools`, `/stats`) opt into Bloomberg-dense
via `data-density="dense"`.

### QuestionBanner — structured multi-part questions

When a pending question is surfaced in the middle panel, `QuestionBanner.svelte` renders
1..4 sub-questions from the raw `questions` array. Each sub-question is displayed with:

- A **chip-style header** (the `header` field, ≤12 chars) labelling the sub-question.
- The **question text** (`question` field) as the prompt.
- An **input control** chosen by the sub-question's shape:
  - `options.length == 0` → `<textarea>` (free-text only).
  - `multiSelect == false` → radio buttons (single-select) with an implicit "Other" option
    that reveals a free-text input when selected.
  - `multiSelect == true` → checkboxes (multi-select).

The submit button is **disabled** until every sub-question has a complete answer (a choice
selected or, for free-text controls, a non-empty string typed).

After submission, `/api/questions/{qid}/answer` accepts
`{answers: [{selected_labels, text}, ...]}` and routes the response through
`QuestionBroker.resolve_answer()`. The broker computes `answer_text` (one rendered line per
sub-question, joined by newlines), persists the answer to SQLite, and emits a
`question_answered` event carrying both `answers` and `answer_text`.

The web reducer handles `question_answered` by synthesizing a `message_in` stream item on
the asker agent's stream: `{kind: "message_in", from: "user", from_is_user: true,
content: answer_text, message_id: qid}`. This creates a permanent inbound chat bubble in
the asker's conversation trace — making the human answer visible in context, without
inventing a fake inbox delivery event.

## StreamItem typed union

Defined in `beidou/web/frontend/src/lib/types.ts`:

```ts
type StreamItem =
  | { kind:'text';        ts; message_id?; text; stop_reason? }
  | { kind:'tool';        ts; tool_use_id; name; input?; duration_ms; is_error; error_reason?; expanded? }
  | { kind:'message_in';  ts; from; from_is_user; content; message_id }
  | { kind:'message_out'; ts; to; content; message_id }
  | { kind:'turn_divider';ts; in_tok; out_tok; stop_reason?; model? }
```

The per-agent stream (`AgentState._stream`) is **capped at 500 items**. To ensure late
`tool_result` events can still patch their paired `tool_called` item even after the stream
has been trimmed, the reducer maintains a per-agent `_pendingTools: Map<tool_use_id, ToolStreamItem>`
that is **never trimmed**; the `tool` item in `_stream` and the `_pendingTools` entry both
reference the same object, so patching either updates both.

## Event flow

```
~/.beidou/events/{task_id}.jsonl
        │
        ▼  tailed by WebSocket relay
/ws/tasks/{task_id}?since=cursor  (envelope: {type, events, cursor})
        │
        ▼  applyEvent() reducer
Svelte 5 rune-backed stores (events.svelte.ts, questions.svelte.ts, ui.svelte.ts, …)
        │
        ▼
Components (AgentStream, TeamTree, GlobalActivityFeed, …)
```

Events arrive in a WebSocket envelope `{type: "events"|"resumed", events: BeidouEvent[], cursor: number}`.
On reconnect the client sends `?since=<cursor>` to replay missed events from the last known
position. State is derived by replaying all events in order through the `applyEvent` reducer
(`beidou/web/frontend/src/reducer/reduce.ts`).

### Cursor caveat

The backend uses a strict `ts > since` filter. Two events sharing the same `ts` at the replay
boundary may be silently dropped on reconnect. Additionally, an idle task never emits a
`resumed` envelope (it only fires after a strictly newer event arrives), so the connection
pill can stay in `replaying` state indefinitely. The UI applies an idle-timeout fallback: if
the WebSocket has been silent for 3 seconds after the cursor appears to have caught up, it
flips `replaying` → `live` without waiting for a `resumed` envelope. **Hard-refresh the tab
if you suspect event drift** — this replays the full JSONL from `cursor=0`.

## Markdown and security

Agent text is rendered via a single-pass pipeline in `beidou/web/frontend/src/lib/markdown.ts`:

1. `marked` with a custom `renderer.code()` that runs `highlight.js` inside the markdown pass
   (no second HTML post-pass).
2. `DOMPurify.sanitize()` strips `<script>`, `on*` handlers, and `javascript:` URIs while
   preserving `class` attributes so highlight.js spans survive.

Output is injected via Svelte's `{@html}` directive. The `github-dark` highlight.js theme CSS
is bundled via `src/main.ts` import.

## Rebuild instructions

```
cd beidou/web/frontend
npm install        # first time only
npm run rebuild    # = scripts/clean-static.sh && vite build
```

`npm run rebuild` runs `scripts/clean-static.sh` before Vite to `git rm` stale hashed assets
from the previous build, preventing orphaned filenames accumulating in the repository.

## Bundle-size budget

Soft target: **≤ 250 kB gzipped JS**. Measure after every build:

```
npm run build && npx vite-bundle-visualizer
```

If highlight.js or Tailwind pulls the bundle over budget, revisit the language allowlist or
purge configuration before merging.

## Commit-the-bundle policy

The compiled output under `beidou/web/static/` is **committed to git**. `pip install -e .`
does not invoke npm; the wheel ships prebuilt assets declared in `pyproject.toml`
`[tool.setuptools.package-data]` under `beidou = ["web/static/*", "web/static/assets/*", ...]`.

There is no CI gate asserting the bundle diff (Node/npm/OS variance produces nondeterministic
hashes). Reviewers eyeball the diff for matched add/remove pairs in `beidou/web/static/assets/`
to catch forgotten rebuilds or stale orphan files.
