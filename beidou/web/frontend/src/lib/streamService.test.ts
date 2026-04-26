/**
 * Tests for the backfill logic in streamService.ts.
 *
 * Mocking strategy:
 *  - ./api  → getTaskSnapshot, fetchEvents
 *  - ./ws   → openTaskStream (returns a no-op handle)
 *  - ../stores/events.svelte → applyEvent (vi.fn for call-counting),
 *      plus bumpCursor, resetEvents, events stubs
 *  - ../stores/connection.svelte → setStatus, setCursor, setAttempts
 *  - ../stores/questions.svelte → triggerRepoll
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Module mocks — must be hoisted (vi.mock calls are moved to the top by vitest)
// ---------------------------------------------------------------------------

vi.mock('./api', () => ({
  getTaskSnapshot: vi.fn(),
  fetchEvents: vi.fn(),
}));

vi.mock('./ws', () => ({
  openTaskStream: vi.fn(() => ({ close: vi.fn() })),
}));

// We mock the entire events.svelte store so applyEvent is countable and the
// module doesn't try to use Svelte $state at import time (which would fail
// outside a Svelte component context).
vi.mock('../stores/events.svelte', () => {
  const applyEvent = vi.fn();
  const bumpCursor = vi.fn();
  const resetEvents = vi.fn();
  const events: Record<string, unknown> = { agentsById: {}, teamsById: {}, stats: { total_cost_usd: 0, total_tokens: 0 } };
  return { applyEvent, bumpCursor, resetEvents, events };
});

vi.mock('../stores/connection.svelte', () => ({
  setStatus: vi.fn(),
  setCursor: vi.fn(),
  setAttempts: vi.fn(),
  setRetryIn: vi.fn(),
}));

vi.mock('../stores/questions.svelte', () => ({
  triggerRepoll: vi.fn(),
}));

// ---------------------------------------------------------------------------
// Import the mocked modules so we can configure per-test
// ---------------------------------------------------------------------------
import { getTaskSnapshot, fetchEvents } from './api';
import { openTaskStream } from './ws';
import { applyEvent } from '../stores/events.svelte';
import { setCursor, setStatus } from '../stores/connection.svelte';

// openTask is imported AFTER vi.mock so it gets the mocked dependencies.
import { openTask } from './streamService';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A minimal TaskSnapshot with no teams / agents and a given cursor. */
function makeSnap(cursor: number) {
  return {
    task: { task_id: 'task-1', description: 'test', model: 'claude', skill: null, started_at: 0, ended_at: null, total_cost_usd: 0 },
    teams: [],
    agents: [],
    stats: {},
    cursor,
  };
}

/** Build N fake events with incrementing ts values starting from `startTs`. */
function makeEvents(count: number, startTs = 1) {
  return Array.from({ length: count }, (_, i) => ({
    type: 'assistant_text' as const,
    ts: startTs + i,
    caller_id: 'ag1',
    message_id: `m${startTs + i}`,
    text: `chunk ${startTs + i}`,
  }));
}

beforeEach(() => {
  vi.clearAllMocks();
  // Default: snapshot succeeds with cursor=100; fetchEvents returns nothing.
  (getTaskSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue(makeSnap(100));
  (fetchEvents as ReturnType<typeof vi.fn>).mockResolvedValue({ events: [], cursor: 0 });
  (openTaskStream as ReturnType<typeof vi.fn>).mockReturnValue({ close: vi.fn() });
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('streamService backfill', () => {
  it('test_backfill_paginates_until_empty: applies 800 events across two pages', async () => {
    // Page 1: 500 events, cursor advances to 200.
    // Page 2: 300 events, cursor advances to 350.
    // Page 3: 0 events → stops.
    (fetchEvents as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ events: makeEvents(500, 1),   cursor: 200 })
      .mockResolvedValueOnce({ events: makeEvents(300, 201), cursor: 350 })
      .mockResolvedValueOnce({ events: [],                   cursor: 350 });

    await openTask('task-1');

    // snapshot has no teams/agents → applyEvent called ONLY by backfill.
    expect(applyEvent).toHaveBeenCalledTimes(800);

    // Final cursor passed to openTaskStream should be max(snap.cursor=100, backfillCursor=350)=350.
    const wsCallArgs = (openTaskStream as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(wsCallArgs[1]).toBe(350);
  });

  it('test_backfill_handles_empty_snapshot: cursor=0, fetchEvents returns events', async () => {
    (getTaskSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue(makeSnap(0));
    (fetchEvents as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ events: makeEvents(10, 1), cursor: 10 })
      .mockResolvedValueOnce({ events: [],               cursor: 10 });

    await openTask('task-2');

    expect(applyEvent).toHaveBeenCalledTimes(10);
    const wsCallArgs = (openTaskStream as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(wsCallArgs[1]).toBe(10);
  });

  it('test_backfill_handles_fetch_error: transient failure does not crash; WS still opens', async () => {
    // First page OK, second throws.
    (fetchEvents as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ events: makeEvents(5, 1), cursor: 5 })
      .mockRejectedValueOnce(new Error('network error'));

    await openTask('task-3');

    // First page events applied.
    expect(applyEvent).toHaveBeenCalledTimes(5);

    // WS was still opened despite the error.
    expect(openTaskStream).toHaveBeenCalledTimes(1);
  });

  it('test_backfill_advances_initialCursor_for_ws: WS opens with post-backfill cursor', async () => {
    // Snapshot cursor = 100, backfill advances to 350.
    (fetchEvents as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ events: makeEvents(10, 101), cursor: 350 })
      .mockResolvedValueOnce({ events: [],                  cursor: 350 });

    await openTask('task-4');

    const wsCallArgs = (openTaskStream as ReturnType<typeof vi.fn>).mock.calls[0];
    // Must use 350, NOT the snapshot cursor of 100.
    expect(wsCallArgs[1]).toBe(350);
  });

  it('test_backfill_skipped_when_no_events: WS opens with snapshot cursor unchanged', async () => {
    // fetchEvents returns empty immediately.
    (fetchEvents as ReturnType<typeof vi.fn>).mockResolvedValue({ events: [], cursor: 0 });

    await openTask('task-5');

    // No backfill events applied (snapshot has no teams/agents either).
    expect(applyEvent).not.toHaveBeenCalled();

    // WS still opens with the snapshot cursor (100).
    expect(openTaskStream).toHaveBeenCalledTimes(1);
    const wsCallArgs = (openTaskStream as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(wsCallArgs[1]).toBe(100);
  });

  it('sets replaying status before backfill and passes correct taskId to WS', async () => {
    await openTask('task-6');

    // setStatus should have been called with 'replaying' during backfill phase.
    const statusCalls = (setStatus as ReturnType<typeof vi.fn>).mock.calls.map(c => c[0]);
    expect(statusCalls).toContain('replaying');

    // WS opened for the right task.
    const wsCallArgs = (openTaskStream as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(wsCallArgs[0]).toBe('task-6');
  });
});
