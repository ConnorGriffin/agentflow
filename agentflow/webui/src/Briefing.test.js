import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import Briefing from './Briefing.svelte';
import STATES from './fixtures/operator-briefing-states.json';

/* Daemon projection → endpoint → rendered briefing, exercised through the real component
   (ADR 0036, #183). The fixture is the schema-v2 + v1 shape `operator_projection.project()`
   and `dashboard_data.snapshot()` actually publish — the same one briefing.test.js's derive
   coverage uses, so both layers are proven against one shared contract. */
const SNAP = {
  generated_at: '2026-07-30T12:00:00+00:00',
  pools: [{ tool: 'claude', clear: true, spent_pct: 20, headroom_pct: 80, running: 1 }],
  repos: [
    { repo: 'o/r', profile: 'reviewed', ready: [], held: [], parked: [],
      recent_merges: [
        { number: 60, title: 'older landing', merged_at: '2026-07-20T12:00:00Z' },
        { number: 61, title: 'newest landing', merged_at: '2026-07-29T12:00:00Z' },
      ],
      in_flight: [{ number: 20, title: 'awaiting merge', builder: 'codex' }],
      ratchet: { samples: 0, correction_rate: 0, ready_to_loosen: false } },
  ],
  repositories: [
    {
      name_with_owner: 'o/r', url: 'https://github.com/o/r', profile: 'reviewed',
      github: { status: 'fresh', fresh_at: '2026-07-30T12:00:00+00:00', error: null },
      maps: {
        active_total: 1,
        active: [{
          number: 179, title: 'Map: operator briefing', url: 'https://github.com/o/r/issues/179',
          updated_at: '2026-07-30T00:00:00Z',
          progress: { total: 2, closed: 1 },
          frontier_state: 'named', frontier_incomplete: false, handoffs_incomplete: false,
          frontier: [{ number: 183, title: 'Lock the visual spec', url: 'https://github.com/o/r/issues/183' }],
          tickets: [
            { number: 180, title: 'Bound the projection', url: 'https://github.com/o/r/issues/180', status: 'done' },
            { number: 183, title: 'Lock the visual spec', url: 'https://github.com/o/r/issues/183', status: 'frontier' },
          ],
          handoffs: [],
          totals: { children: { shown: 2, total: 2 }, handoffs: { shown: 0, total: 0 },
                   adrs: { shown: 1, total: 1 } },
          support: [{ label: 'ADR 36',
                     url: 'https://github.com/o/r/blob/HEAD/docs/adr/0036-bounded-repository-map-projection.md' }],
        }],
      },
    },
  ],
  fleet: { recent_landed: [] },
  attention: {
    rows: [
      { condition: 'awaiting-merge', kind: 'Merge', repo: 'o/r', number: 20,
        title: '#20 awaiting merge', detail: 'r · reviewed · the review finished — yours to merge',
        url: 'https://github.com/o/r/pull/20' },
    ],
    total: 1,
  },
};

beforeEach(() => vi.useFakeTimers({ now: new Date('2026-07-30T12:00:00Z') }));
afterEach(() => vi.useRealTimers());

