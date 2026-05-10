import { describe, expect, it } from 'vitest';
import {
  categoryOf, glyphOf, isMilestone, labelOf,
  planTasksFromDeclare, descendants, deriveTimeline, countByCategory,
} from './timeline';
import type { TimelineEvent, AgentState } from './types';

function ev(p: Partial<TimelineEvent>): TimelineEvent {
  return { kind: 'agent_started', ts: 0, ...p } as TimelineEvent;
}

describe('categoryOf', () => {
  it('classifies primitives, lifecycle, plan, questions, errors', () => {
    expect(categoryOf(ev({ kind: 'primitive_call', tool_name: 'send_message' }))).toBe('primitive');
    expect(categoryOf(ev({ kind: 'agent_started' }))).toBe('lifecycle');
    expect(categoryOf(ev({ kind: 'task_done' }))).toBe('plan');
    expect(categoryOf(ev({ kind: 'question_asked' }))).toBe('question');
    expect(categoryOf(ev({ kind: 'task_failed' }))).toBe('error');
    expect(categoryOf(ev({ kind: 'agent_error' }))).toBe('error');
  });

  it('marks deprecated primitives as deprecated', () => {
    expect(categoryOf(ev({ kind: 'primitive_call', tool_name: 'report_status' }))).toBe('deprecated');
    expect(categoryOf(ev({ kind: 'primitive_call', tool_name: 'create_team' }))).toBe('deprecated');
  });

  it('hides firehose events from milestone filter', () => {
    expect(isMilestone(ev({ kind: 'turn' }))).toBe(false);
    expect(isMilestone(ev({ kind: 'tool_call' }))).toBe(false);
    expect(isMilestone(ev({ kind: 'status' }))).toBe(false);
    expect(isMilestone(ev({ kind: 'primitive_call', tool_name: 'send_message' }))).toBe(true);
  });
});

describe('glyphOf', () => {
  it('returns a non-empty glyph for every kind', () => {
    const kinds = [
      'task_started', 'task_completed', 'agent_started', 'agent_completed',
      'agent_error', 'team_created', 'plan_declared', 'task_spawned', 'task_done',
      'task_failed', 'question_asked', 'question_escalated', 'terminate_posted',
    ] as const;
    for (const k of kinds) {
      expect(glyphOf(ev({ kind: k })).length).toBeGreaterThan(0);
    }
  });

  it('picks the right glyph per primitive', () => {
    expect(glyphOf(ev({ kind: 'primitive_call', tool_name: 'send_message' }))).toBe('✉');
    expect(glyphOf(ev({ kind: 'primitive_call', tool_name: 'signal_review' }))).toBe('◆');
    expect(glyphOf(ev({ kind: 'primitive_call', tool_name: 'ask_user' }))).toBe('?');
    expect(glyphOf(ev({ kind: 'primitive_call', tool_name: 'terminate_child' }))).toBe('×');
  });
});

describe('labelOf', () => {
  it('uses the bare tool name for primitive calls', () => {
    expect(labelOf(ev({ kind: 'primitive_call', tool_name: 'signal_review' }))).toBe('signal_review');
  });
  it('falls back to kind for non-primitives', () => {
    expect(labelOf(ev({ kind: 'agent_started' }))).toBe('agent_started');
    expect(labelOf(ev({ kind: 'task_done' }))).toBe('task_done');
  });
});

describe('planTasksFromDeclare', () => {
  it('extracts tasks from input.tasks', () => {
    const e = ev({
      kind: 'primitive_call',
      tool_name: 'declare_plan',
      input: { tasks: [
        { id: 'phase1', role: 'design-coordinator', depends_on: [] },
        { id: 'phase2', role: 'impl-coordinator', depends_on: ['phase1'] },
      ] },
    });
    const tasks = planTasksFromDeclare(e);
    expect(tasks).toHaveLength(2);
    expect(tasks[0].task_id).toBe('phase1');
    expect(tasks[1].depends_on).toEqual(['phase1']);
  });

  it('returns empty for non-declare_plan events', () => {
    expect(planTasksFromDeclare(ev({ kind: 'primitive_call', tool_name: 'send_message' }))).toEqual([]);
  });
});

describe('descendants', () => {
  it('walks task_spawned edges from root', () => {
    const events: TimelineEvent[] = [
      ev({ kind: 'task_spawned', ts: 1, agent_id: 'A', spawned_agent_id: 'B' }),
      ev({ kind: 'task_spawned', ts: 2, agent_id: 'B', spawned_agent_id: 'C' }),
      ev({ kind: 'task_spawned', ts: 3, agent_id: 'X', spawned_agent_id: 'Y' }),
    ];
    const set = descendants(events, 'A');
    expect(set.has('A')).toBe(true);
    expect(set.has('B')).toBe(true);
    expect(set.has('C')).toBe(true);
    expect(set.has('Y')).toBe(false);
  });
});

