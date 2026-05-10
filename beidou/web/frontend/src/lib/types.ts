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
  name?: string;
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
  error_reason?: string | null; // error_reason carries the failure text when is_error=true (truncated to 2000 chars).
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
  members?: Array<{ agent_id: string; role?: string; skill?: string; name?: string }>;
};

export type QuestionAskedEvent = {
  type: 'question_asked';
  ts: number;
  qid: string;
  asker: string;
  holder?: string;
  prompt: string;       // truncated to 200 chars by inbox.py
};

export type StructuredAnswer = {
  selected_labels: string[];
  text: string | null;
  // Server-derived from the matched option's value (defaults to label).
  // Present on question_answered events; clients send only selected_labels + text.
  selected_values?: string[];
};

export type QuestionOption = {
  label: string;
  description: string;
  // Machine discriminator. Defaults to label when absent. Backend mirrors
  // the matched option's value into StructuredAnswer.selected_values.
  value?: string;
  // When true, the banner reveals a free-text textarea and gates submission
  // on a non-empty value once this option is selected.
  requires_text?: boolean;
};

export type SubQuestion = {
  question: string;
  header: string;          // <=12 chars
  multiSelect: boolean;    // camelCase — matches Claude Code wire shape
  options: QuestionOption[];   // length 0 (free-text) or 2..4 (choice)
};

export type AnswerPayload = {
  answers: StructuredAnswer[];
};

export type QuestionAnsweredEvent = {
  type: 'question_answered';
  ts: number;
  agent_id: string;
  qid: string;
  asker: string;
  chain_len: number;
  answers: StructuredAnswer[];
  answer_text: string;
};

export type QuestionEscalatedEvent = {
  type: 'question_escalated';
  ts: number;
  agent_id: string;
  qid: string;
  by: string;
  new_holder: string | null;   // null when escalated to user gateway
  reason: string;
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

export type AgentInputEvent = {
  type: 'agent_input';
  ts: number;
  caller_id: string;        // the receiving agent
  from: string;             // 'user' | 'beidou' | sender agent_id
  message_kind: string;     // 'user' | 'system' | 'terminate' | 'initial'
  source: 'initial' | 'queue';
  content: string;
  message_id: string;       // e.g. '{caller_id}:initial' for the boot task
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
  | QuestionEscalatedEvent
  | ContractViolationEvent
  | ConfigWarningEvent
  | AgentInputEvent
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
  error_reason?: string | null; // null until tool_result arrives or no error
  expanded?: boolean;           // UI-local
};

export type MessageInStreamItem = {
  kind: 'message_in';
  ts: number;
  from: string;                 // sender id (or 'user')
  from_is_user: boolean;
  from_is_system?: boolean;     // true when from === 'beidou' (system notification)
  is_initial?: boolean;         // true when source === 'initial' (boot task bubble)
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
  name?: string;
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
  _pendingTools: Map<string, number>;     // maps tool_use_id → _stream index; entries evicted when stream is trimmed past their index
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
  task_id?: string | null;         // asker's task — used by UI to scope the banner per-task
  asker_agent_id: string;
  questions: SubQuestion[];        // Claude Code wire shape
  prompt: string;                  // derived plain-text fallback (still emitted by backend)
  context_hint?: string | null;
  chain?: string[];
  created_at: number;
};

// ===== Timeline event capture (PR 3) =====
//
// The reducer maintains a flat, append-only `timelineEvents` array of every
// event the /timeline workspace might want to render. The shape stays close to
// the raw JSONL — only the discriminator (`kind`) and a few derived fields
// are normalized so the workspace can render without re-parsing.

export type TimelineEventKind =
  // Lifecycle
  | 'task_started'
  | 'task_completed'
  | 'agent_started'
  | 'agent_completed'
  | 'agent_error'
  | 'agent_exited'
  | 'team_created'
  // Plan
  | 'plan_declared'
  | 'plan_removed'
  | 'task_spawned'
  | 'task_ready'
  | 'task_done'
  | 'task_failed'
  // Primitive call (one of the 13 — drives the section structure on /timeline)
  | 'primitive_call'
  // Questions
  | 'question_asked'
  | 'question_answered'
  | 'question_escalated'
  // Errors
  | 'contract_violation'
  // Termination
  | 'terminate_posted'
  // Firehose-only events (hidden by default; visible under "Show all events")
  | 'turn'
  | 'tool_call'
  | 'status'
  | 'send_message'
  | 'agent_input'
  | 'completion_marker'
  | 'liveness_marker';

export type TimelineEvent = {
  /** Discriminator — drives glyph + category + render path on /timeline. */
  kind: TimelineEventKind;
  ts: number;
  /** The acting agent for this event (caller_id for tool calls, agent_id for lifecycle, etc.). May be empty for system events. */
  agent_id?: string;
  /** Plan-task id for plan_* / task_* events; otherwise undefined. */
  plan_task_id?: string;
  /** Plan id (uuid) for plan_declared / task_spawned / task_done / task_failed events. */
  plan_id?: string;
  /** Spawned-agent id for task_spawned / spawn_agent (primitive_call). */
  spawned_agent_id?: string;
  /** Team id for team_created. */
  team_id?: string;
  /** Plain primitive name (no mcp prefix) for `primitive_call` events. */
  tool_name?: string;
  /** Raw input dict for primitive_call events. */
  input?: Record<string, unknown>;
  /** True when the underlying tool_result reported is_error. */
  is_error?: boolean;
  /** error_reason / detail strings for failures. */
  error?: string;
  /** Free-form one-line summary computed by the reducer for cheap rendering. */
  summary?: string;
  /** Originating raw event preserved verbatim — used by the firehose toggle. */
  raw?: Record<string, unknown>;
};

// ===== Connection state =====

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
