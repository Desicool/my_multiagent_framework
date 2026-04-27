import { describe, it, expect, beforeEach } from 'vitest';
import { applyEvent } from './reduce';
import { createInitialState, type ReducerState, STREAM_CAP, GLOBAL_ACTIVITY_CAP } from './state';
import type { BeidouEvent } from '../lib/types';

let s: ReducerState;
beforeEach(() => { s = createInitialState(); });

describe('agent_started', () => {
  it('creates the agent with metadata', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null, role: 'root', model: 'opus' });
    expect(s.agentsById.A).toBeDefined();
    expect(s.agentsById.A.role).toBe('root');
    expect(s.agentsById.A.status).toBe('working');
    expect(s.rootAgentId).toBe('A');
  });

  it('does not set rootAgentId when team_id is non-null (non-root team)', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'B', task_id: 'T', team_id: 'team-1', role: 'member' });
    expect(s.rootAgentId).toBeNull();
  });

  it('stores name from agent_started onto agent state', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null, role: 'root', name: 'root-ab12' });
    expect(s.agentsById.A.name).toBe('root-ab12');
  });

  it('preserves existing name when agent_started fires without name', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null, role: 'root', name: 'root-ab12' });
    applyEvent(s, { type: 'agent_started', ts: 2, agent_id: 'A', task_id: 'T', team_id: null, role: 'root' });
    expect(s.agentsById.A.name).toBe('root-ab12');
  });

  it('preserves _stream and _pendingTools when agent already lazy-created', () => {
    // Pre-create stub via text event
    applyEvent(s, { type: 'assistant_text', ts: 1, caller_id: 'A', message_id: 'm1', text: 'hi' });
    expect(s.agentsById.A._stream.length).toBe(1);
    // Now agent_started arrives — should NOT clear the stream
    applyEvent(s, { type: 'agent_started', ts: 2, agent_id: 'A', task_id: 'T', team_id: null, role: 'engineer' });
    expect(s.agentsById.A._stream.length).toBe(1);
    expect(s.agentsById.A.role).toBe('engineer');
  });
});

describe('lazy stub for unseen agent', () => {
  it('assistant_text on unknown agent creates stub', () => {
    applyEvent(s, { type: 'assistant_text', ts: 1, caller_id: 'X', message_id: 'm1', text: 'hi' });
    expect(s.agentsById.X).toBeDefined();
    expect(s.agentsById.X.status).toBe('unknown');
    expect(s.agentsById.X._stream.length).toBe(1);
  });

  it('tool_called on unknown agent creates stub', () => {
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'X', tool_use_id: 't1', name: 'Bash' });
    expect(s.agentsById.X).toBeDefined();
    expect(s.agentsById.X._stream.length).toBe(1);
  });
});

describe('send_message fan-out', () => {
  it('user→agent: only message_in on recipient', () => {
    applyEvent(s, { type: 'send_message', ts: 1, caller_id: 'user', to: 'A', content: 'hi', message_id: 'm1' });
    expect(s.agentsById.A._stream.length).toBe(1);
    expect(s.agentsById.A._stream[0].kind).toBe('message_in');
    if (s.agentsById.A._stream[0].kind === 'message_in') {
      expect(s.agentsById.A._stream[0].from_is_user).toBe(true);
    }
    expect(s.agentsById.user).toBeUndefined();  // user has no agent stream
  });

  it('agent→agent: message_in on recipient + message_out on sender', () => {
    applyEvent(s, { type: 'send_message', ts: 1, caller_id: 'A', to: 'B', content: 'hi', message_id: 'm1' });
    expect(s.agentsById.A._stream.length).toBe(1);
    expect(s.agentsById.A._stream[0].kind).toBe('message_out');
    expect(s.agentsById.B._stream.length).toBe(1);
    expect(s.agentsById.B._stream[0].kind).toBe('message_in');
    if (s.agentsById.B._stream[0].kind === 'message_in') {
      expect(s.agentsById.B._stream[0].from_is_user).toBe(false);
    }
  });
});

