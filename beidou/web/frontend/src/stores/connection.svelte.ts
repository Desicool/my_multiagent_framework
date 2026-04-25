import type { ConnectionState } from '../lib/types';

const conn = $state<ConnectionState>({ status: 'connecting', cursor: 0, attempts: 0, retryInMs: null });

export function setStatus(s: ConnectionState['status']): void { conn.status = s; }
export function setCursor(c: number): void { conn.cursor = c; }
export function setAttempts(n: number): void { conn.attempts = n; }
export function setRetryIn(ms: number | null): void { conn.retryInMs = ms; }

export { conn };
