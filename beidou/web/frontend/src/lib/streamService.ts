import { openTaskStream, type StreamHandle } from './ws';
import { applyEvent, bumpCursor, resetEvents, events } from '../stores/events.svelte';
import { setStatus, setCursor, setAttempts } from '../stores/connection.svelte';
import { triggerRepoll } from '../stores/questions.svelte';
import { getTaskSnapshot } from './api';
import type { BeidouEvent } from './types';

let handle: StreamHandle | null = null;
let currentTaskId: string | null = null;

export async function openTask(taskId: string): Promise<void> {
  if (currentTaskId === taskId && handle) return;
  closeTask();
  currentTaskId = taskId;

  resetEvents();
  setStatus('connecting');
  setAttempts(0);

  let initialCursor = 0;
  try {
    const snap = await getTaskSnapshot(taskId);
    initialCursor = snap.cursor ?? 0;
    if (snap.task) {
      events.task = {
        task_id: snap.task.task_id,
        description: snap.task.description,
        model: snap.task.model,
        skill: snap.task.skill,
        started_at: snap.task.started_at,
        ended_at: snap.task.ended_at ?? null,
        total_cost_usd: snap.task.total_cost_usd ?? 0,
        total_tokens: 0,
        agents_alive: 0,
      };
    }
    for (const t of snap.teams ?? []) {
      applyEvent({
        type: 'team_created',
        ts: t.created_at ?? 0,
        team_id: t.team_id,
        parent_team_id: t.parent_team_id,
        name: t.name,
        leader_agent_id: t.leader_agent_id,
        workspace_path: t.workspace_path,
      } as BeidouEvent);
    }
    for (const a of snap.agents ?? []) {
      applyEvent({
        type: 'agent_started',
        ts: a.started_at ?? 0,
        agent_id: a.agent_id,
        task_id: snap.task?.task_id ?? taskId,
        team_id: a.team_id ?? null,
        role: a.role,
        model: a.model,
        skill: a.skill,
        system_prompt: a.system_prompt,
        tools: a.tools,
        skills: a.skills,
      } as BeidouEvent);
      if (a.ended_at) {
        applyEvent({ type: 'agent_completed', ts: a.ended_at, agent_id: a.agent_id } as BeidouEvent);
      }
      const stub = events.agentsById[a.agent_id];
      if (stub) {
        stub.tokens_in = a.tokens_in ?? 0;
        stub.tokens_out = a.tokens_out ?? 0;
        stub.llm_calls = a.llm_calls ?? 0;
        stub.tool_calls = a.tool_calls ?? 0;
        stub.cost_usd = a.cost_usd ?? 0;
      }
    }
    if (snap.stats?.total_cost_usd) events.stats.total_cost_usd = snap.stats.total_cost_usd;
    if (snap.stats?.total_tokens) events.stats.total_tokens = snap.stats.total_tokens;
    setCursor(initialCursor);
  } catch (e) {
    console.error('snapshot fetch failed', e);
  }

  handle = openTaskStream(taskId, initialCursor, {
    onEvent(ev) {
      applyEvent(ev);
      const t = (ev as { type?: string }).type;
      if (t === 'question_asked' || t === 'question_answered') {
        triggerRepoll();
      }
    },
    onResumed(c) { bumpCursor(c); setCursor(c); },
    onCursorAdvance(c) { bumpCursor(c); setCursor(c); },
    onStatusChange(s) { setStatus(s); },
  });
}

export function closeTask(): void {
  if (handle) { handle.close(); handle = null; }
  currentTaskId = null;
}

export function getCurrentTaskId(): string | null { return currentTaskId; }
