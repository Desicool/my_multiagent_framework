import type { BeidouEvent, AgentState, TeamState, StreamItem, ToolStreamItem } from '../lib/types';
import {
  type ReducerState,
  type GlobalActivityItem,
  STREAM_CAP,
  GLOBAL_ACTIVITY_CAP,
  createAgentStub,
} from './state';
import {
  makeText, makeToolPending, makeMessageIn, makeMessageOut, makeTurnDivider,
} from './streamItem';

/** Lazy-create or fetch the agent slot. Always returns a non-null AgentState. */
function ensureAgent(state: ReducerState, agent_id: string): AgentState {
  if (!agent_id) {
    // Defensive: should not happen but tolerate with a synthetic id.
    agent_id = '__unknown__';
  }
  let a = state.agentsById[agent_id];
  if (!a) {
    a = createAgentStub(agent_id);
    state.agentsById[agent_id] = a;
  }
  return a;
}

/** Push to per-agent stream with cap. */
function pushStream(agent: AgentState, item: StreamItem): void {
  agent._stream.push(item);
  if (agent._stream.length > STREAM_CAP) {
    // Drop from head. _pendingTools survives independently so a tool
    // dropped from _stream can still be patched (and re-rendered as a
    // resurrected card if the consumer cares; default behavior is the
    // map keeps the data, the visible stream loses the head item).
    agent._stream.splice(0, agent._stream.length - STREAM_CAP);
  }
}

function pushGlobalActivity(state: ReducerState, item: GlobalActivityItem): void {
  state.globalActivity.push(item);
  if (state.globalActivity.length > GLOBAL_ACTIVITY_CAP) {
    state.globalActivity.splice(0, state.globalActivity.length - GLOBAL_ACTIVITY_CAP);
  }
}

