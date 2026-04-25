import { describe, it, expect } from 'vitest';
import { fmtNum, fmtMoney, shortId } from './format';
import { ago, elapsed, hhmmss } from './time';

describe('fmtNum', () => {
  it('small numbers', () => { expect(fmtNum(0)).toBe('0'); expect(fmtNum(42)).toBe('42'); });
  it('thousands', () => { expect(fmtNum(1234)).toBe('1.2k'); });
  it('millions', () => { expect(fmtNum(1_500_000)).toBe('1.50M'); });
});

describe('fmtMoney', () => {
  it('zero', () => { expect(fmtMoney(0)).toBe('$0.00'); });
  it('tiny', () => { expect(fmtMoney(0.00005)).toBe('$<0.0001'); });
  it('cents', () => { expect(fmtMoney(0.0042)).toBe('$0.0042'); });
  it('dollars', () => { expect(fmtMoney(3.42)).toBe('$3.42'); });
});

describe('shortId', () => {
  it('truncates', () => { expect(shortId('abcdef1234')).toBe('abcdef'); });
  it('preserves short', () => { expect(shortId('abc')).toBe('abc'); });
});

describe('ago', () => {
  it('seconds', () => { expect(ago(0, 30)).toBe('30s ago'); });
  it('minutes', () => { expect(ago(0, 120)).toBe('2m ago'); });
});

describe('elapsed', () => {
  it('ms', () => { expect(elapsed(300)).toBe('300ms'); });
  it('seconds', () => { expect(elapsed(2500)).toBe('2.5s'); });
});

describe('hhmmss', () => {
  it('returns HH:MM:SS', () => {
    expect(hhmmss(3600 * 5 + 60 * 7 + 9)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});
