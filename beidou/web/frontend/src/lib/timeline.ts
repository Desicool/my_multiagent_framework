/**
 * /timeline workspace — partitioning, categorization, and section derivation.
 *
 * The /timeline workspace projects raw events captured by the reducer
 * (`ReducerState.timelineEvents`) into a tree of plan-task sections per the
 * Plan B partitioning rule:
 *
 *   1. Find the FIRST `plan_declared` event — this is the root plan.
 *   2. Setup section = events from t0 → first `task_spawned` of the root plan.
 *   3. Each declared root-plan task becomes its own section, bounded by:
 *      - opening event: `task_spawned` for that task_id (or `task_ready` if
 *        no spawn yet — pending stub)
 *      - closing event: `task_done` or `task_failed` for that task_id
 *      - body: every event whose actor is the spawned agent OR a descendant.
 *      Nested `plan_declared` calls inside the section's agent subtree open L1
 *      sub-plan task sections (recursively L2+ for further nesting).
 *   4. Teardown section = events after the last `task_done`/`task_failed` of
 *      the root plan, bounded to operational cascade events
 *      (`terminate_posted`, `agent_completed`, `task_completed`).
 *
 * Default expansion: 3 most-active sub-plan-task sections per parent, by event
 * count; the rest collapse into a `{ kind: 'collapsed', tasks: [...] }` row.
 *
 * Pure projection — no backend or reducer mutation.
 */

import type { TimelineEvent, TimelineEventKind, AgentState, ToolStreamItem } from './types';
import { isPrimitive, bareToolName } from './primitives';

export type SectionKind = 'setup' | 'task' | 'pending' | 'subplan' | 'subplan-failed' | 'teardown';

export type TimelineCategory =
  | 'primitive' | 'lifecycle' | 'plan' | 'question' | 'error' | 'deprecated' | 'firehose';

export type SectionEvent = {
  ev: TimelineEvent;
  /** True for the closing event of the section (task_done / task_failed). */
  closes?: boolean;
  /** True when the closing event indicates failure. */
  closesFail?: boolean;
};

export type Section = {
  kind: SectionKind;
  /** Plan task id (undefined for setup/teardown). */
  task_id?: string;
  /** Role text shown in the header (e.g. "design-coordinator"). */
  role?: string;
  /** Dependency list shown as a chip in the header. */
  depends_on?: string[];
  /** Plan id this section belongs to (root plan or sub-plan). */
  plan_id?: string;
  /** ts of the section's opening event. */
  startTs?: number;
  /** ts of the section's closing event (or last event observed). */
  endTs?: number;
  /** Leader / spawned agent that owns this section. */
  agent_id?: string;
  /** Section body events (filtered to milestones unless firehose). */
  events: SectionEvent[];
  /** L1+ sub-plan task sections (or collapsed groups) nested inside this section. */
  children: (Section | CollapsedSubplans)[];
  /** When `kind === 'pending'`, the dep that's still open. */
  pending_for?: string;
  /** Optional secondary header note (e.g. "closed by terminate_child …"). */
  closingNote?: string;
};

export type CollapsedSubplans = {
  kind: 'collapsed';
  parent_task_id?: string;
  tasks: { task_id: string; role?: string; events: number; lastEvent?: TimelineEvent }[];
};

/** A node rendered in the workspace body — either a Section or a CollapsedSubplans summary. */
export type TimelineNode = Section | CollapsedSubplans;

/** ── Categorization (drives glyph color + filter pills) ──────────────── */

const CATEGORY_BY_KIND: Record<TimelineEventKind, TimelineCategory> = {
  task_started:        'lifecycle',
  task_completed:      'lifecycle',
  agent_started:       'lifecycle',
  agent_completed:     'lifecycle',
  agent_error:         'error',
  agent_exited:        'lifecycle',
  team_created:        'lifecycle',
  plan_declared:       'primitive',  // the primitive call drives section structure
  plan_removed:        'plan',
  task_spawned:        'plan',
  task_ready:          'plan',
  task_done:           'plan',
  task_failed:         'error',
  primitive_call:      'primitive',
  question_asked:      'question',
  question_answered:   'question',
  question_escalated:  'question',
  contract_violation:  'error',
  terminate_posted:    'lifecycle',
  turn:                'firehose',
  tool_call:           'firehose',
  status:              'firehose',
  send_message:        'firehose',
  agent_input:         'firehose',
  completion_marker:   'firehose',
  liveness_marker:     'firehose',
};

