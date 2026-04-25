import { describe, it, expect, beforeEach } from 'vitest';
import { applyEvent } from './reduce';
import { createInitialState, type ReducerState, STREAM_CAP, GLOBAL_ACTIVITY_CAP } from './state';
import type { BeidouEvent } from '../lib/types';

let s: ReducerState;
beforeEach(() => { s = createInitialState(); });

describe('agent_started', () => {
  it('creates the agent with metadata', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null, role: 'engineer', model: 'opus' });
    expect(s.agentsById.A).toBeDefined();
    expect(s.agentsById.A.role).toBe('engineer');
    expect(s.agentsById.A.status).toBe('working');
    expect(s.rootAgentId).toBe('A');
  });

  it('does not set rootAgentId when team_id is non-null', () => {
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'B', task_id: 'T', team_id: 'team-1', role: 'member' });
    expect(s.rootAgentId).toBeNull();
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

describe('tool span survives _stream trimming', () => {
  it('tool_called → 600 texts → tool_result: patches via _pendingTools', () => {
    applyEvent(s, { type: 'tool_called', ts: 1, caller_id: 'A', tool_use_id: 't1', name: 'Bash' });
    for (let i = 0; i < 600; i++) {
      applyEvent(s, { type: 'assistant_text', ts: 2 + i, caller_id: 'A', message_id: 'm' + i, text: 'x' });
    }
    // Stream should be trimmed to STREAM_CAP
    expect(s.agentsById.A._stream.length).toBe(STREAM_CAP);
    // _pendingTools must still have the tool item
    expect(s.agentsById.A._pendingTools.has('t1')).toBe(true);
    const pending = s.agentsById.A._pendingTools.get('t1')!;
    expect(pending.duration_ms).toBeNull();
    // Apply tool_result
    applyEvent(s, { type: 'tool_result', ts: 700, caller_id: 'A', tool_use_id: 't1', duration_ms: 42, is_error: false });
    // The patch updates the same object (reference identity)
    expect(pending.duration_ms).toBe(42);
    expect(pending.is_error).toBe(false);
    // _pendingTools entry is removed after patching
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

describe('question events are no-ops in reducer', () => {
  it('does not modify state', () => {
    applyEvent(s, { type: 'question_asked', ts: 1, qid: 'q1', asker: 'A', prompt: 'why' });
    applyEvent(s, { type: 'question_answered', ts: 2, qid: 'q1' });
    expect(Object.keys(s.agentsById)).toHaveLength(0);
    expect(s.globalActivity.length).toBe(0);
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