describe('Briefing.svelte — typical state', () => {
  it('renders the continuous hierarchy: Attention, then Decision Maps, then Fleet health', () => {
    render(Briefing, { snap: SNAP });
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(['Attention', 'Decision Maps', 'Fleet health']);
  });

  it('renders the attention item as an explicit external GitHub action', () => {
    render(Briefing, { snap: SNAP });
    const link = screen.getByRole('link', { name: /Open in GitHub/ });
    expect(link.getAttribute('href')).toBe('https://github.com/o/r/pull/20');
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('renders the map frontier and its bounded ticket outline', () => {
    const { container } = render(Briefing, { snap: SNAP });
    expect(container.querySelector('.frontier').textContent).toContain('#183 Lock the visual spec');
    expect(screen.getByText(/Ticket outline · 2 records/)).toBeTruthy();
  });

  it('renders the fresh masthead with no stale/incomplete banner', () => {
    render(Briefing, { snap: SNAP });
    expect(screen.getByText(/Updated/)).toBeTruthy();
    expect(screen.queryByRole('status')).toBeNull();
  });
});

describe('Briefing.svelte — the attention queue', () => {
  const row = (over) => ({ condition: 'parked-build', kind: 'Parked', repo: 'o/r', number: 30,
    title: '#30 a stopped build', detail: 'r · a reason', url: 'https://github.com/o/r/pull/30',
    ...over });

  it('renders every published row in the published order, ranking nothing itself', () => {
    const rows = [row({ kind: 'Merge', number: 20 }), row({ number: 30 }),
                  row({ kind: 'Projection', number: null, title: 'r briefing data is stale' })];
    const { container } = render(Briefing, { snap: { ...SNAP, attention: { rows, total: 3 } } });
    const kinds = [...container.querySelectorAll('.attention .kind')].map((k) => k.textContent);
    expect(kinds).toEqual(['Merge', 'Parked', 'Projection']);
  });

  it('reports the daemon\'s true total when the queue truncated, never the rows on the page', () => {
    const rows = Array.from({ length: 25 }, (_, i) => row({ number: i + 1 }));
    const { container } = render(Briefing, { snap: { ...SNAP, attention: { rows, total: 31 } } });
    expect(container.querySelector('.count').textContent).toBe('31 items');
    expect(screen.getByText('6 more operator actions not shown')).toBeTruthy();
    /* One final ruled row in the section's own treatment — not a new element. */
    expect(container.querySelectorAll('.rows .attention')).toHaveLength(26);
  });

  it('shows no overflow row when the whole queue fits', () => {
    const { container } = render(Briefing, { snap: SNAP });
    expect(container.querySelector('.count').textContent).toBe('1 item');
    expect(screen.queryByText(/more operator actions not shown/)).toBeNull();
  });
});

/* A stage that spent sessions and produced nothing across a whole window of finished attempts
   (#430). The daemon decides which stages these are; the page only renders them — including
   the one thing that makes them different, a third column with nowhere to go. */
describe('Briefing.svelte — a stage in drought', () => {
  const drought = (over) => ({ condition: 'stage-drought', kind: 'stage drought', repo: '',
    number: null, title: 'pre-publish attack',
    detail: '0 of its last 10 finished attempts published a hardened brief · 37 sessions spent',
    url: null, note: 'What to check: held drafts and ready-for-agent holdbacks', ...over });

  const withRows = (rows) => ({ ...SNAP, attention: { rows, total: rows.length } });

  it('names the stage and states the drought and what it cost', () => {
    const { container } = render(Briefing, { snap: withRows([drought()]) });
    const row = container.querySelector('.attention');
    expect(row.querySelector('.kind').textContent).toBe('stage drought');
    expect(row.querySelector('h3').textContent).toBe('pre-publish attack');
    expect(row.querySelector('p').textContent).toBe(
      '0 of its last 10 finished attempts published a hardened brief · 37 sessions spent');
  });

  it('says where to look as plain text, never as an action that goes nowhere', () => {
    const { container } = render(Briefing, { snap: withRows([drought()]) });
    expect(screen.queryByRole('link', { name: /Open in GitHub/ })).toBeNull();
    const note = container.querySelector('.attention .action-note');
    expect(note.tagName).toBe('SPAN');
    expect(note.textContent).toBe('What to check: held drafts and ready-for-agent holdbacks');
  });

  it('stacks two simultaneous droughts as two rows in the same ruled treatment', () => {
    const { container } = render(Briefing, { snap: withRows([
      drought(),
      drought({ title: 'review',
        detail: '0 of its last 10 finished attempts recorded a review verdict · 41 sessions spent',
        note: 'What to check: open PRs waiting on a verdict' }),
    ]) });
    const rows = container.querySelectorAll('.rows .attention');
    expect(rows).toHaveLength(2);
    expect([...rows].map((r) => r.querySelector('h3').textContent))
      .toEqual(['pre-publish attack', 'review']);
    expect([...rows].map((r) => r.querySelector('.action-note').textContent)).toEqual([
      'What to check: held drafts and ready-for-agent holdbacks',
      'What to check: open PRs waiting on a verdict']);
    expect(container.querySelector('.count').textContent).toBe('2 items');
  });

  it('counts toward the section count and the bound exactly like any other row', () => {
    const rows = [drought(), ...Array.from({ length: 24 }, (_, i) => ({
      condition: 'parked-build', kind: 'Parked', repo: 'o/r', number: i + 1,
      title: `#${i + 1} a stopped build`, detail: 'r · a reason',
      url: `https://github.com/o/r/pull/${i + 1}`, note: null }))];
    const { container } = render(Briefing, { snap: { ...SNAP,
      attention: { rows, total: 26 } } });
    expect(container.querySelector('.count').textContent).toBe('26 items');
    expect(screen.getByText('1 more operator actions not shown')).toBeTruthy();
  });

  it('renders the shipped reading path untouched when no stage is in drought', () => {
    const { container } = render(Briefing, { snap: SNAP });
    expect(container.querySelector('.action-note')).toBeNull();
    expect(screen.getByRole('link', { name: /Open in GitHub/ })).toBeTruthy();
  });
});

/* The two locked Decision Map states (#374), rendered from the very snapshot the daemon
   shapes — `tests/test_operator_briefing_states.py` builds this file and pins it against the
   real projection, so a page that renders it cannot be picturing a map GitHub could not
   produce. Every string checked below is the daemon's or the manifest's, never invented here. */
/* Every supporting record renders with the surface's external-link affordance appended; the
   record's own words are what these assertions are about. */
const recordText = (a) => a.textContent.replace(/\s*↗\s*$/, '');

describe('Briefing.svelte — the frontier matrix', () => {
  const frontiers = (container) =>
    [...container.querySelectorAll('.frontier')].map((f) => f.textContent.replace('Frontier:', '').trim());

  it('says exactly one of the four things about every map, and nothing else', () => {
    const { container } = render(Briefing, { snap: STATES['map-frontier-matrix'] });
    expect(frontiers(container)).toEqual([
      '#374 Say what the projection could not verify',
      'No open decision remains',
      'blocked',
      'Not verified',
    ]);
  });

  it('marks the map it could not verify as incomplete and says what it could not read', () => {
    const { container } = render(Briefing, { snap: STATES['map-frontier-matrix'] });
    const unverified = [...container.querySelectorAll('.map')].at(-1);
    expect(unverified.querySelector('.status').textContent).toBe('incomplete');
    const support = [...unverified.querySelectorAll('.support a')].map(recordText);
    expect(support).toContain('incomplete — blocker data on 1 decision');
    expect(support.every((s) => s.length > 0)).toBe(true);
  });

  it('keeps every map outline and record list a keyboard-reachable native disclosure', () => {
    const { container } = render(Briefing, { snap: STATES['map-frontier-matrix'] });
    const disclosures = container.querySelectorAll('.map details');
    expect(disclosures).toHaveLength(8); // an outline and a record list for each of four maps
    for (const d of disclosures) {
      expect(d.tagName).toBe('DETAILS');
      expect(d.querySelector('summary')).toBeTruthy();
    }
  });
});

describe('Briefing.svelte — bounded overflow and landed evidence', () => {
  const labels = (container) => [...container.querySelectorAll('.support a')].map(recordText);

  it('shows every truncation as a counted link onto GitHub', () => {
    const { container } = render(Briefing, { snap: STATES['map-overflow-evidence'] });
    const shown = labels(container);
    expect(shown).toContain('incomplete — 2 more decisions on GitHub');
    expect(shown).toContain('1 more handoff on GitHub');
    expect(shown).toContain('2 more decision records on GitHub');
    for (const a of container.querySelectorAll('.support a')) {
      expect(a.getAttribute('href')).toMatch(/^https:\/\/github\.com\//);
    }
  });

  it('says how many times a repeatedly-attempted handoff was tried, and nothing for one try', () => {
    const { container } = render(Briefing, { snap: STATES['map-overflow-evidence'] });
    const shown = labels(container);
    expect(shown).toContain('#420 Build 420 — PR #361 merged · 2 attempts');
    expect(shown).toContain('#419 Build 419 — PR #55 merged');
    expect(shown.some((s) => s.includes('#419') && s.includes('attempts'))).toBe(false);
  });

  it('says a failed evidence read is unavailable, never that the build is still running', () => {
    const { container } = render(Briefing, { snap: STATES['map-overflow-evidence'] });
    const unreadable = [...container.querySelectorAll('.map')].at(-1);
    const shown = [...unreadable.querySelectorAll('.support a')];
    const evidence = shown.find((a) => a.textContent.includes('#601'));
    expect(recordText(evidence)).toBe('#601 Build the map schema — landed evidence unavailable');
    expect(evidence.getAttribute('href'))
      .toBe('https://github.com/ConnorGriffin/wayfinder/issues/601');
  });

  it('counts the maps the fleet has, not the maps that fit on the page', () => {
    const { container } = render(Briefing, { snap: STATES['map-overflow-evidence'] });
    const counts = [...container.querySelectorAll('.count')].map((c) => c.textContent);
    expect(counts[1]).toBe('2 active maps');
  });
});

describe('Briefing.svelte — stale state', () => {
  it('renders the honest stale banner and withholds the frontier claim', () => {
    const stale = { ...SNAP, repositories: [{ ...SNAP.repositories[0],
      github: { status: 'stale' } }] };
    const { container } = render(Briefing, { snap: stale });
    expect(screen.getByRole('status').textContent).toMatch(/not refreshed/);
    expect(container.querySelector('.frontier').textContent).toContain('Not verified');
  });

  it('keeps the fleet-wide banner alongside the per-repository row that names it', () => {
    /* The banner is posture; the row is the action, with a link. They pair, never collapse. */
    const stale = {
      ...SNAP,
      repositories: [{ ...SNAP.repositories[0], github: { status: 'stale' } }],
      attention: { rows: [{ condition: 'stale-data', kind: 'Projection', repo: 'o/r',
        number: null, title: 'r briefing data is stale',
        detail: 'the last read of this repository failed',
        url: 'https://github.com/o/r' }], total: 1 },
    };
    render(Briefing, { snap: stale });
    expect(screen.getByRole('status')).toBeTruthy();
    expect(screen.getByText('r briefing data is stale')).toBeTruthy();
    expect(screen.getByRole('link', { name: /Open in GitHub/ }).getAttribute('href'))
      .toBe('https://github.com/o/r');
  });
});

describe('Briefing.svelte — empty state', () => {
  it('renders the honest empty rows, never a blank section', () => {
    render(Briefing, { snap: { repositories: [], repos: [], pools: [], fleet: { recent_landed: [] } } });
    expect(screen.getByText('No operator actions in this projection.')).toBeTruthy();
    expect(screen.getByText('No active Decision Maps in this bounded projection.')).toBeTruthy();
    expect(screen.getByText('No repositories were included in this projection.')).toBeTruthy();
  });
});

describe('Briefing.svelte — fleet health', () => {
  it('renders a healthy repository\'s landing freshness and health cells', () => {
    const { container } = render(Briefing, { snap: SNAP });
    const repo = container.querySelector('.repo');
    expect(repo.textContent).toContain('2 recent · latest 1d ago');
    expect(repo.textContent).toContain('healthy');
  });

  it('renders a busy repository\'s held count as needing you', () => {
    const busy = { ...SNAP, repos: [{ ...SNAP.repos[0],
      held: [{ number: 10, title: 'needs a call', state: 'needs-grilling', reason: 'r', since: null }] }] };
    const { container } = render(Briefing, { snap: busy });
    expect(container.querySelector('.repo').textContent).toContain('1 needs you');
  });

  it('renders an unavailable (never-verified) map read in words on the fleet row', () => {
    const unavailable = { ...SNAP, repositories: [{ ...SNAP.repositories[0],
      github: { status: 'unavailable' } }] };
    const { container } = render(Briefing, { snap: unavailable });
    expect(container.querySelector('.repo').textContent).toContain('map data unverified');
  });

  it('renders partially observed repositories — some fresh, some not — each labelled on its own row', () => {
    const partial = { ...SNAP,
      repos: [SNAP.repos[0], { ...SNAP.repos[0], repo: 'o/s' }],
      repositories: [SNAP.repositories[0], { ...SNAP.repositories[0], name_with_owner: 'o/s',
        github: { status: 'stale' } }],
    };
    const { container } = render(Briefing, { snap: partial });
    const rows = [...container.querySelectorAll('.repo')].map((r) => r.textContent);
    expect(rows.some((r) => r.includes('map data stale'))).toBe(true);
    expect(rows.some((r) => !r.includes('map data'))).toBe(true);
  });

  it('renders a paused pool\'s block reason, never its published utilization as "% used"', () => {
    const paused = { ...SNAP, pools: [
      { tool: 'claude', clear: false, spent_pct: 0.0, headroom_pct: 0.0, running: 0,
        reason: 'weekly spend at 98% exceeds 80.0% released for unattended work' },
    ] };
    const { container } = render(Briefing, { snap: paused });
    const capacity = container.querySelector('.capacity').textContent;
    expect(capacity).toContain('weekly spend at 98% exceeds 80.0% released for unattended work');
    expect(capacity).not.toMatch(/0(\.0)?% used/);
    expect(capacity).not.toMatch(/headroom/);
    expect(capacity).not.toMatch(/slot/i);
  });
});
