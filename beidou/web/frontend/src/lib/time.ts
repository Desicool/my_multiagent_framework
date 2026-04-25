/** Relative time, e.g. "12s ago", "3m ago", "2h ago", "5d ago". */
export function ago(ts: number, now: number = Date.now() / 1000): string {
  const sec = Math.max(0, Math.floor(now - ts));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const days = Math.floor(hr / 24);
  return `${days}d ago`;
}

/** Local time as HH:MM:SS, zero-padded. */
export function hhmmss(ts: number): string {
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Elapsed milliseconds → human "1.2s", "45ms", "3m 20s", "1h 5m". */
export function elapsed(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const min = Math.floor(s / 60);
  const rem = Math.floor(s % 60);
  if (min < 60) return `${min}m ${rem}s`;
  const hr = Math.floor(min / 60);
  const mr = min % 60;
  return `${hr}h ${mr}m`;
}