describe('tool span: index-based patching', () => {
  it('tool_called → tool_result (stream not trimmed): patches via stream index, creates new object', () => {
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'A', tool_use_id: 't1', name: 'Bash' });
    // Capture the original stream item reference
    const before = s.agentsById.A._stream[0];
    expect(before.kind).toBe('tool');
    if (before.kind === 'tool') {
      expect(before.duration_ms).toBeNull();
    }
    // _pendingTools stores an index, not an item reference
    expect(s.agentsById.A._pendingTools.get('t1')).toBe(0);
    // Apply tool_result
    applyEvent(s, { type: 'tool_result', ts: 2, caller_id: 'A', tool_use_id: 't1', duration_ms: 42, is_error: false });
    const after = s.agentsById.A._stream[0];
    // The stream item at the same index must be a NEW object (triggers $state proxy set-trap)
    expect(Object.is(before, after)).toBe(false);
    // The new object has the patched values
    expect(after.kind).toBe('tool');
    if (after.kind === 'tool') {
      expect(after.duration_ms).toBe(42);
      expect(after.is_error).toBe(false);
    }
    // _pendingTools entry is removed after patching
    expect(s.agentsById.A._pendingTools.has('t1')).toBe(false);
  });

  it('tool_called → 600 texts → _pendingTools entry is evicted (stream item trimmed away)', () => {
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'A', tool_use_id: 't1', name: 'Bash' });
    // Push enough text items to trim the tool item off the head
    for (let i = 0; i < 600; i++) {
      applyEvent(s, { type: 'assistant_text', ts: 2 + i, caller_id: 'A', message_id: 'm' + i, text: 'x' });
    }
    // Stream should be trimmed to STREAM_CAP
    expect(s.agentsById.A._stream.length).toBe(STREAM_CAP);
    // _pendingTools entry must have been evicted because the index went negative
    expect(s.agentsById.A._pendingTools.has('t1')).toBe(false);
    // Apply tool_result — must be a no-op (no throw)
    applyEvent(s, { type: 'tool_result', ts: 700, caller_id: 'A', tool_use_id: 't1', duration_ms: 42, is_error: false });
    // Stream is still STREAM_CAP; nothing added or changed by the orphaned tool_result
    expect(s.agentsById.A._stream.length).toBe(STREAM_CAP);
  });

  it('tool_called → few texts (within cap) → tool_result: index adjusted correctly, patch fires', () => {
    // tool_called at index 0
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'A', tool_use_id: 't1', name: 'Bash' });
    // Add texts that don't push the tool item out
    for (let i = 0; i < 10; i++) {
      applyEvent(s, { type: 'assistant_text', ts: 2 + i, caller_id: 'A', message_id: 'm' + i, text: 'x' });
    }
    // Stream has 11 items, tool is at index 0 (no trim happened yet)
    expect(s.agentsById.A._stream.length).toBe(11);
    expect(s.agentsById.A._pendingTools.get('t1')).toBe(0);
    // Apply tool_result
    applyEvent(s, { type: 'tool_result', ts: 20, caller_id: 'A', tool_use_id: 't1', duration_ms: 99, is_error: false });
    const item = s.agentsById.A._stream[0];
    expect(item.kind).toBe('tool');
    if (item.kind === 'tool') {
      expect(item.duration_ms).toBe(99);
    }
    expect(s.agentsById.A._pendingTools.has('t1')).toBe(false);
  });

  it('tool_result for unknown tool_use_id is silently ignored', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null });
    // No tool_called first
    applyEvent(s, { type: 'tool_result', ts: 2, caller_id: 'A', tool_use_id: 'nonexistent', duration_ms: 5, is_error: false });
    // Should not throw; agent exists with empty stream
    expect(s.agentsById.A._stream.length).toBe(0);
  });
});

describe('turn.usage dedup', () => {
  it('same caller_id|message_id only counted once', () => {
    applyEvent(s, { type: 'turn.usage', ts: 1, caller_id: 'A', message_id: 'm1', in_tok: 100, out_tok: 50 });
    applyEvent(s, { type: 'turn.usage', ts: 1, caller_id: 'A', message_id: 'm1', in_tok: 100, out_tok: 50 });
    expect(s.agentsById.A.tokens_in).toBe(100);
    expect(s.agentsById.A.tokens_out).toBe(50);
    expect(s.agentsById.A._stream.length).toBe(1);
  });

  it('different message_ids are both counted', () => {
    applyEvent(s, { type: 'turn.usage', ts: 1, caller_id: 'A', message_id: 'm1', in_tok: 100, out_tok: 50 });
    applyEvent(s, { type: 'turn.usage', ts: 2, caller_id: 'A', message_id: 'm2', in_tok: 200, out_tok: 80 });
    expect(s.agentsById.A.tokens_in).toBe(300);
    expect(s.agentsById.A.llm_calls).toBe(2);
    expect(s.agentsById.A._stream.length).toBe(2);
    expect(s.stats.total_tokens).toBe(430);
  });
});

