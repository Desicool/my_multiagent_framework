import { describe, expect, it } from 'vitest';
import { buildTaskExport } from './exportTask';
import { createInitialState } from '../reducer/state';
import { applyEvent } from '../reducer/reduce';

describe('buildTaskExport', () => {
  it('shapes a clean export from a populated reducer state', () => {
    const s = createInitialState();
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null, role: 'root' });
    applyEvent(s, { type: 'tool_called', ts: 2, caller_id: 'A', tool_use_id: 't1', name: 'Bash', input: { cmd: 'ls' } });

    const out = buildTaskExport(s);
    expect(out.schema).toBe('beidou-task-export/v1');
    expect(out.exported_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(out.agents).toHaveLength(1);
    // Should not leak reactive scaffolding fields.
    const a = out.agents[0] as Record<string, unknown>;
    expect(a._stream).toBeUndefined();
    expect(a._pendingTools).toBeUndefined();
    expect(a._seenTurn).toBeUndefined();
    expect(a._seenTool).toBeUndefined();
    expect(a.role).toBe('root');
  });

  it('serialises to valid JSON', () => {
    const s = createInitialState();
    applyEvent(s, { type: 'agent_started', ts: 1, agent_id: 'A', task_id: 'T', team_id: null, role: 'root' });
    const out = buildTaskExport(s);
    const json = JSON.stringify(out);
    expect(() => JSON.parse(json)).not.toThrow();
  });
});