describe('deriveTimeline', () => {
  it('produces setup + task sections + teardown for a 2-task plan', () => {
    const events: TimelineEvent[] = [
      ev({ kind: 'task_started', ts: 1, plan_task_id: 'tsk_x' }),
      ev({ kind: 'agent_started', ts: 2, agent_id: 'root' }),
      ev({ kind: 'primitive_call', ts: 3, agent_id: 'root', tool_name: 'declare_plan',
           input: { tasks: [
             { id: 'phase1', role: 'designer', depends_on: [] },
             { id: 'phase2', role: 'impl', depends_on: ['phase1'] },
           ] } }),
      ev({ kind: 'plan_declared', ts: 3.1, agent_id: 'root', plan_id: 'plan-1' }),
      ev({ kind: 'task_spawned', ts: 4, agent_id: 'root', plan_task_id: 'phase1', plan_id: 'plan-1', spawned_agent_id: 'a1' }),
      ev({ kind: 'agent_started', ts: 5, agent_id: 'a1' }),
      ev({ kind: 'primitive_call', ts: 6, agent_id: 'a1', tool_name: 'signal_review' }),
      ev({ kind: 'task_done', ts: 7, plan_task_id: 'phase1', plan_id: 'plan-1' }),
      ev({ kind: 'task_spawned', ts: 8, agent_id: 'root', plan_task_id: 'phase2', plan_id: 'plan-1', spawned_agent_id: 'a2' }),
      ev({ kind: 'agent_started', ts: 9, agent_id: 'a2' }),
      ev({ kind: 'task_done', ts: 10, plan_task_id: 'phase2', plan_id: 'plan-1' }),
      ev({ kind: 'primitive_call', ts: 11, agent_id: 'root', tool_name: 'terminate_child' }),
      ev({ kind: 'agent_completed', ts: 12, agent_id: 'a2' }),
    ];
    const tree = deriveTimeline(events, {} as Record<string, AgentState>);
    expect(tree).toHaveLength(4);  // setup + 2 tasks + teardown
    expect(tree[0].kind).toBe('setup');
    expect((tree[1] as { kind: string }).kind).toBe('task');
    expect((tree[2] as { kind: string }).kind).toBe('task');
    expect((tree[3] as { kind: string }).kind).toBe('teardown');
  });

  it('renders pending stub when a plan task has no spawn yet', () => {
    const events: TimelineEvent[] = [
      ev({ kind: 'primitive_call', ts: 1, agent_id: 'root', tool_name: 'declare_plan',
           input: { tasks: [
             { id: 'phase1', role: 'designer', depends_on: [] },
             { id: 'phase2', role: 'impl', depends_on: ['phase1'] },
           ] } }),
      ev({ kind: 'plan_declared', ts: 1.1, agent_id: 'root', plan_id: 'plan-1' }),
      ev({ kind: 'task_spawned', ts: 2, agent_id: 'root', plan_task_id: 'phase1', plan_id: 'plan-1', spawned_agent_id: 'a1' }),
    ];
    const tree = deriveTimeline(events, {} as Record<string, AgentState>);
    const phase2 = (tree as Array<{ kind: string; task_id?: string }>).find((s) => s.task_id === 'phase2');
    expect(phase2?.kind).toBe('pending');
  });

  it('falls back to a single setup section when no plan_declared has fired', () => {
    const events: TimelineEvent[] = [
      ev({ kind: 'agent_started', ts: 1, agent_id: 'root' }),
    ];
    const tree = deriveTimeline(events, {} as Record<string, AgentState>);
    expect(tree).toHaveLength(1);
    expect(tree[0].kind).toBe('setup');
  });
});

describe('countByCategory', () => {
  it('counts only milestone events', () => {
    const events: TimelineEvent[] = [
      ev({ kind: 'agent_started' }),
      ev({ kind: 'agent_started' }),
      ev({ kind: 'primitive_call', tool_name: 'signal_review' }),
      ev({ kind: 'turn' }),
      ev({ kind: 'task_failed' }),
    ];
    const c = countByCategory(events);
    expect(c.all).toBe(4);
    expect(c.lifecycle).toBe(2);
    expect(c.primitive).toBe(1);
    expect(c.error).toBe(1);
    expect(c.firehose).toBe(0);
  });
});