export function categoryOf(ev: TimelineEvent): TimelineCategory {
  if (ev.kind === 'primitive_call') {
    if (ev.tool_name === 'report_status' || ev.tool_name === 'create_team') return 'deprecated';
  }
  return CATEGORY_BY_KIND[ev.kind] ?? 'firehose';
}

/** True if this event row is shown when the milestone filter is on. */
export function isMilestone(ev: TimelineEvent): boolean {
  return categoryOf(ev) !== 'firehose';
}

/** ── Glyph + label per kind ─────────────────────────────────────────── */

export function glyphOf(ev: TimelineEvent): string {
  switch (ev.kind) {
    case 'task_started':       return '▶';
    case 'task_completed':     return '▣';
    case 'agent_started':      return '+';
    case 'agent_completed':    return '−';
    case 'agent_error':        return '!';
    case 'agent_exited':       return '⏏';
    case 'team_created':       return '⊞';
    case 'plan_declared':      return '◫';
    case 'plan_removed':       return '⊟';
    case 'task_spawned':       return '⟶';
    case 'task_ready':         return '◯';
    case 'task_done':          return '✓';
    case 'task_failed':        return '✗';
    case 'primitive_call': {
      switch (ev.tool_name) {
        case 'send_message':   return '✉';
        case 'list_peers':     return '☰';
        case 'spawn_agent':    return '⟶';
        case 'terminate_child':return '×';
        case 'create_team':    return '⊞';
        case 'declare_plan':   return '◫';
        case 'remove_plan':    return '⊟';
        case 'list_ready':     return '☰';
        case 'signal_review':  return '◆';
        case 'request_termination': return '⏻';
        case 'report_status':  return '~';
        case 'list_pending_reviews': return '☰';
        case 'ask_user':       return '?';
      }
      return '·';
    }
    case 'question_asked':     return '·';
    case 'question_answered':  return '✓';
    case 'question_escalated': return '↑';
    case 'contract_violation': return '!';
    case 'terminate_posted':   return '×';
    case 'turn':               return '◌';
    case 'tool_call':          return '·';
    case 'status':             return '·';
    case 'send_message':       return '✉';
    case 'agent_input':        return '↩';
    case 'completion_marker':  return '○';
    case 'liveness_marker':    return '·';
  }
  return '·';
}

/** Human-readable event-type label (the bold text on line 1). */
export function labelOf(ev: TimelineEvent): string {
  if (ev.kind === 'primitive_call' && ev.tool_name) return ev.tool_name;
  if (ev.kind === 'turn') return 'turn.usage';
  if (ev.kind === 'completion_marker') return ev.summary ?? 'completion';
  if (ev.kind === 'liveness_marker')   return ev.summary ?? 'liveness';
  return ev.kind;
}

/** ── declare_plan → plan task list ───────────────────────────────────── */

export type PlanTaskSpec = {
  task_id: string;
  role?: string;
  skill?: string;
  depends_on: string[];
};

/** Find the `tool_called` (declare_plan) event corresponding to a `plan_declared`
 *  orchestrator event — same agent, ts within a small window. */
export function findDeclarePlanCall(
  events: readonly TimelineEvent[],
  agentId: string,
  declTs: number,
  windowSec = 2,
): TimelineEvent | undefined {
  let best: TimelineEvent | undefined;
  let bestDelta = Infinity;
  for (const e of events) {
    if (e.kind !== 'primitive_call' || e.tool_name !== 'declare_plan') continue;
    if (e.agent_id !== agentId) continue;
    const dt = Math.abs(e.ts - declTs);
    if (dt > windowSec) continue;
    if (dt < bestDelta) { bestDelta = dt; best = e; }
  }
  return best;
}

/** Extract a plan's task list from a primitive_call declare_plan event. */
export function planTasksFromDeclare(ev: TimelineEvent): PlanTaskSpec[] {
  if (ev.kind !== 'primitive_call' || ev.tool_name !== 'declare_plan') return [];
  const inp = ev.input ?? (ev.raw?.input as Record<string, unknown> | undefined);
  if (!inp) return [];
  const tasks = (inp.tasks ?? inp.plan) as Array<Record<string, unknown>> | undefined;
  if (!Array.isArray(tasks)) return [];
  return tasks.map((t) => ({
    task_id: ((t.id ?? t.task_id) as string | undefined) ?? '',
    role: t.role as string | undefined,
    skill: t.skill as string | undefined,
    depends_on: (t.depends_on as string[] | undefined) ?? [],
  }));
}

