/**
 * Typed fetch wrappers for the Beidou REST API.
 * All response shapes are verified against beidou/web/app.py.
 */
import type { BeidouEvent, PendingQuestion } from './types';

// ---------------------------------------------------------------------------
// Response types (inline — not in types.ts which is reducer-state-only)
// ---------------------------------------------------------------------------

export type TaskListItem = {
  task_id: string;
  description?: string;
  model?: string;
  skill?: string;
  started_at?: number;
  ended_at?: number | null;
  total_cost_usd?: number;
  live_agent_count?: number;
};

export type TaskTeam = {
  team_id: string;
  parent_team_id: string | null;
  name?: string;
  leader_agent_id: string;
  workspace_path?: string;
  created_at?: number;
};

export type TaskAgent = {
  agent_id: string;
  team_id?: string | null;
  role?: string;
  model?: string;
  skill?: string;
  started_at?: number;
  ended_at?: number | null;
  tokens_in?: number;
  tokens_out?: number;
  llm_calls?: number;
  tool_calls?: number;
  cost_usd?: number;
  system_prompt?: string;
  tools?: string[];
  skills?: string[];
  status?: string;
};

export type TaskSnapshot = {
  task: TaskListItem;
  teams: TaskTeam[];
  agents: TaskAgent[];
  stats?: { total_cost_usd?: number; total_tokens?: number };
  cursor: number;
};

/** Agent metadata returned by GET /api/agents/{agent_id} (unwrapped from {agent, events}). */
export type AgentMetadata = {
  agent_id: string;
  team_id?: string | null;
  task_id?: string;
  role?: string;
  model?: string;
  skill?: string;
  system_prompt?: string;
  tools?: string[];
  skills?: string[];
  started_at?: number;
  ended_at?: number | null;
  tokens_in?: number;
  tokens_out?: number;
  llm_calls?: number;
  tool_calls?: number;
  cost_usd?: number;
  status?: string;
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`${resp.status} ${resp.statusText}: ${body}`);
  }
  return (await resp.json()) as T;
}

// ---------------------------------------------------------------------------
// Public API wrappers
// ---------------------------------------------------------------------------

/** GET /api/tasks → TaskListItem[] */
export async function getTasks(): Promise<TaskListItem[]> {
  const r = await fetch('/api/tasks');
  const data = await jsonOrThrow<{ tasks: TaskListItem[] }>(r);
  return data.tasks ?? [];
}

/** GET /api/tasks/{task_id} → TaskSnapshot */
export async function getTaskSnapshot(taskId: string): Promise<TaskSnapshot> {
  const r = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`);
  return jsonOrThrow<TaskSnapshot>(r);
}

/**
 * GET /api/tasks/{task_id}/events?since=&limit=
 * REST polling fallback. `since` is a float unix timestamp (strict >).
 */
export async function fetchEvents(
  taskId: string,
  since: number,
  limit: number = 200,
): Promise<{ events: BeidouEvent[]; cursor: number }> {
  const url = `/api/tasks/${encodeURIComponent(taskId)}/events?since=${since}&limit=${limit}`;
  const r = await fetch(url);
  return jsonOrThrow<{ events: BeidouEvent[]; cursor: number }>(r);
}

/**
 * GET /api/agents/{agent_id} → AgentMetadata
 * Backend returns {agent, events: []}; we unwrap and return agent only.
 */
export async function getAgent(agentId: string): Promise<AgentMetadata> {
  const r = await fetch(`/api/agents/${encodeURIComponent(agentId)}`);
  const data = await jsonOrThrow<{ agent: AgentMetadata; events: unknown[] }>(r);
  return data.agent;
}

/** GET /api/questions/pending → PendingQuestion[] */
export async function getPendingQuestions(): Promise<PendingQuestion[]> {
  const r = await fetch('/api/questions/pending');
  const data = await jsonOrThrow<{ questions: PendingQuestion[] }>(r);
  return data.questions ?? [];
}

/** POST /api/questions/{qid}/answer with body {answer} */
export async function submitAnswer(qid: string, answer: string): Promise<void> {
  const r = await fetch(`/api/questions/${encodeURIComponent(qid)}/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer }),
  });
  await jsonOrThrow<{ ok: boolean }>(r);
}

/** POST /api/agents/{agent_id}/send with body {content} */
export async function sendMessage(agentId: string, content: string): Promise<void> {
  const r = await fetch(`/api/agents/${encodeURIComponent(agentId)}/send`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
  await jsonOrThrow<{ ok: boolean }>(r);
}
