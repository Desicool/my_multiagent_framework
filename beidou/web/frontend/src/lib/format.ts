/** Compact number: 1234 → "1.2k", 1500000 → "1.5M". */
export function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return '0';
  const abs = Math.abs(n);
  if (abs < 1000) return String(Math.round(n));
  if (abs < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  if (abs < 1_000_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  return `${(n / 1_000_000_000).toFixed(2)}B`;
}

/** USD formatter: 0 → "$0.00", < 0.01 → "$<0.01", else "$X.XXXX" up to 4 decimals. */
export function fmtMoney(n: number): string {
  if (!Number.isFinite(n) || n === 0) return '$0.00';
  if (n < 0.0001) return '$<0.0001';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

/** Short id: first N chars, default 6. */
export function shortId(id: string, len: number = 6): string {
  if (!id) return '';
  return id.length <= len ? id : id.slice(0, len);
}
