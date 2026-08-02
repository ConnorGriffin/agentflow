import { describe, it, expect } from 'vitest';
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
    { repo: 'o/r', profile: 'reviewed', ready: [], held: [], parked: [], recent_merges: [],
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
};

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

describe('Briefing.svelte — stale state', () => {
  it('renders the honest stale banner and withholds the frontier claim', () => {
    const stale = { ...SNAP, repositories: [{ ...SNAP.repositories[0],
      github: { status: 'stale' } }] };
    const { container } = render(Briefing, { snap: stale });
    expect(screen.getByRole('status').textContent).toMatch(/not refreshed/);
    expect(container.querySelector('.frontier').textContent).toContain('Not verified');
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
