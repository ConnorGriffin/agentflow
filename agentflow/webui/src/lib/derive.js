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

/* --- Operator briefing (ADR 0035/0036, locked spec #183) -----------------------------
   Turns the schema-v2 nested contract (`snap.repositories[].maps`/`.github`) plus the
   existing v1 fields (`snap.repos[].held/parked/in_flight`, `snap.pools`) into the flat
   attention/maps/fleet/capacity shape the locked mockups/operator-surface-finalist.html
   renders. The backend already computed frontier classification, handoff verification,
   bounds, and freshness (ADR 0036: "the browser derives presentation only") — every
   function below only flattens, formats, and ranks what the daemon already decided. */

const ATTENTION_KIND = { merge: 'Merge', held: 'Held', parked: 'Parked', loosen: 'Trust' };

function attentionDetail(item) {
  if (item.kind === 'merge') return `${short(item.repo)} · ${item.profile} · reviewer ${item.reviewer}`;
  if (item.kind === 'held') return `${short(item.repo)} · ${(HELD[item.state] || {}).label || item.state}`;
  if (item.kind === 'parked') return `${short(item.repo)} · ${(PARKED[item.reason] || {}).why || item.reason}`;
  return short(item.repo);
}

/* Reuses `deriveInbox`'s exact ranking (guarded merge > reviewed merge > held > parked >
   loosen, oldest-first within a weight) rather than re-deriving it — this section and the
   v1 Inbox tab answer the same question ("what needs you") from the same source fields. */
export function deriveAttention(snap) {
  return deriveInbox(snap).map((item) => {
    if (item.kind === 'loosen') {
      return {
        kind: ATTENTION_KIND.loosen, title: `${short(item.repo)} ready to loosen`,
        detail: `${item.samples} decisions · ${Math.round((item.rate || 0) * 100)}% corrected`,
        url: `https://github.com/${item.repo}`,
      };
    }
    const path = item.kind === 'held' ? 'issues' : 'pull';
    return {
      kind: ATTENTION_KIND[item.kind] || item.kind,
      title: `#${item.number} ${item.title}`,
      detail: attentionDetail(item),
      url: `https://github.com/${item.repo}/${path}/${item.number}`,
    };
  });
}

function handoffLabel(handoff) {
  const pipe = handoff.pipeline;
  const state = pipe && (pipe.state === 'merged' ? `PR #${pipe.pr_number} merged`
    : pipe.state === 'in_review' ? `PR #${pipe.pr_number} in review`
    : pipe.state === 'pr_open' ? `PR #${pipe.pr_number} open` : 'building');
  return `#${handoff.number} ${handoff.title} — ${state || 'building'}`;
}

/* One flat row per active map across every repository, in the order the daemon returned
   them. `frontier` reads "Not verified" whenever that repository's map read is not fresh
   (ADR 0036: stale/incomplete data never claims a verified frontier) — never recomputed
   from the child list, only gated on the freshness the backend already stamped. */
export function deriveMaps(snap) {
  const out = [];
  for (const repo of snap?.repositories || []) {
    const verified = repo.github?.status === 'fresh';
    for (const m of repo.maps?.active || []) {
      out.push({
        title: m.title,
        repository: short(repo.name_with_owner),
        progress: `${m.progress.closed} of ${m.progress.total} decided`,
        status: m.complete ? 'bounded' : `truncated · ${m.progress.total} total`,
        frontier: !verified ? 'Not verified'
          : m.frontier.length ? m.frontier.map((f) => `#${f.number} ${f.title}`).join(', ')
          : 'None open',
        tickets: m.tickets.map((t) => `#${t.number} ${t.title} — ${t.status}`),
        support: [
          ...(m.adrs || []).map((a) => ({ url: a.url, label: a.label })),
          ...(m.handoffs || []).map((h) => ({ url: h.url, label: handoffLabel(h) })),
          { url: m.url, label: 'Map on GitHub ↗' },
        ],
      });
    }
  }
  return out;
}

export function deriveFleet(snap) {
  return (snap?.repos || []).map((r) => {
    const needsAttention = (r.held || []).length > 0 || (r.parked || []).length > 0;
    return {
      name: short(r.repo), profile: r.profile,
      work: `${(r.in_flight || []).length} in flight · ${(r.ready || []).length} ready`,
      health: needsAttention ? 'needs attention' : 'healthy',
    };
  });
}

export function deriveCapacity(snap) {
  return (snap?.pools || []).map((p) => ({
    name: p.tool,
    detail: p.clear ? `${p.headroom_pct}% headroom · ${p.running} running` : (p.reason || 'blocked'),
  }));
}

/* The masthead/banner state: worst-case across every repository's Decision Map freshness
   (ADR 0036's three states). No repository read yet at all reads as `incomplete`, the same
   honest-empty contract the endpoint itself returns before any daemon has published. */
export function deriveFreshness(snap) {
  const repos = snap?.repositories || [];
  if (!repos.length) {
    return { state: 'incomplete', label: 'Projection unavailable',
            message: 'No daemon-published projection yet. Open GitHub for authoritative state.' };
  }
  const statuses = repos.map((r) => r.github?.status || 'unavailable');
  if (statuses.every((s) => s === 'fresh')) {
    return { state: 'fresh',
            label: snap.generated_at ? `Updated ${rel(snap.generated_at, Date.now())}` : 'Updated' };
  }
  if (statuses.includes('unavailable')) {
    return { state: 'incomplete', label: 'Projection incomplete',
            message: 'One or more repositories have never published a verified Decision Map '
              + 'read. Open GitHub for authoritative state.' };
  }
  return { state: 'stale', label: 'Some repositories are stale',
          message: 'One or more repositories have not refreshed within two heartbeats. '
            + 'Frontier and map data may be out of date.' };
}

export function deriveBriefing(snap) {
  return {
    freshness: deriveFreshness(snap),
    attention: deriveAttention(snap),
    maps: deriveMaps(snap),
    fleet: deriveFleet(snap),
    capacity: deriveCapacity(snap),
  };
}
