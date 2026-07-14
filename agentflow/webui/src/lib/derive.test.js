import { describe, it, expect } from 'vitest';
import { deriveInbox } from './derive.js';

/* A snapshot with one PR in every profile, plus a ratchet ready to loosen. The
   ordering + exclusion rules are the Inbox's public contract; this fails if they
   regress. */
const snap = {
  repos: [
    { repo: 'o/auto', profile: 'autonomous',
      in_flight: [{ number: 1, title: 'auto pr', builder: 'claude' }],
      ratchet: { samples: 5, correction_rate: 0.0, ready_to_loosen: false } },
    { repo: 'o/rev', profile: 'reviewed',
      in_flight: [{ number: 2, title: 'reviewed pr', builder: 'codex' }],
      ratchet: { samples: 3, correction_rate: 0.1, ready_to_loosen: false } },
    { repo: 'o/guard', profile: 'guarded',
      in_flight: [{ number: 3, title: 'guarded pr', builder: 'claude' }],
      ratchet: { samples: 20, correction_rate: 0.02, ready_to_loosen: true } },
  ],
};

describe('deriveInbox — today’s contract', () => {
  const items = deriveInbox(snap);

  it('ranks guarded merge first, reviewed merge second, ratchet-ready third', () => {
    expect(items.map((it) => it.kind + ':' + (it.number ?? it.repo))).toEqual([
      'merge:3', // guarded, weight 1
      'merge:2', // reviewed, weight 2
      'loosen:o/guard', // ratchet ready, weight 3
    ]);
  });

  it('never surfaces an autonomous in-flight PR', () => {
    expect(items.some((it) => it.repo === 'o/auto' && it.kind === 'merge')).toBe(false);
  });

  it('derives an empty inbox when nothing needs a human', () => {
    const quiet = {
      repos: [
        { repo: 'o/auto', profile: 'autonomous',
          in_flight: [{ number: 9, title: 'auto', builder: 'codex' }],
          ratchet: { samples: 4, correction_rate: 0.0, ready_to_loosen: false } },
      ],
    };
    expect(deriveInbox(quiet)).toEqual([]);
  });
});
