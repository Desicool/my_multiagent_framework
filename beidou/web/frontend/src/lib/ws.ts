/**
 * WebSocket client with replay/resume semantics, exponential backoff, and
 * REST polling fallback.
 *
 * Protocol (from beidou/web/app.py):
 *   {type:'event',   event: BeidouEvent, cursor: number}
 *   {type:'resumed', cursor: number}
 *
 * Cursor edge cases (plan §4):
 * - WS replay uses strict ts > since, so an idle replay never emits 'resumed'.
 * - The idle-timeout (3 s) flips status to 'live' when no envelope arrives after
 *   replaying has begun — silence IS the catch-up signal.
 * - live-only mode (initialCursor === 0): backend never sends 'resumed'; set
 *   'live' directly on open, never enter 'replaying'.
 */
import type { BeidouEvent, ConnectionStatus } from './types';
import { fetchEvents } from './api';

export type StreamCallbacks = {
  onEvent: (ev: BeidouEvent) => void;
  onResumed: (cursor: number) => void;
  onStatusChange: (status: ConnectionStatus) => void;
  onCursorAdvance: (cursor: number) => void;
};

export type StreamHandle = { close: () => void };

// Backoff ladder: 500 ms → 1 s → 2 s → 4 s → 8 s, capped at 10 s.
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
const BACKOFF_CAP_MS = 10_000;
const IDLE_LIVE_TIMEOUT_MS = 3_000;
const POLL_INTERVAL_MS = 1_500;
const POLL_RECONNECT_INTERVAL_MS = 30_000;
const POLLING_AFTER_ATTEMPTS = 3;

function buildWsUrl(taskId: string, cursor: number): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const base = `${proto}//${location.host}/ws/tasks/${encodeURIComponent(taskId)}`;
  return cursor > 0 ? `${base}?since=${cursor}` : base;
}

export function openTaskStream(
  taskId: string,
  initialCursor: number,
  cb: StreamCallbacks,
): StreamHandle {
  let closed = false;
  let currentCursor = initialCursor;
  let attempts = 0;
  let ws: WebSocket | null = null;

  // Timers
  let idleTimer: ReturnType<typeof setTimeout> | null = null;
  let pollInterval: ReturnType<typeof setInterval> | null = null;
  let pollReconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  function clearIdleTimer() {
    if (idleTimer !== null) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
  }

  function resetIdleTimer() {
    clearIdleTimer();
    idleTimer = setTimeout(() => {
      // 3 s of silence after replaying started → assume caught up, go live.
      cb.onStatusChange('live');
    }, IDLE_LIVE_TIMEOUT_MS);
  }

  function stopPolling() {
    if (pollInterval !== null) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
    if (pollReconnectTimer !== null) {
      clearTimeout(pollReconnectTimer);
      pollReconnectTimer = null;
    }
  }

  function startPolling() {
    if (pollInterval !== null) return; // already polling
    cb.onStatusChange('polling');

    pollInterval = setInterval(async () => {
      try {
        const { events, cursor } = await fetchEvents(taskId, currentCursor, 200);
        for (const ev of events) {
          cb.onEvent(ev);
        }
        if (cursor > currentCursor) {
          currentCursor = cursor;
          cb.onCursorAdvance(cursor);
        }
      } catch {
        // transient failure — keep polling
      }
    }, POLL_INTERVAL_MS);

    // Background WS reconnect attempts every 30 s while polling.
    function scheduleWsRetry() {
      if (closed) return;
      pollReconnectTimer = setTimeout(() => {
        if (closed) return;
        connect(/* fromPolling */ true, scheduleWsRetry);
      }, POLL_RECONNECT_INTERVAL_MS);
    }
    scheduleWsRetry();
  }

  /**
   * @param fromPolling  When true, a successful open stops polling and resets
   *                     the attempt counter.
   * @param onFailCb     Called when the connection fails so polling can schedule
   *                     the next background retry.
   */
  function connect(fromPolling = false, onFailCb?: () => void) {
    if (closed) return;

    cb.onStatusChange('connecting');

    const url = buildWsUrl(taskId, currentCursor);
    const socket = new WebSocket(url);
    ws = socket;

    socket.onopen = () => {
      if (closed) { socket.close(); return; }

      // Successful open: reset attempt counter.
      attempts = 0;

      if (fromPolling) {
        // Resumed from WS after polling — stop the poll loop.
        stopPolling();
      }

      if (currentCursor > 0) {
        // Replay mode: wait for 'resumed' envelope OR idle timeout.
        cb.onStatusChange('replaying');
        resetIdleTimer();
      } else {
        // Live-only: backend never sends 'resumed' for this branch.
        cb.onStatusChange('live');
      }
    };

    socket.onmessage = (ev: MessageEvent) => {
      if (closed) return;
      let envelope: { type: string; event?: BeidouEvent; cursor?: number };
      try {
        envelope = JSON.parse(ev.data as string) as typeof envelope;
      } catch {
        return;
      }

      if (envelope.type === 'event' && envelope.event !== undefined) {
        const cursor = envelope.cursor ?? (envelope.event.ts ?? 0);
        cb.onEvent(envelope.event);
        if (cursor > currentCursor) {
          currentCursor = cursor;
          cb.onCursorAdvance(cursor);
        }
        // Any envelope resets the idle timer (we're still receiving).
        if (currentCursor > 0) resetIdleTimer();
      } else if (envelope.type === 'resumed') {
        const cursor = envelope.cursor ?? currentCursor;
        if (cursor > currentCursor) {
          currentCursor = cursor;
          cb.onCursorAdvance(cursor);
        }
        cb.onResumed(cursor);
        cb.onStatusChange('live');
        clearIdleTimer();
      }
    };

    socket.onclose = () => {
      if (closed) return;
      ws = null;
      clearIdleTimer();

      if (fromPolling) {
        // This was a background WS retry from polling — just reschedule.
        onFailCb?.();
        return;
      }

      attempts += 1;
      if (attempts >= POLLING_AFTER_ATTEMPTS && pollInterval === null) {
        startPolling();
        // Polling handles its own background WS reconnect — don't schedule below.
        return;
      }

      // Normal exponential backoff reconnect.
      const delay = Math.min(BACKOFF_MS[attempts - 1] ?? BACKOFF_CAP_MS, BACKOFF_CAP_MS);
      reconnectTimer = setTimeout(() => {
        if (!closed) connect();
      }, delay);
    };

    socket.onerror = () => {
      // onerror always fires before onclose; let onclose handle the logic.
      // We just ensure ws is nulled so it can be GC'd.
      socket.close();
    };
  }

  // Start first connection.
  connect();

  return {
    close() {
      if (closed) return;
      closed = true;
      clearIdleTimer();
      stopPolling();
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      if (ws !== null) {
        ws.onclose = null; // prevent reconnect logic on intentional close
        ws.close();
        ws = null;
      }
    },
  };
}