/** ── Agent subtree resolution ────────────────────────────────────────── */

/** Build the parent_team / leader chain so we can decide which agents belong
 *  to which section. Walks `task_spawned` events: caller_id (parent) ⇒
 *  spawned_agent_id (child). Returns a set of every agent id reachable from
 *  `root` by repeatedly following these spawn edges. */
export function descendants(events: readonly TimelineEvent[], rootAgentId: string): Set<string> {
  const childrenByParent = new Map<string, string[]>();
  for (const ev of events) {
    if (ev.kind === 'task_spawned' && ev.agent_id && ev.spawned_agent_id) {
      const arr = childrenByParent.get(ev.agent_id) ?? [];
      arr.push(ev.spawned_agent_id);
      childrenByParent.set(ev.agent_id, arr);
    }
    // spawn_agent primitive_call: the spawned id arrives via the matching
    // task_spawned event, so we don't need to add a separate edge here.
  }
  const set = new Set<string>([rootAgentId]);
  const stack = [rootAgentId];
  while (stack.length) {
    const cur = stack.pop()!;
    for (const child of childrenByParent.get(cur) ?? []) {
      if (!set.has(child)) {
        set.add(child);
        stack.push(child);
      }
    }
  }
  return set;
}

/** ── Section derivation ──────────────────────────────────────────────── */

type DeriveOpts = {
  /** Show all events including firehose (turn.usage, status, tool_call, …). */
  showAll?: boolean;
  /** Default expansion budget per parent; rest collapse to a "▸ N more" row. */
  defaultExpand?: number;
};

/** Build the section tree for /timeline. Returns top-level rows (mix of
 *  Section and CollapsedSubplans). */
