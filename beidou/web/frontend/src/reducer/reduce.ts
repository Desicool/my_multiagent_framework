import type { BeidouEvent, AgentState, TeamState, StreamItem, ToolStreamItem, TimelineEvent } from '../lib/types';
import {
  type ReducerState,
  type GlobalActivityItem,
  STREAM_CAP,
  GLOBAL_ACTIVITY_CAP,
  TIMELINE_CAP,
  createAgentStub,
} from './state';
import {
  makeText, makeToolPending, makeMessageIn, makeMessageOut, makeTurnDivider,
} from './streamItem';
import { displayToolName } from '../lib/format';
import { bareToolName, isPrimitive } from '../lib/primitives';

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
    // Drop from head. _pendingTools stores _stream indices, so we must
    // adjust (decrement) all stored indices by the number of dropped items.
    // Entries whose index becomes negative had their tool item trimmed away;
    // patching them is no longer meaningful so we evict those entries.
    const dropCount = agent._stream.length - STREAM_CAP;
    for (const [id, oldIdx] of agent._pendingTools) {
      const newIdx = oldIdx - dropCount;
      if (newIdx < 0) {
        agent._pendingTools.delete(id);
      } else {
        agent._pendingTools.set(id, newIdx);
      }
    }
    agent._stream.splice(0, dropCount);
  }
}

function pushGlobalActivity(state: ReducerState, item: GlobalActivityItem): void {
  state.globalActivity.push(item);
  if (state.globalActivity.length > GLOBAL_ACTIVITY_CAP) {
    state.globalActivity.splice(0, state.globalActivity.length - GLOBAL_ACTIVITY_CAP);
  }
}

function pushTimeline(state: ReducerState, ev: TimelineEvent): void {
  state.timelineEvents.push(ev);
  if (state.timelineEvents.length > TIMELINE_CAP) {
    state.timelineEvents.splice(0, state.timelineEvents.length - TIMELINE_CAP);
  }
}

/** Build a TimelineEvent from a raw event dict — used for forward-compat events
 *  (`task_done`, `task_spawned`, `plan_declared`, etc.) that aren't part of
 *  the typed BeidouEvent union but still flow through `applyEvent`. */
function timelineFromRaw(raw: Record<string, unknown>, kind: TimelineEvent['kind']): TimelineEvent {
  const r = raw;
  return {
    kind,
    ts: (r.ts as number) ?? 0,
    agent_id: (r.agent_id as string) || (r.caller_id as string) || undefined,
    plan_task_id: (r.task_id as string) || undefined,
    plan_id: (r.plan_id as string) || undefined,
    spawned_agent_id: (r.spawned_agent_id as string) || undefined,
    team_id: (r.team_id as string) || undefined,
    raw: { ...r },
  };
}

/** Capture milestone-eligible events into `state.timelineEvents`. Runs before
 *  the main switch so the firehose toggle can still see them even if they
 *  aren't part of the typed BeidouEvent union (task_done, plan_declared, …). */
