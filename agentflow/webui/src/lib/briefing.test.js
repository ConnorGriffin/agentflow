import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { deriveAttention, deriveMaps, deriveFleet, deriveCapacity, deriveFreshness,
         deriveBriefing } from './derive.js';

/* A snapshot shaped exactly like what `operator_projection.project()` + `dashboard_data.
   snapshot()` publish (ADR 0036) — the same schema-v2 + v1 fields the real daemon composes,
   not an ad hoc shape. This is the "rendered briefing" test's input; Briefing.test.js renders
   the component from the same fixture. */
const SNAP = {
  generated_at: '2026-07-30T12:00:00+00:00',
  schema_version: 2,
  pools: [
    { tool: 'claude', clear: true, spent_pct: 20, headroom_pct: 80, running: 1, reason: null },
    { tool: 'codex', clear: false, spent_pct: 90, headroom_pct: 0, running: 0, reason: 'weekly pace' },
  ],
  repos: [
    { repo: 'o/r', profile: 'reviewed',
      ready: [{ number: 50, title: 'ready one' }],
      held: [{ number: 10, title: 'needs a call', state: 'needs-grilling',
               reason: 'a real fork the pipeline could not settle', since: '2026-07-29T00:00:00Z' }],
      in_flight: [{ number: 20, title: 'awaiting merge', builder: 'codex' }],
      parked: [],
      recent_merges: [
        { number: 60, title: 'older landing', merged_at: '2026-07-20T12:00:00Z' },
        { number: 61, title: 'newest landing', merged_at: '2026-07-29T12:00:00Z' },
      ],
      ratchet: { samples: 0, correction_rate: 0, ready_to_loosen: false } },
  ],
  repositories: [
    {
      name_with_owner: 'o/r', url: 'https://github.com/o/r', profile: 'reviewed',
      github: { status: 'fresh', attempted_at: '2026-07-30T12:00:00+00:00',
               fresh_at: '2026-07-30T12:00:00+00:00', error: null },
      maps: {
        active_total: 1,
        active: [{
          number: 179, title: 'Map: operator briefing', url: 'https://github.com/o/r/issues/179',
          updated_at: '2026-07-30T00:00:00Z', complete: true,
          progress: { total: 2, closed: 1 },
          frontier: [{ number: 183, title: 'Lock the visual spec', url: 'https://github.com/o/r/issues/183' }],
          tickets: [
            { number: 180, title: 'Bound the projection', url: 'https://github.com/o/r/issues/180', status: 'done' },
            { number: 183, title: 'Lock the visual spec', url: 'https://github.com/o/r/issues/183', status: 'frontier' },
          ],
          handoffs: [{ number: 372, title: 'Ship the typical state', url: 'https://github.com/o/r/issues/372',
                      pipeline: { state: 'pr_open', pr_number: 400, pr_url: 'https://github.com/o/r/pull/400' },
                      attempt_count: 1 }],
          handoffs_overflow: false,
          adrs: [{ label: 'ADR 36', url: 'docs/adr/0036-bounded-repository-map-projection.md' }],
          adrs_overflow: 0,
        }],
      },
    },
  ],
  fleet: { recent_landed: [] },
};

beforeEach(() => vi.useFakeTimers({ now: new Date('2026-07-30T12:00:00Z') }));
afterEach(() => vi.useRealTimers());

describe('deriveFreshness', () => {
  it('reads a fully fresh fleet as fresh, labelled by generated_at', () => {
    const f = deriveFreshness(SNAP);
    expect(f.state).toBe('fresh');
    expect(f.label).toMatch(/Updated/);
  });

  it('reads no repositories at all as incomplete, never fresh', () => {
    expect(deriveFreshness({ repositories: [] }).state).toBe('incomplete');
    expect(deriveFreshness({}).state).toBe('incomplete');
  });

  it('reads a stale repository as stale, an unavailable one as incomplete', () => {
    const stale = { ...SNAP, repositories: [{ ...SNAP.repositories[0],
      github: { status: 'stale' } }] };
    expect(deriveFreshness(stale).state).toBe('stale');
    const unavailable = { ...SNAP, repositories: [{ ...SNAP.repositories[0],
      github: { status: 'unavailable' } }] };
    expect(deriveFreshness(unavailable).state).toBe('incomplete');
  });
});

