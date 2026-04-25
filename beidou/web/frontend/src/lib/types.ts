// ===== JSONL event union =====
// All events share `ts: number` (unix seconds, may be float).

export type AgentStartedEvent = {
  type: 'agent_started';
  ts: number;
  agent_id: string;       // the new agent
  task_id: string;
  team_id: string | null;
  parent_team_id?: string | null;
  role?: string;
  model?: string;
  skill?: string;
  system_prompt?: string;
  tools?: string[];
  skills?: string[];
};

export type AgentCompletedEvent = {
  type: 'agent_completed';
  ts: number;
  agent_id: string;
  reason?: string;
};

export type AgentErrorEvent = {
  type: 'agent_error';
  ts: number;
  agent_id: string;
  error: string;
};

export type StatusEvent = {
  type: 'status';
  ts: number;
  agent_id: string;
  state: string;        // 'working' | 'blocked' | 'idle' | 'done' | other
  detail?: string | null;
};

export type AssistantTextEvent = {
  type: 'assistant_text';
  ts: number;
  caller_id: string;
  message_id?: string;
  text: string;
  stop_reason?: string;
};

export type ToolCalledEvent = {
  type: 'tool_called';
  ts: number;
  caller_id: string;
  message_id?: string;
  tool_use_id: string;
  name: string;
  input?: Record<string, unknown>;
};

export type ToolResultEvent = {
  type: 'tool_result';
  ts: number;
  caller_id: string;
  tool_use_id: string;
  duration_ms: number | null;
  is_error: boolean;
  // NOTE: result body is NOT in the event stream — see plan §6.
};

export type TurnUsageEvent = {
  type: 'turn.usage';
  ts: number;
  caller_id: string;
  message_id?: string;
  model?: string;
  in_tok?: number;
  out_tok?: number;
  cache_read?: number;
  cache_create?: number;
  stop_reason?: string;
};

export type RunCostEvent = {
  type: 'run.cost';
  ts: number;
  agent_id?: string;
  caller_id?: string;
  total_cost_usd: number;
  duration_ms?: number;
  duration_api_ms?: number;
  num_turns?: number;
  stop_reason?: string;
  usage?: Record<string, number>;
  model_usage?: Record<string, unknown>;
  session_id?: string | null;
};

export type SendMessageEvent = {
  type: 'send_message';
  ts: number;
  caller_id: string;       // sender (agent_id or 'user')
  to: string;              // recipient agent_id
  content: string;
  message_id: string;
};

export type TeamCreatedEvent = {
  type: 'team_created';
  ts: number;
  team_id: string;
  parent_team_id: string | null;
  name?: string;
  leader_agent_id: string;
  workspace_path?: string;
};

export type QuestionAskedEvent = {
  type: 'question_asked';
  ts: number;
  qid: string;
  asker: string;
  holder?: string;
  prompt: string;       // truncated to 200 chars by inbox.py
};

export type QuestionAnsweredEvent = {
  type: 'question_answered';
  ts: number;
  qid: string;
  asker?: string;
};

export type ContractViolationEvent = {
  type: 'contract_violation';
  ts: number;
  agent_id?: string;
  caller_id?: string;
  reason?: string;
};

export type ConfigWarningEvent = {
  type: 'config_warning';
  ts: number;
  message: string;
};

export type BeidouEvent =
  | AgentStartedEvent
  | AgentCompletedEvent
  | AgentErrorEvent
  | StatusEvent
  | AssistantTextEvent
  | ToolCalledEvent
  | ToolResultEvent
  | TurnUsageEvent
  | RunCostEvent
  | SendMessageEvent
  | TeamCreatedEvent
  | QuestionAskedEvent
  | QuestionAnsweredEvent
  | ContractViolationEvent
  | ConfigWarningEvent
  | { type: string; ts: number; [k: string]: unknown }; // catch-all for forward compat

// ===== Stream items (per-agent _stream union, see plan §4) =====

export type TextStreamItem = {
  kind: 'text';
  ts: number;
  message_id?: string;
  text: string;
  stop_reason?: string;
};

export type ToolStreamItem = {
  kind: 'tool';
  ts: number;
  tool_use_id: string;
  name: string;
  input?: Record<string, unknown>;
  duration_ms: number | null;   // null = pending
  is_error: boolean | null;     // null = pending
  expanded?: boolean;           // UI-local
};

export type MessageInStreamItem = {
  kind: 'message_in';
  ts: number;
  from: string;                 // sender id (or 'user')
  from_is_user: boolean;
  content: string;
  message_id: string;
};

export type MessageOutStreamItem = {
  kind: 'message_out';
  ts: number;
  to: string;                   // recipient id
  content: string;
  message_id: string;
};

export type TurnDividerStreamItem = {
  kind: 'turn_divider';
  ts: number;
  in_tok: number;
  out_tok: number;
  stop_reason?: string;
  model?: string;
};

export type StreamItem =
  | TextStreamItem
  | ToolStreamItem
  | MessageInStreamItem
  | MessageOutStreamItem
  | TurnDividerStreamItem;

// ===== State shapes =====

export type AgentStatusState = 'working' | 'blocked' | 'idle' | 'done' | 'unknown';

export type AgentState = {
  agent_id: string;
  task_id?: string;
  team_id?: string | null;
  role?: string;
  model?: string;
  skill?: string;
  system_prompt?: string;
  tools?: string[];
  skills?: string[];
  started_at?: number;
  ended_at?: number | null;
  status: AgentStatusState;
  status_detail?: string | null;
  tokens_in: number;
  tokens_out: number;
  llm_calls: number;
  tool_calls: number;
  cost_usd: number;
  _stream: StreamItem[];
  _pendingTools: Map<string, ToolStreamItem>;     // never trimmed; see plan §4 Bootstrap
  _seenTurn: Set<string>;       // dedup key: agent_id|message_id
  _seenTool: Set<string>;       // dedup key: tool_use_id (for tool_called/tool_result pair)
};

export type TeamState = {
  team_id: string;
  task_id?: string;
  parent_team_id: string | null;
  name?: string;
  leader_agent_id: string;
  workspace_path?: string;
  created_at?: number;
  agent_ids: string[];          // members observed via agent_started
};

export type TaskRecord = {
  task_id: string;
  description?: string;
  model?: string;
  skill?: string;
  started_at?: number;
  ended_at?: number | null;
  total_cost_usd: number;
  total_tokens: number;
  agents_alive: number;
};

export type PendingQuestion = {
  qid: string;
  asker_agent_id: string;
  prompt: string;
  context_hint?: string | null;
  chain?: string[];
  created_at: number;
};

export type ConnectionStatus =
  | 'connecting'
  | 'replaying'
  | 'live'
  | 'polling'
  | 'disconnected';

export type ConnectionState = {
  status: ConnectionStatus;
  cursor: number;
  attempts: number;
  retryInMs?: number | null;
};