describe('stream cap', () => {
  it('keeps at most STREAM_CAP items, drops head', () => {
    for (let i = 0; i < STREAM_CAP + 100; i++) {
      applyEvent(s, { type: 'assistant_text', ts: i, caller_id: 'A', message_id: 'm' + i, text: 'x' });
    }
    expect(s.agentsById.A._stream.length).toBe(STREAM_CAP);
  });
});

describe('globalActivity cap', () => {
  it('keeps at most GLOBAL_ACTIVITY_CAP items', () => {
    for (let i = 0; i < GLOBAL_ACTIVITY_CAP + 100; i++) {
      applyEvent(s, { type: 'tool_called', ts: i, caller_id: 'A', tool_use_id: 't' + i, name: 'Bash' });
    }
    expect(s.globalActivity.length).toBe(GLOBAL_ACTIVITY_CAP);
  });
});

describe('agent_completed flips status', () => {
  it('marks done', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null });
    applyEvent(s, { type: 'agent_completed', ts: 2, agent_id: 'A' });
    expect(s.agentsById.A.status).toBe('done');
    expect(s.agentsById.A.ended_at).toBe(2);
  });
});

describe('team_created lazy-creates leader', () => {
  it('creates team and stub leader', () => {
    applyEvent(s, { type: 'team_created', ts: 1, team_id: 'T1', parent_team_id: null, leader_agent_id: 'L', name: 'team-A' });
    expect(s.teamsById.T1).toBeDefined();
    expect(s.agentsById.L).toBeDefined();
  });

  it('team_created is idempotent — duplicate events do not overwrite', () => {
    applyEvent(s, { type: 'team_created', ts: 1, team_id: 'T1', parent_team_id: null, leader_agent_id: 'L', name: 'first' });
    applyEvent(s, { type: 'team_created', ts: 2, team_id: 'T1', parent_team_id: null, leader_agent_id: 'L2', name: 'second' });
    expect(s.teamsById.T1.name).toBe('first');
  });
});

describe('question_asked is no-op in reducer', () => {
  it('does not modify state', () => {
    applyEvent(s, { type: 'question_asked', ts: 1, qid: 'q1', asker: 'A', prompt: 'why' });
    expect(Object.keys(s.agentsById)).toHaveLength(0);
    expect(s.globalActivity.length).toBe(0);
  });
});

describe('question_answered pushes answer bubble onto asker stream', () => {
  it('creates a message_in item with from_is_user true and answer_text as content', () => {
    // Pre-create the asker agent
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'agent-A', task_id: 'T', team_id: null });
    // Dispatch question_answered
    applyEvent(s, {
      type: 'question_answered',
      ts: 2,
      agent_id: 'agent-A',
      qid: 'q-42',
      asker: 'agent-A',
      chain_len: 0,
      answers: [{ selected_labels: ['Yes'], text: null }],
      answer_text: 'Yes',
    });
    const stream = s.agentsById['agent-A']._stream;
    const last = stream[stream.length - 1];
    expect(last.kind).toBe('message_in');
    if (last.kind === 'message_in') {
      expect(last.from).toBe('user');
      expect(last.from_is_user).toBe(true);
      expect(last.content).toBe('Yes');
      expect(last.message_id).toBe('q-42');
    }
  });

  it('lazy-creates the asker agent if not yet seen', () => {
    applyEvent(s, {
      type: 'question_answered',
      ts: 3,
      agent_id: 'sys',
      qid: 'q-99',
      asker: 'agent-B',
      chain_len: 1,
      answers: [{ selected_labels: [], text: 'free text answer' }],
      answer_text: 'free text answer',
    });
    expect(s.agentsById['agent-B']).toBeDefined();
    const stream = s.agentsById['agent-B']._stream;
    expect(stream.length).toBe(1);
    expect(stream[0].kind).toBe('message_in');
    if (stream[0].kind === 'message_in') {
      expect(stream[0].content).toBe('free text answer');
      expect(stream[0].message_id).toBe('q-99');
    }
  });
});