describe('deriveAttention', () => {
  const items = deriveAttention(SNAP);

  it('ranks the held issue and in-flight merge, each with a real GitHub link', () => {
    expect(items.map((i) => i.kind)).toEqual(['Merge', 'Held']);
    expect(items[0].url).toBe('https://github.com/o/r/pull/20');
    expect(items[1].url).toBe('https://github.com/o/r/issues/10');
  });

  it('never invents an attention item beyond what v1 already computed', () => {
    expect(deriveAttention({ repos: [] })).toEqual([]);
  });
});

describe('deriveMaps', () => {
  const maps = deriveMaps(SNAP);

  it('flattens the one active map with its frontier, tickets, and support links', () => {
    expect(maps).toHaveLength(1);
    expect(maps[0].repository).toBe('r');
    expect(maps[0].progress).toBe('1 of 2 decided');
    expect(maps[0].frontier).toBe('#183 Lock the visual spec');
    expect(maps[0].tickets).toEqual([
      '#180 Bound the projection — done',
      '#183 Lock the visual spec — frontier',
    ]);
    expect(maps[0].support).toContainEqual(
      { url: 'docs/adr/0036-bounded-repository-map-projection.md', label: 'ADR 36' });
    expect(maps[0].support.some((s) => s.label.includes('PR #400 open'))).toBe(true);
  });

  it('never claims a verified frontier when the repository read is not fresh', () => {
    const stale = { ...SNAP, repositories: [{ ...SNAP.repositories[0],
      github: { status: 'stale' } }] };
    expect(deriveMaps(stale)[0].frontier).toBe('Not verified');
  });
});

describe('deriveFleet / deriveCapacity', () => {
  it('flags a repository with held work as needing you, and its landing freshness', () => {
    expect(deriveFleet(SNAP)).toEqual([
      { name: 'r', profile: 'reviewed', work: '1 in flight · 1 ready',
        landings: '2 recent · latest 1d ago', health: '1 needs you', healthy: false },
    ]);
  });

  it('reads no landings as an honest empty label, not a zero count', () => {
    const noLandings = { ...SNAP, repos: [{ ...SNAP.repos[0], recent_merges: [] }] };
    expect(deriveFleet(noLandings)[0].landings).toBe('no landings yet');
  });

  it('renders visibly different landing cells for two repositories at the same count', () => {
    /* The count-alone regression case: both repositories sit at the ten-per-repository
       cap, one landed yesterday and one thirteen days ago. */
    const tenLandings = (newestDay) => Array.from({ length: 10 }, (_, i) => ({
      number: i + 1, title: `landing ${i + 1}`,
      merged_at: `2026-07-${newestDay - i}T12:00:00Z`,
    }));
    const equalCounts = { ...SNAP, repos: [
      { ...SNAP.repos[0], repo: 'o/quiet', recent_merges: tenLandings(17) },
      { ...SNAP.repos[0], repo: 'o/busy', recent_merges: tenLandings(29) },
    ] };
    const [busy, quiet] = deriveFleet(equalCounts);
    expect(busy.landings).toBe('10 recent · latest 1d ago');
    expect(quiet.landings).toBe('10 recent · latest 13d ago');
    expect(busy.landings).not.toBe(quiet.landings);
  });

  it('labels an unverified or stale map read alongside the attention count', () => {
    const stale = { ...SNAP, repositories: [{ ...SNAP.repositories[0],
      github: { status: 'stale' } }] };
    expect(deriveFleet(stale)[0].health).toBe('1 needs you · map data stale');
    const unavailable = { ...SNAP, repositories: [{ ...SNAP.repositories[0],
      github: { status: 'unavailable' } }] };
    expect(deriveFleet(unavailable)[0].health).toBe('1 needs you · map data unverified');
  });

  it('reads a fully healthy repository as healthy, no join penalty when fresh', () => {
    const healthy = { ...SNAP, repos: [{ ...SNAP.repos[0], held: [], parked: [] }] };
    expect(deriveFleet(healthy)[0].health).toBe('healthy');
    expect(deriveFleet(healthy)[0].healthy).toBe(true);
  });

  it('orders repositories needing you first, then alphabetically case-insensitively, ' +
     'owner/name as tiebreak — grouping never keys off unobserved map freshness', () => {
    const repoRow = (repo, over) => ({
      repo, profile: 'reviewed', ready: [], held: [], parked: [], in_flight: [], recent_merges: [],
      ratchet: { samples: 0, correction_rate: 0, ready_to_loosen: false }, ...over,
    });
    const mixed = {
      repos: [
        repoRow('o/dotfiles'),
        repoRow('o/Brewgen', { held: [{ number: 1, title: 't', state: 'needs-grilling',
                                        reason: 'r', since: null }] }),
        repoRow('o/agentflow', { held: [{ number: 2, title: 't', state: 'needs-grilling',
                                          reason: 'r', since: null }] }),
        repoRow('o/homelab'),
      ],
      repositories: [
        { name_with_owner: 'o/dotfiles', github: { status: 'unavailable' } },
        { name_with_owner: 'o/Brewgen', github: { status: 'unavailable' } },
        { name_with_owner: 'o/agentflow', github: { status: 'unavailable' } },
        { name_with_owner: 'o/homelab', github: { status: 'unavailable' } },
      ],
    };
    expect(deriveFleet(mixed).map((r) => r.name)).toEqual(['agentflow', 'Brewgen', 'dotfiles', 'homelab']);
  });
});

