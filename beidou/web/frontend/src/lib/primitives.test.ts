import { describe, expect, it } from 'vitest';
import {
  agentMonogramBucket,
  agentMonogramText,
  allPrimitives,
  bareToolName,
  categoryAccentClasses,
  getPrimitive,
  isPrimitive,
} from './primitives';

describe('bareToolName', () => {
  it('strips mcp prefix', () => {
    expect(bareToolName('mcp__beidou__send_message')).toBe('send_message');
    expect(bareToolName('mcp__beidou__signal_review')).toBe('signal_review');
  });
  it('returns name unchanged when no prefix', () => {
    expect(bareToolName('Bash')).toBe('Bash');
    expect(bareToolName('send_message')).toBe('send_message');
  });
});

describe('getPrimitive / isPrimitive', () => {
  it('recognises all 13 primitives by bare name and prefixed name', () => {
    const names = [
      'send_message', 'list_peers',
      'spawn_agent', 'terminate_child', 'create_team',
      'declare_plan', 'remove_plan', 'list_ready',
      'signal_review', 'request_termination', 'report_status', 'list_pending_reviews',
      'ask_user',
    ];
    for (const n of names) {
      expect(isPrimitive(n)).toBe(true);
      expect(isPrimitive(`mcp__beidou__${n}`)).toBe(true);
      expect(getPrimitive(n)?.name).toBe(n);
    }
  });

  it('returns null for non-primitive tools', () => {
    expect(isPrimitive('Bash')).toBe(false);
    expect(getPrimitive('Read')).toBeNull();
  });

  it('marks legacy primitives deprecated', () => {
    expect(getPrimitive('report_status')?.deprecated).toBe(true);
    expect(getPrimitive('create_team')?.deprecated).toBe(true);
    expect(getPrimitive('signal_review')?.deprecated).toBeFalsy();
  });

  it('exposes the full list', () => {
    expect(allPrimitives()).toHaveLength(13);
  });
});

describe('agentMonogramBucket', () => {
  it('is deterministic — same id → same bucket', () => {
    expect(agentMonogramBucket('ag_14378f95')).toBe(agentMonogramBucket('ag_14378f95'));
    expect(agentMonogramBucket('ag_9b0c451d')).toBe(agentMonogramBucket('ag_9b0c451d'));
  });

  it('returns one of h1..h6', () => {
    const buckets = new Set<string>();
    for (const id of ['ag_a', 'ag_b', 'ag_c', 'ag_d', 'ag_e', 'ag_f', 'ag_g', 'ag_h']) {
      buckets.add(agentMonogramBucket(id));
    }
    for (const b of buckets) {
      expect(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']).toContain(b);
    }
  });

  it('falls back to h1 for empty id', () => {
    expect(agentMonogramBucket('')).toBe('h1');
  });
});

describe('agentMonogramText', () => {
  it('strips prefix and returns first two chars after underscore', () => {
    expect(agentMonogramText('ag_14378f95')).toBe('14');
    expect(agentMonogramText('ag_9b0c451d')).toBe('9b');
    expect(agentMonogramText('tm_8756c268')).toBe('87');
  });

  it('handles unprefixed ids', () => {
    expect(agentMonogramText('abcd')).toBe('ab');
  });

  it('falls back to ?? for empty', () => {
    expect(agentMonogramText('')).toBe('??');
  });
});

describe('categoryAccentClasses', () => {
  it('returns distinct classes per category', () => {
    const cats = ['coordination', 'lifecycle', 'plan', 'review', 'human'] as const;
    const seen = new Set<string>();
    for (const c of cats) {
      const v = categoryAccentClasses(c);
      seen.add(v.textClass);
    }
    expect(seen.size).toBe(cats.length - 1); // 'lifecycle' & 'human' share text-accent
  });
});
