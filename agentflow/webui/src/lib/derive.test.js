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

/* Held issues + parked PRs join the inbox (issue #71). Ordering follows the locked spec:
   guarded merge (1) · reviewed merge (2) · held needs-grilling (3) · held needs-mockup (4)
   · parked (5) · loosen (6); oldest-first within a weight. */
const held = {
  repos: [
    {
      repo: 'o/app',
      profile: 'reviewed',
      in_flight: [{ number: 2, title: 'reviewed pr', builder: 'codex' }],
      held: [
        { number: 40, title: 'mockup me', state: 'needs-mockup', reason: 'ui', since: '2026-07-13T10:00:00Z' },
        { number: 41, title: 'grill me', state: 'needs-grilling', reason: 'fork', since: '2026-07-13T10:00:00Z' },
      ],
      parked: [
        { number: 50, title: 'newer park', reason: 'failed-merge', builder: 'claude', since: '2026-07-13T12:00:00Z' },
        { number: 51, title: 'older park', reason: 'ui-evidence', builder: 'codex', since: '2026-07-13T09:00:00Z' },
      ],
      ratchet: { samples: 20, correction_rate: 0.02, ready_to_loosen: true },
    },
  ],
};

describe('deriveInbox — held + parked (issue #71)', () => {
  const items = deriveInbox(held);

  it('ranks merge > held(grilling) > held(mockup) > parked > loosen, oldest parked first', () => {
    expect(items.map((it) => it.kind + ':' + (it.number ?? it.repo))).toEqual([
      'merge:2', // reviewed, weight 2
      'held:41', // needs-grilling, weight 3
      'held:40', // needs-mockup, weight 4
      'parked:51', // parked, weight 5 — older since ranks ahead of #50
      'parked:50',
      'loosen:o/app', // weight 6
    ]);
  });

  it('carries the fields each row needs', () => {
    const parked = items.find((it) => it.number === 51);
    expect(parked.reason).toBe('ui-evidence');
    expect(parked.reviewer).toBe('claude'); // codex builder → claude reviews
    const grill = items.find((it) => it.number === 41);
    expect(grill.state).toBe('needs-grilling');
    expect(grill.reason).toBe('fork');
  });
});