export function deriveTimeline(
  rawEvents: readonly TimelineEvent[],
  agentsById: Record<string, AgentState>,
  opts: DeriveOpts = {},
): TimelineNode[] {
  const showAll = !!opts.showAll;
  const defaultExpand = opts.defaultExpand ?? 3;

  // Filter to milestones unless firehose toggled. Sort ascending to be safe.
  const events = [...rawEvents].sort((a, b) => a.ts - b.ts);
  const visible = showAll ? events : events.filter(isMilestone);

  // Find root `plan_declared` (orchestrator-emitted, carries plan_id).
  const rootPlanDecl = events.find((e) => e.kind === 'plan_declared');
  if (!rootPlanDecl || !rootPlanDecl.plan_id) {
    // No plan declared yet → render a single "events" section so the workspace
    // isn't empty during boot.
    return [
      {
        kind: 'setup',
        events: visible.map((ev) => ({ ev })),
        children: [],
        startTs: visible[0]?.ts,
        endTs: visible[visible.length - 1]?.ts,
      },
    ];
  }

  const rootPlanId = rootPlanDecl.plan_id;
  const rootPlanAgent = rootPlanDecl.agent_id ?? '';
  // The matching declare_plan primitive_call (carries the task list in input).
  // We look for the closest one in time by the same agent — typically fires
  // a few ms before the plan_declared event.
  const rootPlanCall = findDeclarePlanCall(events, rootPlanAgent, rootPlanDecl.ts);
  const rootPlanTasks = rootPlanCall ? planTasksFromDeclare(rootPlanCall) : [];

  // Index task_spawned / task_done / task_failed by (plan_id, task_id).
  const spawned = new Map<string, TimelineEvent>();   // key: planId|taskId
  const closed = new Map<string, TimelineEvent>();
  for (const e of events) {
    if (e.kind === 'task_spawned' && e.plan_id && e.plan_task_id) {
      spawned.set(`${e.plan_id}|${e.plan_task_id}`, e);
    }
    if ((e.kind === 'task_done' || e.kind === 'task_failed') && e.plan_id && e.plan_task_id) {
      closed.set(`${e.plan_id}|${e.plan_task_id}`, e);
    }
  }

  // Helper: build a section for a plan task. Recursively walks the spawned
  // agent's subtree to find nested plan_declared calls and creates L1+
  // children.
  function buildTaskSection(
    plan_id: string,
    spec: PlanTaskSpec,
    parentDepth = 0,
  ): Section {
    const key = `${plan_id}|${spec.task_id}`;
    const spawnEv = spawned.get(key);
    const closeEv = closed.get(key);

    if (!spawnEv) {
      // Pending stub
      return {
        kind: 'pending',
        task_id: spec.task_id,
        role: spec.role,
        depends_on: spec.depends_on,
        plan_id,
        events: [],
        children: [],
        pending_for: spec.depends_on.join(', '),
      };
    }

    const ownerId = spawnEv.spawned_agent_id ?? '';
    const startTs = spawnEv.ts;
    const endTs = closeEv?.ts ?? events[events.length - 1]?.ts;

    // Find any nested plans declared by the spawned leader directly. We
    // anchor on `plan_declared` events (which carry plan_id) and pull the
    // matching declare_plan tool call to read the task list.
    const nestedDecls = events.filter(
      (e) => e.kind === 'plan_declared'
          && e.agent_id === ownerId && e.ts > spawnEv.ts
          && (closeEv ? e.ts < closeEv.ts : true)
          && e.plan_id !== plan_id,
    );

    const children: Section[] = [];
    /** Set of `${plan_id}|${task_id}` for child tasks. */
    const childTaskKeys = new Set<string>();
    /** Plain task_ids of child tasks (for matching spawn_agent input.task_id). */
    const childTaskIds = new Set<string>();
    for (const npd of nestedDecls) {
      const npid = npd.plan_id ?? '';
      if (!npid) continue;
      const call = findDeclarePlanCall(events, ownerId, npd.ts);
      if (!call) continue;
      for (const spec of planTasksFromDeclare(call)) {
        childTaskKeys.add(`${npid}|${spec.task_id}`);
        childTaskIds.add(spec.task_id);
        children.push(buildTaskSection(npid, spec, parentDepth + 1));
      }
    }

    // L0 body: events whose actor IS the spawned leader directly. Exclude
    // task_spawned and spawn_agent events that route to a child L1 section.
    // Always include the opening event regardless of caller_id.
    const sectionEvents: SectionEvent[] = [];
    // The spawn_agent primitive_call that opened THIS section — also surface
    // it inside the section as the opener pair (paired with task_spawned).
    const spawnPrimitive = events.find(
      (e) => e.kind === 'primitive_call' && e.tool_name === 'spawn_agent'
          && e.input && (e.input as { task_id?: string }).task_id === spec.task_id
          && Math.abs(e.ts - spawnEv.ts) < 5,
    );
    if (spawnPrimitive) sectionEvents.push({ ev: spawnPrimitive });

    for (const e of events) {
      if (e.ts < startTs) continue;
      if (closeEv && e.ts > closeEv.ts) continue;
      if (!isMilestone(e) && !showAll) continue;
      if (e === spawnEv) { sectionEvents.push({ ev: e }); continue; }
      if (e === closeEv) continue;
      if (e === spawnPrimitive) continue;  // already pushed above
      // Exclude events that belong to a child L1 section (open / spawn).
      if (e.kind === 'task_spawned' && e.plan_id && e.plan_task_id
          && childTaskKeys.has(`${e.plan_id}|${e.plan_task_id}`)) {
        continue;
      }
      if (e.kind === 'primitive_call' && e.tool_name === 'spawn_agent' && e.input) {
        const tid = (e.input.task_id as string | undefined);
        if (tid && childTaskIds.has(tid)) continue;
      }
      if (e.agent_id === ownerId) {
        sectionEvents.push({ ev: e });
        continue;
      }
      // team_created where the leader is our spawned agent — surface here.
      if (e.kind === 'team_created' && e.agent_id === ownerId) {
        sectionEvents.push({ ev: e });
      }
    }
    // Re-sort because spawnPrimitive may not be the earliest ts in subscriber order
    sectionEvents.sort((a, b) => a.ev.ts - b.ev.ts);

    // Closing event lives inside the section.
    if (closeEv) {
      sectionEvents.push({
        ev: closeEv,
        closes: true,
        closesFail: closeEv.kind === 'task_failed',
      });
    }

    // For nested sections (parentDepth > 0), apply the "3 most-active" rule
    // when there are >3 children — the caller (top-level section) handles
    // that via collapseChildren below.

    const sectionKind: SectionKind =
      parentDepth === 0
        ? 'task'
        : (closeEv?.kind === 'task_failed' ? 'subplan-failed' : 'subplan');

    return {
      kind: sectionKind,
      task_id: spec.task_id,
      role: spec.role,
      depends_on: spec.depends_on,
      plan_id,
      startTs,
      endTs,
      agent_id: ownerId,
      events: sectionEvents,
      children,
      closingNote: closeEv ? undefined : 'no closing event yet',
    };
  }

  // Build top-level sections.
  const setupEvents: SectionEvent[] = [];
  const firstSpawnTs = (() => {
    for (const e of events) {
      if (e.kind === 'task_spawned' && e.plan_id === rootPlanId) return e.ts;
    }
    return Number.POSITIVE_INFINITY;
  })();
  for (const e of events) {
    if (e.ts >= firstSpawnTs) break;
    if (!isMilestone(e) && !showAll) continue;
    setupEvents.push({ ev: e });
  }

  const setupSection: Section = {
    kind: 'setup',
    events: setupEvents,
    children: [],
    startTs: setupEvents[0]?.ev.ts,
    endTs: setupEvents[setupEvents.length - 1]?.ev.ts,
  };

  const taskSections = rootPlanTasks.map((spec) => buildTaskSection(rootPlanId, spec, 0));

  // Apply default-expand to children of each task section. children may
  // already include CollapsedSubplans groups from a deeper recursion, but at
  // the buildTaskSection call site they're always Section[].
  for (const ts of taskSections) {
    ts.children = collapseChildren(ts.children as Section[], defaultExpand);
  }

  // Teardown: events after the last close of any root-plan task. Limit to
  // operational cascade kinds.
  let lastClose = 0;
  for (const t of rootPlanTasks) {
    const ce = closed.get(`${rootPlanId}|${t.task_id}`);
    if (ce) lastClose = Math.max(lastClose, ce.ts);
  }
  const teardownKinds: ReadonlySet<TimelineEventKind> = new Set([
    'terminate_posted', 'agent_completed', 'agent_exited', 'task_completed',
    // Allow primitive_call=terminate_child too
    'primitive_call',
  ]);
  const teardownEvents: SectionEvent[] = [];
  if (lastClose > 0) {
    for (const e of events) {
      if (e.ts <= lastClose) continue;
      if (!teardownKinds.has(e.kind)) continue;
      if (e.kind === 'primitive_call' && e.tool_name !== 'terminate_child') continue;
      if (!isMilestone(e) && !showAll) continue;
      teardownEvents.push({ ev: e });
    }
  }
  const teardownSection: Section | null = teardownEvents.length
    ? {
        kind: 'teardown',
        events: teardownEvents,
        children: [],
        startTs: teardownEvents[0]?.ev.ts,
        endTs: teardownEvents[teardownEvents.length - 1]?.ev.ts,
      }
    : null;

  const out: TimelineNode[] = [setupSection, ...taskSections];
  if (teardownSection) out.push(teardownSection);
  return out;
}

