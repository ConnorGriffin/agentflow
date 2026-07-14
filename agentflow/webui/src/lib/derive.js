/* The needs-you worklist, derived from TODAY's snapshot contract (dispatch + pools
   + repos). This is the current dashboard's derivation, lifted unchanged so the v2
   console lands with identical Inbox behavior:

     - reviewed/guarded repos' in-flight PRs = awaiting your merge
         guarded ranks first (weight 1), reviewed second (weight 2)
     - a repo whose trust ratchet is ready to loosen = a loosen prompt (weight 3)
     - autonomous in-flight PRs auto-merge, so they NEVER enter the inbox

   Ranked most-urgent-first. The proposed-v2 stage/checks/held/parked fields are NOT
   read here — those arrive in later slices when the snapshot carries them. */

export const reviewerOf = (builder) => (builder === 'claude' ? 'codex' : 'claude');

export function deriveInbox(snap) {
  const items = [];
  for (const r of snap.repos || []) {
    const needsHumanMerge = r.profile === 'reviewed' || r.profile === 'guarded';
    if (needsHumanMerge) {
      for (const pr of r.in_flight || []) {
        items.push({
          kind: 'merge',
          accent: r.profile,
          weight: r.profile === 'guarded' ? 1 : 2,
          repo: r.repo,
          profile: r.profile,
          number: pr.number,
          title: pr.title,
          builder: pr.builder,
          reviewer: reviewerOf(pr.builder),
        });
      }
    }
    if (r.ratchet && r.ratchet.ready_to_loosen) {
      items.push({
        kind: 'loosen',
        accent: 'loosen',
        weight: 3,
        repo: r.repo,
        profile: r.profile,
        samples: r.ratchet.samples,
        rate: r.ratchet.correction_rate,
      });
    }
  }
  return items.sort((a, b) => a.weight - b.weight);
}

export const short = (repo) => repo.split('/').pop();
export const pct = (v) => Math.max(0, Math.min(100, v));
export const headroomColor = (h) =>
  h > 40 ? 'var(--green)' : h > 15 ? 'var(--amber)' : 'var(--red)';

export function ratchetBar(rt) {
  const s = (rt && rt.samples) || 0;
  const corr = (rt && rt.correction_rate) || 0;
  if (s === 0) return { barW: 0, barC: 'var(--muted)', stat: '0 samples · earning trust' };
  const p = Math.round(corr * 100);
  const c = corr < 0.1 ? 'var(--green)' : corr < 0.25 ? 'var(--amber)' : 'var(--red)';
  return { barW: p, barC: c, stat: `${s} decisions · ${p}% corrected` };
}

export function rel(iso, nowMs) {
  if (!iso) return '—';
  const d = Math.max(0, Math.floor((nowMs - Date.parse(iso)) / 1000));
  if (d < 60) return 'just now';
  if (d < 3600) return Math.floor(d / 60) + 'm ago';
  if (d < 86400) return Math.floor(d / 3600) + 'h ago';
  return Math.floor(d / 86400) + 'd ago';
}

/* Compact elapsed (no "ago") for a live session's ticking timer — lifted from the locked
   mockup so a row reads 12s → 5m → 1h 3m as it runs. */
export function elapsed(iso, nowMs) {
  if (!iso) return '—';
  const d = Math.max(0, Math.floor((nowMs - Date.parse(iso)) / 1000));
  if (d < 60) return d + 's';
  const m = Math.floor(d / 60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

/* The live pipeline in flow order (glyph = a "filling clock" through the pipe). */
export const STAGES = [
  { key: 'triaging', label: 'triaging', glyph: '◔' },
  { key: 'building', label: 'building', glyph: '◑' },
  { key: 'reviewing', label: 'reviewing', glyph: '◕' },
];