function captureTimeline(state: ReducerState, ev: BeidouEvent): void {
  const raw = ev as unknown as Record<string, unknown>;
  const type = (raw.type ?? raw.event) as string | undefined;
  if (!type) return;

  switch (type) {
    case 'task_started': {
      const skill = raw.skill as string | undefined;
      const desc = (raw.task as string | undefined)?.slice(0, 200);
      pushTimeline(state, {
        kind: 'task_started', ts: raw.ts as number,
        plan_task_id: raw.task_id as string,
        summary: skill ? `skill ${skill}` : '',
        raw, ...(desc ? {} : {}),
      });
      // Surface the task description in the summary; we keep raw for full text.
      const evt = state.timelineEvents[state.timelineEvents.length - 1];
      if (evt && desc) evt.summary = `skill ${skill ?? '?'} · ${desc}`;
      return;
    }
    case 'task_completed': {
      pushTimeline(state, { kind: 'task_completed', ts: raw.ts as number, plan_task_id: raw.task_id as string, raw });
      return;
    }
    case 'agent_started': {
      pushTimeline(state, {
        kind: 'agent_started', ts: raw.ts as number,
        agent_id: raw.agent_id as string,
        team_id: raw.team_id as string | undefined,
        summary: `role ${raw.role ?? '?'} · skill ${raw.skill ?? '?'}`,
        raw,
      });
      return;
    }
    case 'agent_completed': {
      pushTimeline(state, {
        kind: 'agent_completed', ts: raw.ts as number,
        agent_id: raw.agent_id as string,
        summary: (raw.reason as string | undefined) ?? '',
        raw,
      });
      return;
    }
    case 'agent_error': {
      pushTimeline(state, {
        kind: 'agent_error', ts: raw.ts as number,
        agent_id: raw.agent_id as string,
        is_error: true,
        error: (raw.error as string | undefined) ?? '',
        raw,
      });
      return;
    }
    case 'agent_exited': {
      pushTimeline(state, { kind: 'agent_exited', ts: raw.ts as number, agent_id: raw.agent_id as string, summary: (raw.reason as string | undefined) ?? '', raw });
      return;
    }
    case 'team_created': {
      pushTimeline(state, {
        kind: 'team_created', ts: raw.ts as number,
        agent_id: raw.leader_agent_id as string,
        team_id: raw.team_id as string,
        summary: `team ${(raw.name as string | undefined) ?? (raw.team_id as string).slice(0, 6)} · leader ${raw.leader_agent_id ?? '?'}`,
        raw,
      });
      return;
    }
    case 'plan_declared': {
      pushTimeline(state, {
        kind: 'plan_declared', ts: raw.ts as number,
        agent_id: raw.agent_id as string,
        plan_id: raw.plan_id as string,
        summary: `${raw.task_count ?? '?'} tasks declared`,
        raw,
      });
      return;
    }
    case 'plan_removed': {
      pushTimeline(state, { kind: 'plan_removed', ts: raw.ts as number, agent_id: raw.agent_id as string, plan_id: raw.plan_id as string, raw });
      return;
    }
    case 'task_spawned': {
      pushTimeline(state, {
        kind: 'task_spawned', ts: raw.ts as number,
        agent_id: raw.agent_id as string,
        plan_task_id: raw.task_id as string,
        plan_id: raw.plan_id as string,
        spawned_agent_id: raw.spawned_agent_id as string,
        team_id: raw.team_id as string | undefined,
        raw,
      });
      return;
    }
    case 'task_ready': {
      pushTimeline(state, { kind: 'task_ready', ts: raw.ts as number, plan_task_id: raw.task_id as string, plan_id: raw.plan_id as string, raw });
      return;
    }
    case 'task_done': {
      pushTimeline(state, { kind: 'task_done', ts: raw.ts as number, plan_task_id: raw.task_id as string, plan_id: raw.plan_id as string, summary: 'plan task done', raw });
      return;
    }
    case 'task_failed': {
      pushTimeline(state, { kind: 'task_failed', ts: raw.ts as number, plan_task_id: raw.task_id as string, plan_id: raw.plan_id as string, is_error: true, summary: (raw.reason as string | undefined) ?? 'plan task failed', raw });
      return;
    }
    case 'tool_called': {
      const name = raw.name as string;
      if (isPrimitive(name)) {
        pushTimeline(state, {
          kind: 'primitive_call', ts: raw.ts as number,
          agent_id: raw.caller_id as string,
          tool_name: bareToolName(name),
          input: raw.input as Record<string, unknown> | undefined,
          raw,
        });
      } else {
        // Captured for the firehose toggle only.
        pushTimeline(state, {
          kind: 'tool_call', ts: raw.ts as number,
          agent_id: raw.caller_id as string,
          tool_name: bareToolName(name),
          input: raw.input as Record<string, unknown> | undefined,
          raw,
        });
      }
      return;
    }
    case 'question_asked': {
      pushTimeline(state, {
        kind: 'question_asked', ts: raw.ts as number,
        agent_id: raw.asker as string,
        summary: (raw.holder ? `held by ${raw.holder}` : ''),
        raw,
      });
      return;
    }
    case 'question_answered': {
      pushTimeline(state, {
        kind: 'question_answered', ts: raw.ts as number,
        agent_id: raw.asker as string,
        summary: `chain ${raw.chain_len ?? '?'}`,
        raw,
      });
      return;
    }
    case 'question_escalated': {
      pushTimeline(state, {
        kind: 'question_escalated', ts: raw.ts as number,
        agent_id: (raw.by as string) ?? (raw.agent_id as string),
        summary: raw.new_holder == null ? '→ user' : `→ ${raw.new_holder}`,
        raw,
      });
      return;
    }
    case 'contract_violation': {
      pushTimeline(state, {
        kind: 'contract_violation', ts: raw.ts as number,
        agent_id: (raw.agent_id as string) ?? (raw.caller_id as string),
        is_error: true,
        error: raw.reason as string | undefined,
        summary: (raw.reason as string | undefined) ?? '',
        raw,
      });
      return;
    }
    case 'terminate_posted': {
      pushTimeline(state, {
        kind: 'terminate_posted', ts: raw.ts as number,
        agent_id: raw.caller_id as string,
        spawned_agent_id: raw.agent_id as string,
        raw,
      });
      return;
    }
    // Firehose-only capture (hidden until "Show all events" toggles).
    case 'turn.usage': {
      pushTimeline(state, { kind: 'turn', ts: raw.ts as number, agent_id: raw.caller_id as string, raw });
      return;
    }
    case 'status': {
      pushTimeline(state, { kind: 'status', ts: raw.ts as number, agent_id: raw.agent_id as string, summary: (raw.state as string) ?? '', raw });
      return;
    }
    case 'send_message': {
      pushTimeline(state, { kind: 'send_message', ts: raw.ts as number, agent_id: raw.caller_id as string, raw });
      return;
    }
    case 'agent_input': {
      pushTimeline(state, { kind: 'agent_input', ts: raw.ts as number, agent_id: raw.caller_id as string, raw });
      return;
    }
    case 'completion.reported':
    case 'completion.rework':
    case 'completion.approved':
    case 'completion.reping': {
      pushTimeline(state, { kind: 'completion_marker', ts: raw.ts as number, agent_id: raw.agent_id as string, summary: type, raw });
      return;
    }
    case 'liveness_check':
    case 'liveness.nudge':
    case 'liveness.escalated_to_user': {
      pushTimeline(state, { kind: 'liveness_marker', ts: raw.ts as number, agent_id: raw.agent_id as string, summary: type, raw });
      return;
    }
    default:
      return;
  }
}