describe('agent_error', () => {
  it('sets status to done with detail', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null });
    applyEvent(s, { type: 'agent_error', ts: 2, agent_id: 'A', error: 'timeout' });
    expect(s.agentsById.A.status).toBe('done');
    expect(s.agentsById.A.status_detail).toBe('timeout');
    expect(s.agentsById.A.ended_at).toBe(2);
    expect(s.globalActivity.some(e => e.kind === 'agent_error')).toBe(true);
  });
});

describe('status event', () => {
  it('updates status and detail', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null });
    applyEvent(s, { type: 'status', ts: 2, agent_id: 'A', state: 'blocked', detail: 'waiting for answer' });
    expect(s.agentsById.A.status).toBe('blocked');
    expect(s.agentsById.A.status_detail).toBe('waiting for answer');
  });

  it('unknown state maps to unknown', () => {
    applyEvent(s, { type: 'status', ts: 1, agent_id: 'A', state: 'bogus_state' });
    expect(s.agentsById.A.status).toBe('unknown');
  });
});

describe('run.cost', () => {
  it('accumulates cost on agent and stats', () => {
    applyEvent(s, { type: 'run.cost', ts: 1, agent_id: 'A', total_cost_usd: 0.01 });
    applyEvent(s, { type: 'run.cost', ts: 2, agent_id: 'A', total_cost_usd: 0.02 });
    expect(s.agentsById.A.cost_usd).toBeCloseTo(0.03);
    expect(s.stats.total_cost_usd).toBeCloseTo(0.03);
  });
});

describe('tool_called dedup', () => {
  it('same tool_use_id only counted once', () => {
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'A', tool_use_id: 't1', name: 'Bash' });
    applyEvent(s, { type: 'tool_called', ts: 2, caller_id: 'A', tool_use_id: 't1', name: 'Bash' });
    expect(s.agentsById.A.tool_calls).toBe(1);
    expect(s.agentsById.A._stream.length).toBe(1);
  });
});

describe('contract_violation and config_warning', () => {
  it('contract_violation pushes to globalActivity', () => {
    applyEvent(s, { type: 'contract_violation', ts: 1, agent_id: 'A', reason: 'bad call' });
    expect(s.globalActivity.length).toBe(1);
    expect(s.globalActivity[0].kind).toBe('contract_violation');
    expect(s.globalActivity[0].label).toContain('bad call');
  });

  it('config_warning pushes to globalActivity', () => {
    applyEvent(s, { type: 'config_warning', ts: 1, message: 'deprecated option' });
    expect(s.globalActivity.length).toBe(1);
    expect(s.globalActivity[0].kind).toBe('config_warning');
  });
});

describe('unknown event types', () => {
  it('are silently ignored', () => {
    applyEvent(s, { type: 'future_event_type', ts: 1, foo: 'bar' } as BeidouEvent);
    expect(Object.keys(s.agentsById)).toHaveLength(0);
    expect(s.globalActivity.length).toBe(0);
  });
});

describe('tool_called globalActivity label strips mcp__ prefix', () => {
  it('renders → create_team (not → mcp__beidou__create_team)', () => {
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'A', tool_use_id: 't1', name: 'mcp__beidou__create_team' });
    const activity = s.globalActivity.find(e => e.kind === 'tool_start');
    expect(activity).toBeDefined();
    expect(activity!.label).toBe('→ create_team');
  });

  it('tool_error label also strips mcp__ prefix', () => {
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'A', tool_use_id: 't2', name: 'mcp__beidou__send_message' });
    applyEvent(s, { type: 'tool_result', ts: 2, caller_id: 'A', tool_use_id: 't2', duration_ms: 10, is_error: true });
    const activity = s.globalActivity.find(e => e.kind === 'tool_error');
    expect(activity).toBeDefined();
    expect(activity!.label).toBe('✗ send_message (10ms)');
  });

  it('plain tool names pass through unchanged in globalActivity', () => {
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'A', tool_use_id: 't3', name: 'Bash' });
    const activity = s.globalActivity.find(e => e.kind === 'tool_start');
    expect(activity).toBeDefined();
    expect(activity!.label).toBe('→ Bash');
  });
});
