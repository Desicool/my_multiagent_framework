import type {
  AgentState, TeamState, TaskRecord, StreamItem,
} from '../lib/types';

export type ReducerState = {
  task: TaskRecord | null;
  agentsById: Record<string, AgentState>;
  teamsById: Record<string, TeamState>;
  rootAgentId: string | null;
  stats: { total_cost_usd: number; total_tokens: number };
  globalActivity: GlobalActivityItem[];
  cursor: number;
};

export type GlobalActivityItem = {
  ts: number;
  agent_id?: string;
  team_id?: string;
  kind: string;        // 'turn' | 'tool_start' | 'tool_error' | 'team_created' | 'agent_started' | 'agent_completed' | 'agent_error' | 'contract_violation' | 'config_warning' | 'run_cost'
  label: string;       // human-readable summary
};

export const STREAM_CAP = 500;
export const GLOBAL_ACTIVITY_CAP = 500;

export function createInitialState(): ReducerState {
  return {
    task: null,
    agentsById: {},
    teamsById: {},
    rootAgentId: null,
    stats: { total_cost_usd: 0, total_tokens: 0 },
    globalActivity: [],
    cursor: 0,
  };
}

export function createAgentStub(agent_id: string): AgentState {
  return {
    agent_id,
    status: 'unknown',
    tokens_in: 0,
    tokens_out: 0,
    llm_calls: 0,
    tool_calls: 0,
    cost_usd: 0,
    _stream: [],
    _pendingTools: new Map(),
    _seenTurn: new Set(),
    _seenTool: new Set(),
  };
}