describe('deriveCapacity', () => {
  it('reads a taking-work pool as availability first, then running, then utilization', () => {
    expect(deriveCapacity(SNAP)).toEqual([
      { name: 'claude', detail: 'taking work, 1 running, 20% used' },
      { name: 'codex', detail: 'paused, nothing running · weekly pace' },
    ]);
  });

  it('never shows the published utilization as "% used" nor a headroom figure for a paused pool', () => {
    const pools = [
      { tool: 'claude', clear: false, spent_pct: 0.0, headroom_pct: 0.0, running: 0,
        reason: 'weekly spend at 98% exceeds 80.0% released for unattended work' },
      { tool: 'codex', clear: false, spent_pct: 40.0, headroom_pct: 0.0, running: 1,
        reason: 'weekly spend at 40% exceeds 22.9% released' },
    ];
    const [claude, codex] = deriveCapacity({ pools });
    expect(claude.detail).toBe('paused, nothing running · weekly spend at 98% exceeds 80.0% released for unattended work');
    expect(claude.detail).not.toMatch(/0(\.0)?% used/);
    expect(claude.detail).not.toMatch(/headroom/);
    expect(codex.detail).toBe('paused, 1 running · weekly spend at 40% exceeds 22.9% released');
    expect(codex.detail).not.toMatch(/40(\.0)?% used/);
  });

  it('never mentions a slot count, in any state', () => {
    const pools = [
      { tool: 'claude', clear: true, spent_pct: 38, headroom_pct: 62, running: 3, reason: null },
      { tool: 'codex', clear: false, spent_pct: 40, headroom_pct: 0, running: 0, reason: 'weekly pace' },
    ];
    for (const p of deriveCapacity({ pools })) expect(p.detail).not.toMatch(/slot/i);
  });
});

describe('deriveBriefing — the whole shape', () => {
  it('composes freshness/attention/maps/fleet/capacity together', () => {
    const b = deriveBriefing(SNAP);
    expect(Object.keys(b).sort()).toEqual(
      ['attention', 'capacity', 'fleet', 'freshness', 'maps'].sort());
    expect(b.maps).toHaveLength(1);
    expect(b.attention).toHaveLength(2);
  });
});