export function applyEvent(state: ReducerState, ev: BeidouEvent): void {
  captureTimeline(state, ev);
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
        name: e.name ?? a.name,
        system_prompt: e.system_prompt ?? a.system_prompt,
        tools: e.tools ?? a.tools,
        skills: e.skills ?? a.skills,
        started_at: e.ts,
        status: a.status === 'unknown' ? 'working' : a.status,
      });
      // Track root: detect via role='root'. The root agent now spawns with
      // team_id=null (no synthetic tm_root team).
      if (state.rootAgentId == null && e.role === 'root') {
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
        // Persist name from members[] entries onto each member's AgentState.
        if (e.members) {
          for (const m of e.members) {
            const ma = ensureAgent(state, m.agent_id);
            ma.name = m.name ?? ma.name;
          }
        }
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
      pushStream(a, item);
      // Store the stream index (after push, it's the last element).
      const idx = a._stream.length - 1;
      a._pendingTools.set(e.tool_use_id, idx);
      a.tool_calls += 1;
      pushGlobalActivity(state, { ts: e.ts, agent_id: e.caller_id, kind: 'tool_start', label: `→ ${displayToolName(e.name)}` });
      break;
    }

    case 'tool_result': {
      const e = ev as Extract<BeidouEvent, { type: 'tool_result' }>;
      const a = ensureAgent(state, e.caller_id);
      const idx = a._pendingTools.get(e.tool_use_id);
      if (idx !== undefined) {
        const item = a._stream[idx];
        if (item && item.kind === 'tool') {
          // Replace the array element with a new object so the Svelte 5 $state
          // proxy set-trap fires and subscribers (e.g. ToolCard derived) are
          // notified. Mutating the old item in-place would bypass the proxy.
          a._stream[idx] = { ...item, duration_ms: e.duration_ms, is_error: e.is_error ?? false, error_reason: e.error_reason ?? null };
          if (e.is_error) {
            pushGlobalActivity(state, { ts: e.ts, agent_id: e.caller_id, kind: 'tool_error', label: `✗ ${displayToolName(item.name)} (${e.duration_ms ?? 0}ms)` });
          }
        }
        a._pendingTools.delete(e.tool_use_id);
      }
      // If idx not in _pendingTools (e.g. replay started after the tool_called,
      // or the entry was evicted because _stream was trimmed past it),
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
      // Inbound on recipient is now handled by agent_input (delivery-side event).
      // Only render the outbound bubble on the sender.
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

    case 'agent_input': {
      const e = ev as Extract<BeidouEvent, { type: 'agent_input' }>;
      const recipient = ensureAgent(state, e.caller_id);
      // Dedup: drop if a message_in with this message_id is already on the stream
      // (guards against SSE reconnect/replay delivering the same event twice).
      const alreadySeen = recipient._stream.some(
        item => item.kind === 'message_in' && item.message_id === e.message_id,
      );
      if (alreadySeen) break;
      pushStream(recipient, makeMessageIn({
        ts: e.ts,
        from: e.from,
        from_is_user: e.from === 'user',
        from_is_system: e.from === 'beidou',
        is_initial: e.source === 'initial',
        content: e.content,
        message_id: e.message_id,
      }));
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

    // question_asked / question_escalated are consumed by stores/questions.svelte.ts
    // (re-poll trigger via streamService.ts). The reducer does NOT push them onto
    // any stream (intentional — see plan §5). question_escalated fires when a
    // holder forwards a question one hop up the chain — including to the user
    // gateway, which is what flips chain[-1] to "USER" and lets the banner appear.
    case 'question_asked':
    case 'question_escalated':
      break;

    case 'question_answered': {
      const e = ev as Extract<BeidouEvent, { type: 'question_answered' }>;
      const asker = ensureAgent(state, e.asker);
      pushStream(asker, makeMessageIn({
        ts: e.ts,
        from: 'user',
        from_is_user: true,
        content: e.answer_text,
        message_id: e.qid,
      }));
      break;
    }

    default:
      // Unknown event types are tolerated and ignored.
      break;
  }
}
