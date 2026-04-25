/**
 * Vitest tests for ws.ts — WS URL construction and message dispatch.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { StreamCallbacks } from './ws';

// ---------------------------------------------------------------------------
// Minimal WebSocket mock
// ---------------------------------------------------------------------------

type MockWsInstance = {
  url: string;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  close: () => void;
  /** Test helper: push a raw string as a message event. */
  push: (data: string) => void;
  /** Test helper: trigger open. */
  open: () => void;
};

let lastInstance: MockWsInstance | null = null;

class MockWebSocket {
  url: string;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    lastInstance = this as unknown as MockWsInstance;
  }

  close() {
    this.onclose?.({} as CloseEvent);
  }

  /** Test helper: simulate server sending a message. */
  push(data: string) {
    this.onmessage?.({ data } as MessageEvent);
  }

  /** Test helper: simulate connection established. */
  open() {
    this.onopen?.({} as Event);
  }
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  lastInstance = null;
  // Install mock globally; location is already available in jsdom via vitest.
  vi.stubGlobal('WebSocket', MockWebSocket);
  // Vitest/jsdom default host includes a port (e.g. localhost:3000).
  // Stub location so tests produce deterministic URLs.
  Object.defineProperty(globalThis, 'location', {
    value: { protocol: 'http:', host: 'localhost' },
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Helper: import dynamically so vi.stubGlobal takes effect
// ---------------------------------------------------------------------------
async function importWs() {
  // Force fresh import each test via cache-busting is not possible with static
  // ESM; instead we import once and rely on the global stub being in place
  // before openTaskStream calls `new WebSocket(...)`.
  const mod = await import('./ws');
  return mod;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('openTaskStream — WS URL construction', () => {
  it('uses /ws/tasks/abc with no query when initialCursor is 0', async () => {
    const { openTaskStream } = await importWs();
    const handle = openTaskStream('abc', 0, {
      onEvent: () => {},
      onResumed: () => {},
      onStatusChange: () => {},
      onCursorAdvance: () => {},
    });

    expect(lastInstance).not.toBeNull();
    // ws:// because location.protocol === 'http:'
    expect(lastInstance!.url).toBe('ws://localhost/ws/tasks/abc');

    handle.close();
  });

  it('appends ?since=42.5 when initialCursor is 42.5', async () => {
    const { openTaskStream } = await importWs();
    const handle = openTaskStream('abc', 42.5, {
      onEvent: () => {},
      onResumed: () => {},
      onStatusChange: () => {},
      onCursorAdvance: () => {},
    });

    expect(lastInstance).not.toBeNull();
    expect(lastInstance!.url).toBe('ws://localhost/ws/tasks/abc?since=42.5');

    handle.close();
  });
});

describe('openTaskStream — message dispatch', () => {
  it('calls onEvent when a {type:"event"} envelope arrives', async () => {
    const { openTaskStream } = await importWs();

    const receivedEvents: unknown[] = [];
    const receivedCursors: number[] = [];

    const cb: StreamCallbacks = {
      onEvent: (ev) => receivedEvents.push(ev),
      onResumed: () => {},
      onStatusChange: () => {},
      onCursorAdvance: (c) => receivedCursors.push(c),
    };

    const handle = openTaskStream('abc', 0, cb);
    expect(lastInstance).not.toBeNull();

    // Simulate connection open (live-only, cursor=0).
    lastInstance!.open();

    // Push an event envelope.
    const fakeEvent = { type: 'agent_started', ts: 1.0, agent_id: 'ag1', task_id: 't1', team_id: null };
    lastInstance!.push(JSON.stringify({ type: 'event', event: fakeEvent, cursor: 1.0 }));

    expect(receivedEvents).toHaveLength(1);
    expect(receivedEvents[0]).toEqual(fakeEvent);
    expect(receivedCursors).toContain(1.0);

    handle.close();
  });

  it('calls onResumed and onStatusChange("live") when {type:"resumed"} arrives', async () => {
    const { openTaskStream } = await importWs();

    const statusChanges: string[] = [];
    const resumedCursors: number[] = [];

    const cb: StreamCallbacks = {
      onEvent: () => {},
      onResumed: (c) => resumedCursors.push(c),
      onStatusChange: (s) => statusChanges.push(s),
      onCursorAdvance: () => {},
    };

    const handle = openTaskStream('abc', 10.0, cb);
    lastInstance!.open();

    // Should be 'replaying' after open with cursor > 0.
    expect(statusChanges).toContain('replaying');

    // Push resumed envelope.
    lastInstance!.push(JSON.stringify({ type: 'resumed', cursor: 10.0 }));

    expect(resumedCursors).toContain(10.0);
    expect(statusChanges[statusChanges.length - 1]).toBe('live');

    handle.close();
  });

  it('goes directly to "live" status on open when initialCursor is 0', async () => {
    const { openTaskStream } = await importWs();

    const statusChanges: string[] = [];

    const handle = openTaskStream('abc', 0, {
      onEvent: () => {},
      onResumed: () => {},
      onStatusChange: (s) => statusChanges.push(s),
      onCursorAdvance: () => {},
    });

    lastInstance!.open();

    expect(statusChanges).toContain('live');
    expect(statusChanges).not.toContain('replaying');

    handle.close();
  });

  it('close() prevents further onEvent calls', async () => {
    const { openTaskStream } = await importWs();

    const receivedEvents: unknown[] = [];
    const handle = openTaskStream('abc', 0, {
      onEvent: (ev) => receivedEvents.push(ev),
      onResumed: () => {},
      onStatusChange: () => {},
      onCursorAdvance: () => {},
    });

    lastInstance!.open();
    handle.close();

    // Push after close — should be ignored.
    lastInstance!.push(JSON.stringify({ type: 'event', event: { type: 'agent_started', ts: 2.0, agent_id: 'a', task_id: 't', team_id: null }, cursor: 2.0 }));

    expect(receivedEvents).toHaveLength(0);
  });
});
