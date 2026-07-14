/* The needs-you worklist, derived from the v2 snapshot contract (dispatch + pools +
   repos, each repo carrying held[] + parked[]). Ranked most-urgent-first, exactly as the
   locked spec derives:

     - reviewed/guarded in-flight PRs = awaiting your merge (guarded 1, reviewed 2)
     - held issues: needs-grilling (3) outranks needs-mockup (4)
     - parked PRs = a stalled build waiting on your decision (5)
     - a repo whose trust ratchet is ready to loosen = a loosen prompt (6)
     - autonomous in-flight PRs auto-merge, so they NEVER enter the inbox

   Within one weight, the oldest waiting item ranks first (it's waited longest). */

export const reviewerOf = (builder) => (builder === 'claude' ? 'codex' : 'claude');
const age = (iso) => (iso ? Date.parse(iso) : 0); // older `since` → smaller → ranks first

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
    for (const h of r.held || []) {
      items.push({
        kind: 'held',
        accent: 'held',
        weight: h.state === 'needs-grilling' ? 3 : 4,
        repo: r.repo,
        profile: r.profile,
        number: h.number,
        title: h.title,
        state: h.state,
        reason: h.reason,
        since: h.since,
      });
    }
    for (const p of r.parked || []) {
      items.push({
        kind: 'parked',
        accent: 'parked',
        weight: 5,
        repo: r.repo,
        profile: r.profile,
        number: p.number,
        title: p.title,
        reason: p.reason,
        builder: p.builder,
        reviewer: p.reviewer || reviewerOf(p.builder),
        since: p.since,
      });
    }
    if (r.ratchet && r.ratchet.ready_to_loosen) {
      items.push({
        kind: 'loosen',
        accent: 'loosen',
        weight: 6,
        repo: r.repo,
        profile: r.profile,
        samples: r.ratchet.samples,
        rate: r.ratchet.correction_rate,
      });
    }
  }
  return items.sort((a, b) => a.weight - b.weight || age(a.since) - age(b.since));
}

/* parked reason → primary action + plain "why it stopped" (lifted from the locked spec) */
export const PARKED = {
  'drop-to-reviewed': { act: 'Review drop', why: 'builder wants to drop autonomy — your call' },
  'failed-merge': { act: 'Retry merge', why: 'merge failed — needs a retry or a fix' },
  'open-question': { act: 'Answer', why: 'an open question stopped the build' },
  'ui-evidence': { act: 'Add proof', why: 'a UI change with no screenshot evidence' },
};
export const HELD = {
  'needs-grilling': { act: 'Reply', label: 'needs grilling' },
  'needs-mockup': { act: 'Mock up', label: 'needs mockup' },
};

export const short = (repo) => repo.split('/').pop();
export const pct = (v) => Math.max(0, Math.min(100, v));
export const headroomColor = (h) =>
  h > 40 ? 'var(--green)' : h > 15 ? 'var(--amber)' : 'var(--red)';

/* trust-ratchet → bar width/color + a stat line. barW = corr% (how much corrected);
   trustW = 100−corr% (the Fleet bar, so a trusted repo shows a FULL bar). 0 samples reads
   as "earning trust". Lifted from the locked spec. */
export function ratchetBar(rt) {
  const s = (rt && rt.samples) || 0;
  const corr = (rt && rt.correction_rate) || 0;
  if (s === 0)
    return { barW: 0, trustW: 0, barC: 'var(--muted)', stat: '0 samples · earning trust', short: 'new' };
  const p = Math.round(corr * 100);
  const c = corr < 0.1 ? 'var(--green)' : corr < 0.25 ? 'var(--amber)' : 'var(--red)';
  return { barW: p, trustW: 100 - p, barC: c, stat: `${s} decisions · ${p}% corrected`, short: p + '% corr' };
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
