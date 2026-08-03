import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import Briefing from './Briefing.svelte';

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
          updated_at: '2026-07-30T00:00:00Z', complete: true,
          progress: { total: 2, closed: 1 },
          frontier: [{ number: 183, title: 'Lock the visual spec', url: 'https://github.com/o/r/issues/183' }],
          tickets: [
            { number: 180, title: 'Bound the projection', url: 'https://github.com/o/r/issues/180', status: 'done' },
            { number: 183, title: 'Lock the visual spec', url: 'https://github.com/o/r/issues/183', status: 'frontier' },
          ],
          handoffs: [], handoffs_overflow: false,
          adrs: [{ label: 'ADR 36', url: 'docs/adr/0036-bounded-repository-map-projection.md' }],
          adrs_overflow: 0,
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