/** Apply the "3 most-active" expansion rule to a list of sub-plan-task sections. */
function collapseChildren(children: Section[], defaultExpand: number): (Section | CollapsedSubplans)[] {
  if (children.length <= defaultExpand) return children;

  const scored = children.map((c, idx) => ({
    idx,
    score: c.events.length + c.children.length * 2,
  }));
  scored.sort((a, b) => b.score - a.score || a.idx - b.idx);
  const expandedIdx = new Set(scored.slice(0, defaultExpand).map((s) => s.idx));

  const out: (Section | CollapsedSubplans)[] = [];
  let pending: { task_id: string; role?: string; events: number; lastEvent?: TimelineEvent }[] = [];

  function flushPending(): void {
    if (pending.length === 0) return;
    out.push({ kind: 'collapsed', tasks: pending });
    pending = [];
  }

  for (let i = 0; i < children.length; i++) {
    if (expandedIdx.has(i)) {
      flushPending();
      out.push(children[i]);
    } else {
      const c = children[i];
      const last = c.events[c.events.length - 1]?.ev;
      pending.push({
        task_id: c.task_id ?? '?',
        role: c.role,
        events: c.events.length,
        lastEvent: last,
      });
    }
  }
  flushPending();
  return out;
}

export function isCollapsed(node: TimelineNode): node is CollapsedSubplans {
  return (node as CollapsedSubplans).kind === 'collapsed';
}

/** Count milestone-eligible events by category for the filter pills. */
export function countByCategory(events: readonly TimelineEvent[]): Record<TimelineCategory | 'all', number> {
  const c: Record<TimelineCategory | 'all', number> = {
    all: 0, primitive: 0, lifecycle: 0, plan: 0, question: 0, error: 0, deprecated: 0, firehose: 0,
  };
  for (const ev of events) {
    const cat = categoryOf(ev);
    if (cat === 'firehose') continue;
    c.all += 1;
    c[cat] += 1;
  }
  return c;
}