export function applyEvent(state: ReducerState, ev: BeidouEvent): void {
  switch ((ev as { type: string }).type) {
    case 'agent_started': {
      const e = ev as Extract<BeidouEvent, { type: 'agent_started' }>;
      const a = ensureAgent(state, e.agent_id);
      // Fill in metadata; do NOT clobber _stream / _pendingTools / dedup sets.
      Object.assign(a, {
        task_id: e.task_id ?? a.task_id,
        team_id: e.team_id ?? a.team_id ?? null,
        role: e.role ?? a.role,
        model: e.model ?? a.model,
        skill: e.skill ?? a.skill,
        system_prompt: e.system_prompt ?? a.system_prompt,
        tools: e.tools ?? a.tools,
        skills: e.skills ?? a.skills,
        started_at: e.ts,
        status: a.status === 'unknown' ? 'working' : a.status,
      });
      // Track root: first agent with team_id == null and parent_team_id null is root.
      if (state.rootAgentId == null && (e.team_id == null || e.team_id === null)) {
        state.rootAgentId = e.agent_id;
      }
      // Add to team membership if known.
      if (e.team_id) {
        const t = state.teamsById[e.team_id];
        if (t && !t.agent_ids.includes(e.agent_id)) t.agent_ids.push(e.agent_id);
      }
      pushGlobalActivity(state, { ts: e.ts, agent_id: e.agent_id, kind: 'agent_started', label: `${e.role ?? 'agent'} started` });
      break;
    }

    case 'agent_completed': {
      const e = ev as Extract<BeidouEvent, { type: 'agent_completed' }>;
      const a = ensureAgent(state, e.agent_id);
      a.ended_at = e.ts;
      a.status = 'done';
      pushGlobalActivity(state, { ts: e.ts, agent_id: e.agent_id, kind: 'agent_completed', label: `${a.role ?? 'agent'} completed` });
      break;
    }

    case 'agent_error': {
      const e = ev as Extract<BeidouEvent, { type: 'agent_error' }>;
      const a = ensureAgent(state, e.agent_id);
      a.ended_at = e.ts;
      a.status = 'done';
      a.status_detail = e.error;
      pushGlobalActivity(state, { ts: e.ts, agent_id: e.agent_id, kind: 'agent_error', label: `error: ${e.error}` });
      break;
    }

    case 'status': {
      const e = ev as Extract<BeidouEvent, { type: 'status' }>;
      const a = ensureAgent(state, e.agent_id);
      const valid = ['working', 'blocked', 'idle', 'done'] as const;
      a.status = (valid as readonly string[]).includes(e.state) ? (e.state as AgentState['status']) : 'unknown';
      a.status_detail = e.detail ?? null;
      break;
    }

    case 'team_created': {
      const e = ev as Extract<BeidouEvent, { type: 'team_created' }>;
      if (!state.teamsById[e.team_id]) {
        state.teamsById[e.team_id] = {
          team_id: e.team_id,
          parent_team_id: e.parent_team_id,
          name: e.name,
          leader_agent_id: e.leader_agent_id,
          workspace_path: e.workspace_path,
          created_at: e.ts,
          agent_ids: [],
        } satisfies TeamState;
        // Also lazy-create the leader agent if not yet seen.
        ensureAgent(state, e.leader_agent_id);
        pushGlobalActivity(state, { ts: e.ts, team_id: e.team_id, kind: 'team_created', label: `team ${e.name ?? e.team_id.slice(0,6)} created` });
      }
      break;
    }

    case 'assistant_text': {
      const e = ev as Extract<BeidouEvent, { type: 'assistant_text' }>;
      const a = ensureAgent(state, e.caller_id);
      pushStream(a, makeText(e));
      break;
    }

    case 'tool_called': {
      const e = ev as Extract<BeidouEvent, { type: 'tool_called' }>;
      const a = ensureAgent(state, e.caller_id);
      if (a._seenTool.has(e.tool_use_id)) break;  // dedup if replayed
      a._seenTool.add(e.tool_use_id);
      const item = makeToolPending(e);
      a._pendingTools.set(e.tool_use_id, item);
      pushStream(a, item);
      a.tool_calls += 1;
      pushGlobalActivity(state, { ts: e.ts, agent_id: e.caller_id, kind: 'tool_start', label: `→ ${e.name}` });
      break;
    }

    case 'tool_result': {
      const e = ev as Extract<BeidouEvent, { type: 'tool_result' }>;
      const a = ensureAgent(state, e.caller_id);
      const item = a._pendingTools.get(e.tool_use_id);
      if (item) {
        // Patch in place — the reference is shared with _stream (if not trimmed).
        item.duration_ms = e.duration_ms;
        item.is_error = e.is_error ?? false;
        a._pendingTools.delete(e.tool_use_id);
        if (e.is_error) {
          pushGlobalActivity(state, { ts: e.ts, agent_id: e.caller_id, kind: 'tool_error', label: `✗ ${item.name} (${e.duration_ms ?? 0}ms)` });
        }
      }
      // If item not in _pendingTools (e.g. replay started after the tool_called),
      // we silently drop the patch — the stream item won't exist either.
      break;
    }

    case 'turn.usage': {
      const e = ev as Extract<BeidouEvent, { type: 'turn.usage' }>;
      const a = ensureAgent(state, e.caller_id);
      const dedupKey = `${e.caller_id}|${e.message_id ?? `${e.ts}`}`;
      if (a._seenTurn.has(dedupKey)) break;
      a._seenTurn.add(dedupKey);
      a.tokens_in += e.in_tok ?? 0;
      a.tokens_out += e.out_tok ?? 0;
      a.llm_calls += 1;
      state.stats.total_tokens += (e.in_tok ?? 0) + (e.out_tok ?? 0);
      pushStream(a, makeTurnDivider(e));
      pushGlobalActivity(state, { ts: e.ts, agent_id: e.caller_id, kind: 'turn', label: `turn · in:${e.in_tok ?? 0} out:${e.out_tok ?? 0}` });
      break;
    }

    case 'run.cost': {
      const e = ev as Extract<BeidouEvent, { type: 'run.cost' }>;
      const aid = e.agent_id ?? e.caller_id;
      if (aid) {
        const a = ensureAgent(state, aid);
        a.cost_usd = (a.cost_usd ?? 0) + e.total_cost_usd;
      }
      state.stats.total_cost_usd += e.total_cost_usd;
      pushGlobalActivity(state, { ts: e.ts, agent_id: aid, kind: 'run_cost', label: `cost +$${e.total_cost_usd.toFixed(4)}` });
      break;
    }

    case 'send_message': {
      const e = ev as Extract<BeidouEvent, { type: 'send_message' }>;
      // Inbound on recipient
      const recipient = ensureAgent(state, e.to);
      pushStream(recipient, makeMessageIn({
        ts: e.ts,
        from: e.caller_id,
        from_is_user: e.caller_id === 'user',
        content: e.content,
        message_id: e.message_id,
      }));
      // Outbound on sender — only if sender is an agent (not user)
      if (e.caller_id !== 'user') {
        const sender = ensureAgent(state, e.caller_id);
        pushStream(sender, makeMessageOut({
          ts: e.ts,
          to: e.to,
          content: e.content,
          message_id: e.message_id,
        }));
      }
      break;
    }

    case 'contract_violation': {
      const e = ev as Extract<BeidouEvent, { type: 'contract_violation' }>;
      const aid = e.agent_id ?? e.caller_id;
      pushGlobalActivity(state, { ts: e.ts, agent_id: aid, kind: 'contract_violation', label: `contract violation${e.reason ? ': ' + e.reason : ''}` });
      break;
    }

    case 'config_warning': {
      const e = ev as Extract<BeidouEvent, { type: 'config_warning' }>;
      pushGlobalActivity(state, { ts: e.ts, kind: 'config_warning', label: `warning: ${e.message}` });
      break;
    }

    // question_asked is consumed by stores/questions.svelte.ts (re-poll trigger).
    // The reducer does NOT push it onto any stream (intentional — see plan §5).
    case 'question_asked':
    case 'question_answered':
      break;

    default:
      // Unknown event types are tolerated and ignored.
      break;
  }
}
