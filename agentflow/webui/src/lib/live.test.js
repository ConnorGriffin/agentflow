import { describe, it, expect } from 'vitest';
import { elapsed, STAGES } from './derive.js';

/* The Live board's ticking timer and its three lanes are the tab's public contract. */
describe('elapsed — a live session’s ticking timer', () => {
  const now = Date.parse('2026-07-13T12:00:00Z');
  const ago = (s) => new Date(now - s * 1000).toISOString();

  it('shows seconds under a minute, then minutes, then hours', () => {
    expect(elapsed(ago(12), now)).toBe('12s');
    expect(elapsed(ago(5 * 60), now)).toBe('5m');
    expect(elapsed(ago(63 * 60), now)).toBe('1h 3m');
    expect(elapsed(ago(120 * 60), now)).toBe('2h');
  });

  it('reads a missing start time as “—”, never NaN', () => {
    expect(elapsed(null, now)).toBe('—');
  });
});

describe('lane derivation — triaging → building → reviewing', () => {
  const runs = [
    { stage: 'reviewing', started_at: '2026-07-13T11:59:00Z' },
    { stage: 'building', started_at: '2026-07-13T11:50:00Z' },
    { stage: 'building', started_at: '2026-07-13T11:40:00Z' },
  ];
  const counts = { triaging: 0, building: 0, reviewing: 0 };
  for (const s of runs) counts[s.stage]++;

  it('counts per stage, reading zero when a lane is empty', () => {
    expect(counts).toEqual({ triaging: 0, building: 2, reviewing: 1 });
  });

  it('orders lanes triaging → building → reviewing', () => {
    expect(STAGES.map((s) => s.key)).toEqual(['triaging', 'building', 'reviewing']);
  });
});
