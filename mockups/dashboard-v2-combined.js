/* ============================================================================
   agentflow · dashboard v2 — COMBINED (round-2 lock candidate).

   A SYNTHESIS of four grounded variants — not a fresh design:
     · SHELL   ← dashboard-v2-console.js  (router, tab bar, drill-down drawer,
                 keyboard 1-4 / j/k / Enter / Esc, header + CLI status bar)
     · INBOX   ← dashboard-v2-queue.js    (ranked, numbered, one-action rows)
     · LIVE    ← dashboard-v2-live.js      (triaging→building→reviewing kanban)
     · FLEET   ← dashboard-v2-grid.js      (repo×signals master table, expand-in-place)
     · HISTORY ← console's flat merged/audit feed

   Binds the PROPOSED v2 snapshot (dashboard-v2.capture.json):
     daemon   {enabled, last_cycle_at, poll_seconds, gh_fresh_at}
     pools[]  {tool, clear, spent_pct, headroom_pct, running}   ← running = live count
     running[]{repo, number, title, stage, tool, model, branch, worktree, pid, started_at}
     repos[]  {repo, profile, ready[], held[], in_flight[], parked[],
               recent_merges[], ratchet{samples, correction_rate, ready_to_loosen}}

   Concurrency is headroom-bounded now (the serial cap is gone), so running[] holds
   many sessions across stages/tools and the Live kanban is exercised at real
   occupancy. pools[].running is derived from running[] so the correlation stays
   honest across every mock state.

   Forked helpers (esc, short, pct, headroomColor, rel, reviewerOf, ratchetBar,
   elapsed) are consolidated here — one copy, lifted from the source variants.
   ========================================================================== */
const CAPTURE_URL = './dashboard-v2.capture.json';

/* ---- forked helpers (consolidated from the four sources) ---- */
const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const short = repo => repo.split('/').pop();
const pct = v => Math.max(0, Math.min(100, v));
const headroomColor = h => h > 40 ? 'var(--green)' : h > 15 ? 'var(--amber)' : 'var(--red)';
const reviewerOf = builder => builder === 'claude' ? 'codex' : 'claude';

/* relative "…ago" for audit feeds + drawer timestamps */
function rel(iso, nowMs){
  if (!iso) return '—';
  let d = Math.max(0, Math.floor((nowMs - Date.parse(iso)) / 1000));
  if (d < 60)    return 'just now';
  if (d < 3600)  return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
}
/* compact elapsed (no "ago") for live sessions + how-long-waiting labels */
function elapsed(iso, nowMs){
  if (!iso) return '—';
  let d = Math.max(0, Math.floor((nowMs - Date.parse(iso)) / 1000));
  if (d < 60) return d + 's';
  const m = Math.floor(d/60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m/60), rem = m % 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}
/* trust-ratchet: correction-rate → width/color + a stat line. barW = corr% (the
   drawer's "how much corrected"); trustW = 100−corr% (the Fleet bar, so a trusted
   repo shows a FULL bar). 0 samples reads as "earning trust" (gates on). */
function ratchetBar(rt){
  const s = (rt && rt.samples) || 0, corr = (rt && rt.correction_rate) || 0;
  if (s === 0) return {barW:0, trustW:0, barC:'var(--muted)', stat:'0 samples · earning trust', short:'new'};
  const p = Math.round(corr*100);
  const c = corr < 0.10 ? 'var(--green)' : corr < 0.25 ? 'var(--amber)' : 'var(--red)';
  return {barW:p, trustW:100-p, barC:c, stat:`${s} decisions · ${p}% corrected`, short:p+'% corr'};
}
function checksMark(c){
  if (c === 'passing') return {g:'✓', cls:'ok', label:'checks passing'};
  if (c === 'failing') return {g:'✗', cls:'bad', label:'checks failing'};
  return {g:'◷', cls:'muted', label:'checks pending'};
}

/* ---- shared vocab ---- */
const PROFILE_COLOR = {autonomous:'var(--green)', reviewed:'var(--blue)', guarded:'var(--amber)'};
const CHECK_GLYPH = {passing:'<span class="chk">✓ checks passing</span>',
                     pending:'<span class="caution">◔ checks running</span>',
                     failing:'<span class="fail">✕ checks failing</span>'};
/* leading glyph per inbox kind — shape carries meaning (never color alone) */
const KIND_GLYPH = {merge:'↑', held:'?', parked:'‖', loosen:'◉'};
/* the live pipeline, in flow order (glyph = "filling clock" through the pipe) */
const STAGES = [
  {key:'triaging',  label:'triaging',  glyph:'◔'},
  {key:'building',  label:'building',  glyph:'◑'},
  {key:'reviewing', label:'reviewing', glyph:'◕'},
];
/* parked reason → primary action + plain-English "why it stopped" */
const PARKED = {
  'drop-to-reviewed':{act:'Review drop', why:'builder wants to drop autonomy — your call'},
  'failed-merge':    {act:'Retry merge', why:'merge failed — needs a retry or a fix'},
  'open-question':   {act:'Answer',      why:'an open question stopped the build'},
  'ui-evidence':     {act:'Add proof',   why:'a UI change with no screenshot evidence'},
};
const HELD = {
  'needs-grilling':{act:'Reply',  label:'needs grilling'},
  'needs-mockup':  {act:'Mock up', label:'needs mockup'},
};

/* anchor "now" to the newest timestamp so a static capture reads as a live feed;
   the tick() below advances elapsed labels up from there (MOCK-ONLY convenience —
   the real app uses the wall clock). */
function anchorNow(snap){
  let max = 0;
  const bump = t => { const p = Date.parse(t); if (p > max) max = p; };
  if (snap.daemon){ bump(snap.daemon.last_cycle_at); bump(snap.daemon.gh_fresh_at); }
  for (const r of (snap.running || [])) bump(r.started_at);
  for (const r of snap.repos){
    for (const m of (r.recent_merges || [])) bump(m.merged_at);
    for (const h of (r.held || [])) bump(h.since);
    for (const p of (r.parked || [])) bump(p.since);
  }
  return max || Date.now();
}

/* ============================================================================
   Inbox derivation — the needs-you worklist, derived EXACTLY as queue does:
   reviewed/guarded in_flight awaiting-merge (checks passing) + every held[] +
   every parked[] + any repo with ratchet.ready_to_loosen. Autonomous in_flight
   auto-merges → never in the inbox. Ranked most-urgent-first by weight.
   Each item carries `kind` (drives the row + drawer) and `profile` (for badges).
   ========================================================================== */
function deriveInbox(snap){
  const items = [];
  for (const r of snap.repos){
    const humanMerges = r.profile === 'reviewed' || r.profile === 'guarded';
    if (humanMerges){
      for (const pr of (r.in_flight || [])){
        const awaiting = pr.stage === 'awaiting-merge' && pr.checks === 'passing';
        if (!awaiting) continue;                       // still building/reviewing → Live, not Inbox
        items.push({kind:'merge', accent:r.profile, weight:r.profile === 'guarded' ? 1 : 2,
          repo:r.repo, profile:r.profile, number:pr.number, title:pr.title,
          builder:pr.builder, reviewer:pr.reviewer || reviewerOf(pr.builder), checks:pr.checks});
      }
    }
    for (const h of (r.held || [])){
      items.push({kind:'held', accent:'held', weight:h.state === 'needs-grilling' ? 3 : 4,
        repo:r.repo, profile:r.profile, number:h.number, title:h.title,
        state:h.state, reason:h.reason, since:h.since});
    }
    for (const p of (r.parked || [])){
      items.push({kind:'parked', accent:'parked', weight:5, repo:r.repo, profile:r.profile,
        number:p.number, title:p.title, reason:p.reason,
        builder:p.builder, reviewer:p.reviewer || reviewerOf(p.builder), since:p.since});
    }
    if (r.ratchet && r.ratchet.ready_to_loosen){
      items.push({kind:'loosen', accent:'loosen', weight:6, repo:r.repo, profile:r.profile,
        samples:r.ratchet.samples, rate:r.ratchet.correction_rate});
    }
  }
  return items.sort((a,b) => a.weight - b.weight);
}

/* ============================================================================
   MOCK-ONLY state transforms — reshape the fetched capture client-side so every
   review state is reachable from the toolbar. Not part of the real app.
   ========================================================================== */
function quietize(snap){                         // inbox-zero + nothing running
  const s = structuredClone(snap);
  s.running = [];
  s.pools = (s.pools || []).map((p, i) => ({...p, clear:true,
    spent_pct:i ? 8 : 12, headroom_pct:i ? 92 : 88, running:0}));
  s.daemon = {...s.daemon, last_cycle_at:s.daemon.gh_fresh_at};
  for (const r of s.repos){
    r.held = []; r.parked = [];
    r.in_flight = (r.in_flight || []).filter(pr => r.profile === 'autonomous' && pr.stage !== 'awaiting-merge');
    if (r.ratchet) r.ratchet.ready_to_loosen = false;
  }
  return s;
}
function densify(snap){                           // inbox full + Live busy across lanes
  const s = structuredClone(snap);
  // spread duplicated sessions across all three lanes so every lane is occupied
  const base = (s.running || []).slice();
  const extra = [];
  let seq = 900;
  for (let k = 0; k < 3; k++){
    base.forEach((x, idx) => {
      const stage = STAGES[(idx + k) % 3].key;
      const shift = ((k*7 + idx*3 + 1) * 60 * 1000);   // vary elapsed
      extra.push({...x, number:x.number + (++seq),
        stage, branch:stage === 'triaging' ? null : x.branch,
        started_at:new Date(Date.parse(x.started_at) - shift).toISOString(),
        pid:x.pid + seq});
    });
  }
  s.running = base.concat(extra);
  // busier pools (claude scarce → red); running counts are recomputed in activeSnap()
  s.pools = [
    {tool:'claude', clear:false, spent_pct:88.0, headroom_pct:12.0, running:0},
    {tool:'codex',  clear:false, spent_pct:67.0, headroom_pct:33.0, running:0}
  ];
  // clone held/parked/awaiting-merge across repos so the inbox fills out
  for (const r of s.repos){
    if ((r.parked || []).length)
      r.parked = r.parked.concat(r.parked.map(p => ({...p, number:p.number + 900, title:p.title + ' (follow-up)'})));
    if ((r.held || []).length)
      r.held = r.held.concat(r.held.map(h => ({...h, number:h.number + 900, title:h.title + ' (variant)'})));
    if (r.profile === 'reviewed' || r.profile === 'guarded')
      for (const pr of (r.in_flight || [])){
        pr.stage = 'awaiting-merge';
        pr.checks = pr.checks === 'failing' ? 'failing' : 'passing';
      }
  }
  return s;
}

/* ============================================================================
   INBOX view (← queue): ranked, numbered, one-action-per-row worklist.
   ========================================================================== */
function renderInbox(snap, nowMs){
  const items = deriveInbox(snap);
  STATE.rows.inbox = items;                       // for j/k + Enter
  const el = document.getElementById('view-inbox');
  const head = `<div class="viewhead"><h2>Needs you</h2>
    <span class="cnt">${items.length}</span>
    <span class="sub">exceptions only · most urgent first · one action each</span></div>`;
  if (!items.length){
    el.innerHTML = head + `<div class="zero">
      <div class="mark"><svg width="24" height="24" viewBox="0 0 24 24" fill="none"
        stroke="var(--green)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 12.5l5 5L20 6"/></svg></div>
      <h3>Inbox zero</h3>
      <p>Nothing awaits your merge, nothing's held or parked, no dial is ready to
      loosen. The fleet is running itself — check Live for what it's doing now.</p></div>`;
    return;
  }
  el.innerHTML = head + `<div class="list">` +
    items.map((it, i) => `<div class="qrow ${it.accent}" data-view="inbox" data-i="${i}" style="--i:${i}">
      <div class="qidx">${String(i + 1).padStart(2, '0')}</div>${inboxBody(it, nowMs)}</div>`).join('') +
    `</div>`;
}
function inboxBody(it, nowMs){
  if (it.kind === 'merge')  return mergeRow(it);
  if (it.kind === 'held')   return heldRow(it, nowMs);
  if (it.kind === 'parked') return parkedRow(it, nowMs);
  return loosenRow(it);
}
function mergeRow(it){
  const care = it.profile === 'guarded'
    ? `<span class="care">read before merging</span>` : `<span class="qmuted">quick glance</span>`;
  return `
    <div class="qglyph k-${it.accent}">${KIND_GLYPH.merge}</div>
    <div class="qbody">
      <div class="qtitle"><span class="num">#${it.number}</span> ${esc(it.title)}
        <a class="open" href="#" title="open PR ↗" data-noop>↗</a></div>
      <div class="qmeta"><b class="k-${it.accent}">${it.profile}</b> merge · ${esc(short(it.repo))} ·
        <span class="tool-${it.builder}">${it.builder}</span>→<span class="tool-${it.reviewer}">${it.reviewer}</span>
        <span class="ok">✓</span> · checks passing · ${care}</div>
    </div>
    <div class="qaside"><span class="ok">checks ✓</span></div>
    <button class="qact ${it.accent}" data-act aria-label="Merge #${it.number} in ${esc(short(it.repo))}">Merge #${it.number}</button>`;
}
function heldRow(it, nowMs){
  const a = HELD[it.state] || {act:'Pickup', label:it.state};
  return `
    <div class="qglyph k-held">${KIND_GLYPH.held}</div>
    <div class="qbody">
      <div class="qtitle"><span class="num">#${it.number}</span> ${esc(it.title)}
        <a class="open" href="#" title="open issue ↗" data-noop>↗</a></div>
      <div class="qmeta"><b class="k-held">held</b> · ${esc(a.label)} · ${esc(short(it.repo))} · ${esc(it.reason)}</div>
    </div>
    <div class="qaside">held <span data-since="${esc(it.since)}">${elapsed(it.since, nowMs)}</span></div>
    <button class="qact held" data-act aria-label="${a.act} on #${it.number} in ${esc(short(it.repo))}">${a.act}</button>`;
}
function parkedRow(it, nowMs){
  const p = PARKED[it.reason] || {act:'Resume', why:it.reason};
  return `
    <div class="qglyph k-parked">${KIND_GLYPH.parked}</div>
    <div class="qbody">
      <div class="qtitle"><span class="num">#${it.number}</span> ${esc(it.title)}
        <a class="open" href="#" title="open PR ↗" data-noop>↗</a></div>
      <div class="qmeta"><b class="k-parked">parked</b> · ${esc(it.reason)} · ${esc(short(it.repo))} ·
        <span class="tool-${it.builder}">${it.builder}</span>→<span class="tool-${it.reviewer}">${it.reviewer}</span> ·
        ${esc(p.why)}</div>
    </div>
    <div class="qaside">parked <span data-since="${esc(it.since)}">${elapsed(it.since, nowMs)}</span></div>
    <button class="qact parked" data-act aria-label="${p.act} on #${it.number} in ${esc(short(it.repo))}">${p.act}</button>`;
}
function loosenRow(it){
  const rate = Math.round((it.rate || 0) * 100);
  return `
    <div class="qglyph k-loosen">${KIND_GLYPH.loosen}</div>
    <div class="qbody">
      <div class="qtitle">${esc(short(it.repo))} — trust ratchet ready to loosen</div>
      <div class="qmeta"><b class="k-loosen">ratchet</b> · ${it.samples} clean samples · ${rate}% corrected —
        consistently confirmed. Move toward more autonomy?</div>
    </div>
    <div class="qaside"><span class="k-loosen">${it.samples} clean</span></div>
    <button class="qact loosen" data-act aria-label="Loosen the dial on ${esc(short(it.repo))}">Loosen dial</button>`;
}

/* ============================================================================
   LIVE view (← live): triaging→building→reviewing kanban. Compact session rows,
   always-visible per-stage counts, pool↔lane correlation (running-count + headroom).
   ========================================================================== */
function renderLive(snap, nowMs){
  const runs = snap.running || [];
  STATE.rows.live = runs;                          // flat order for j/k + Enter
  const el = document.getElementById('view-live');
  const counts = {triaging:0, building:0, reviewing:0};
  for (const s of runs) if (counts[s.stage] != null) counts[s.stage]++;

  // pool↔lane correlation strip — running-count tied to headroom (rule 4)
  const corr = (snap.pools || []).map(p => {
    const h = pct(p.headroom_pct);
    return `<span class="pc"><span class="nm tool-${p.tool}">${p.tool}</span>
      <span class="run"><b>${p.running || 0}</b> running</span>
      <span class="cap"><i style="width:${h}%;background:${headroomColor(h)}"></i></span>
      <span class="hr">${h}%</span></span>`;
  }).join('');

  const head = `<div class="board-head">
    <span class="bh-title">Live sessions</span>
    <span class="flow">triaging<b>${counts.triaging}</b>
      <span class="arrow">→</span> building<b>${counts.building}</b>
      <span class="arrow">→</span> reviewing<b>${counts.reviewing}</b></span>
    <span class="poolcorr">${corr}</span></div>`;

  if (!runs.length){
    // honest idle — the 0/0/0 per-stage counts read plainly, not three empty boxes
    const enabled = snap.daemon && snap.daemon.enabled;
    const poll = snap.daemon && snap.daemon.poll_seconds ? Math.round(snap.daemon.poll_seconds/60) : 5;
    el.innerHTML = head + `<div class="board-idle">
      <div class="idle-mark">◍</div>
      <div class="idle-counts"><b>0 triaging · 0 building · 0 reviewing</b> — fleet idle</div>
      <div class="idle-p">Nothing is executing this second; the pipeline is clear.
        ${enabled ? `The daemon polls every ${poll}m for new work.`
                  : `The daemon is paused — no new sessions will start.`}</div></div>`;
    return;
  }
  el.innerHTML = head + `<div class="lanes">` + STAGES.map(st => {
    const rows = runs
      .map((s, i) => ({s, i}))
      .filter(o => o.s.stage === st.key)
      // longest-running on top (most likely to want a look)
      .sort((a, b) => Date.parse(a.s.started_at) - Date.parse(b.s.started_at))
      .map(o => sessionRow(o.s, o.i, nowMs)).join('') || `<div class="lane-empty">— no sessions —</div>`;
    return `<section class="lane" data-stage="${st.key}">
      <header class="lane-head">
        <span class="lane-glyph">${st.glyph}</span>
        <span class="lane-name">${st.label}</span>
        <span class="lane-count">${counts[st.key]}</span>
      </header>
      <div class="lane-rows">${rows}</div>
    </section>`;
  }).join('') + `</div>`;
}
function sessionRow(s, i, nowMs){
  const tc = s.tool === 'claude' ? 'var(--amber)' : 'var(--blue)';
  const foot = s.branch ? esc(s.branch) : `#${s.number} · not yet branched`;
  return `<div class="sess" data-view="live" data-i="${i}">
    <span class="pulse" style="--p:${tc}" aria-hidden="true"></span>
    <div class="sess-main">
      <div class="sess-id">
        <span class="repo">${esc(short(s.repo))}</span><span class="num">#${s.number}</span>
        <span class="tbadge tool-${s.tool}">${esc(s.tool)}·${esc(s.model)}</span>
      </div>
      <div class="sess-title" title="${esc(s.title)}">${esc(s.title)}</div>
      <div class="sess-foot"><span class="branch" title="${esc(s.worktree || '')}">${foot}</span></div>
    </div>
    <div class="sess-elapsed" data-started="${esc(s.started_at)}">${elapsed(s.started_at, nowMs)}</div>
  </div>`;
}

/* ============================================================================
   FLEET view (← grid): one dense master table, repo row as the spine; columns are
   the signals. "Needs you" is a cross-cutting row highlight + filter over the same
   table; a repo row expands IN PLACE into its lane (caret), and opens the shared
   drawer on a row-body click / Enter (rule 5). Headroom is NOT redrawn here — it
   lives once in the header (rule 3).
   ========================================================================== */
const FLEET_COLS = [
  {key:null,       label:'',          cls:'c-caret'},
  {key:'repo',     label:'repo',      cls:'c-repo'},
  {key:'profile',  label:'profile',   cls:'c-prof'},
  {key:'running',  label:'running',   cls:'c-run'},
  {key:'ready',    label:'ready',     cls:'c-num', num:true, hint:'issues queued for an agent'},
  {key:'inflight', label:'in-flight', cls:'c-flight',       hint:'open PRs in the pipeline'},
  {key:'held',     label:'held',      cls:'c-num', num:true, hint:'issues held: needs-grilling / needs-mockup'},
  {key:'parked',   label:'parked',    cls:'c-num', num:true, hint:'PRs parked for your decision'},
  {key:'ratchet',  label:'ratchet',   cls:'c-rat',          hint:'trust ratchet — progress toward more autonomy'},
];
const FLEET_SORTS = {
  repo: r => short(r.repo).toLowerCase(),
  profile: r => ({autonomous:0, reviewed:1, guarded:2}[r.profile]),
  running: r => r._running.length,
  ready: r => (r.ready || []).length,
  inflight: r => (r.in_flight || []).length,
  held: r => (r.held || []).length,
  parked: r => (r.parked || []).length,
  ratchet: r => (r.ratchet ? r.ratchet.correction_rate : 1),
  needs: r => r._needs.length,
};
function repoNeeds(r){
  const out = [];
  if (r.profile === 'reviewed' || r.profile === 'guarded')
    for (const pr of r.in_flight || []) if (pr.stage === 'awaiting-merge') out.push({kind:'merge', number:pr.number});
  for (const h of r.held || []) out.push({kind:'held', number:h.number});
  for (const p of r.parked || []) out.push({kind:'parked', number:p.number});
  if (r.ratchet && r.ratchet.ready_to_loosen) out.push({kind:'loosen'});
  return out;
}
function fleetSort(rows){
  const f = FLEET_SORTS[STATE.fleet.sort.key] || FLEET_SORTS.needs;
  const dir = STATE.fleet.sort.dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = f(a), vb = f(b);
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return short(a.repo).toLowerCase() < short(b.repo).toLowerCase() ? -1 : 1;
  });
}
function runningCell(sessions, now){
  if (!sessions.length) return `<span class="dim">idle</span>`;
  const s = sessions[0];
  const more = sessions.length > 1 ? ` <span class="dim">+${sessions.length - 1}</span>` : '';
  return `<span class="run">
    <span class="pulse tool-${s.tool}"></span>
    <b class="rstage">${esc(s.stage)}</b>
    <span class="tool-${s.tool}">${esc(s.tool)}</span>
    <span class="dim num">· ${elapsed(s.started_at, now)}</span>${more}</span>`;
}
function inflightCell(r){
  const prs = r.in_flight || [];
  if (!prs.length) return `<span class="dim">—</span>`;
  const pr = prs[0];
  const ck = checksMark(pr.checks);
  const awaiting = pr.stage === 'awaiting-merge';
  const human = awaiting && r.profile !== 'autonomous';
  const more = prs.length > 1 ? ` <span class="dim">+${prs.length - 1}</span>` : '';
  let tail;
  if (human) tail = `<button class="mrg" data-act="merge" data-repo="${esc(r.repo)}" data-n="${pr.number}">Merge #${pr.number}</button>`;
  else if (awaiting) tail = `<span class="auto">auto ⤳</span>`;
  else tail = `<span class="dim">${esc(pr.stage)}</span>`;
  return `<span class="inflight">
    <span class="num">#${pr.number}</span>
    <span class="ttl">${esc(pr.title)}</span>
    <span class="ck ${ck.cls}" title="${ck.label}">${ck.g}</span>${more}
    ${tail}</span>`;
}
function countCell(n, kind){
  if (!n) return `<span class="dim">·</span>`;
  const glyph = kind === 'held' ? '⚠ ' : kind === 'parked' ? '⏸ ' : '';
  return `<span class="cnt-cell ${kind || ''}">${glyph}${n}</span>`;
}
function ratchetCell(rt){
  const rb = ratchetBar(rt || {});
  const loosen = rt && rt.ready_to_loosen;
  const w = Math.round(46 * rb.trustW / 100);
  const bar = `<svg class="rbar" width="46" height="8" viewBox="0 0 46 8" aria-hidden="true">
      <rect x="0" y="2" width="46" height="4" rx="2" fill="#21262d"/>
      <rect x="0" y="2" width="${w}" height="4" rx="2" fill="${rb.barC}"/></svg>`;
  const tail = loosen ? `<span class="loosen-pill">▲ loosen</span>` : `<span class="rnum">${rb.short}</span>`;
  return `<span class="rat" title="${rb.stat}">${bar}${tail}</span>`;
}
/* expand-in-place lane */
function lsec(label, count, inner){
  const c = count != null ? ` <span class="lcount">${count}</span>` : '';
  return `<div class="lsec"><div class="lhead">${label}${c}</div><div class="lbody">${inner}</div></div>`;
}
const lempty = t => `<div class="lempty">${t}</div>`;
function sessItem(s, now){
  return `<div class="litem">
    <div class="lrow1"><span class="pulse tool-${s.tool}"></span> <b>${esc(s.stage)}</b>
      <span class="tool-${s.tool}">${esc(s.tool)}</span> <span class="dim">${esc(s.model)}</span>
      <span class="dim">· ${elapsed(s.started_at, now)}</span></div>
    <div class="lmeta">#${s.number} ${esc(s.title)}</div>
    <div class="lmeta dim mono">${s.branch ? esc(s.branch) : '(branch pending)'} · pid ${s.pid}</div>
    <div class="lmeta dim mono">${esc(s.worktree)}</div>
  </div>`;
}
function flightItem(r, pr){
  const ck = checksMark(pr.checks);
  const awaiting = pr.stage === 'awaiting-merge';
  const human = awaiting && r.profile !== 'autonomous';
  const careful = r.profile === 'guarded' ? ' · <span class="caution">read before merging</span>' : '';
  let act;
  if (human) act = `<button class="btn" data-act="merge" data-repo="${esc(r.repo)}" data-n="${pr.number}">Merge #${pr.number}</button>`;
  else if (awaiting) act = `<span class="auto">auto-merges on green ⤳</span>`;
  else act = `<span class="dim">${esc(pr.stage)}…</span>`;
  return `<div class="litem">
    <div class="lrow1"><span class="num">#${pr.number}</span> ${esc(pr.title)}</div>
    <div class="lmeta"><span class="tool-${pr.builder}">${esc(pr.builder)}</span> built ·
      <span class="tool-${pr.reviewer || reviewerOf(pr.builder)}">${esc(pr.reviewer || reviewerOf(pr.builder))}</span> reviewed ·
      <span class="ck ${ck.cls}">${ck.g}</span> ${ck.label}${careful}</div>
    <div class="lacts">${act} <a class="lnk" data-noop role="button" tabindex="0">PR ↗</a></div>
  </div>`;
}
function readyItem(q){
  return `<div class="litem lrow1"><span class="num">#${q.number}</span> ${esc(q.title)}
    <span class="tag2">${esc(q.complexity)}</span><span class="tag2 alt">${esc(q.effort)} effort</span></div>`;
}
function heldItem(h){
  return `<div class="litem">
    <div class="lrow1"><span class="num">#${h.number}</span> ${esc(h.title)}
      <span class="badge ${h.state}">${esc(h.state)}</span></div>
    <div class="lmeta dim">${esc(h.reason)}</div>
    <div class="lacts"><button class="btn" data-act="pickup" data-n="${h.number}">Pickup #${h.number}</button>
      <span class="dim">held ${rel(h.since, STATE.now)}</span></div>
  </div>`;
}
function parkedItem(r, p){
  const rev = p.reviewer || reviewerOf(p.builder);
  return `<div class="litem">
    <div class="lrow1"><span class="num">#${p.number}</span> ${esc(p.title)}
      <span class="badge park">${esc(p.reason)}</span></div>
    <div class="lmeta dim"><span class="tool-${p.builder}">${esc(p.builder)}</span> built ·
      <span class="tool-${rev}">${esc(rev)}</span> reviewed · parked ${rel(p.since, STATE.now)}</div>
    <div class="lacts"><button class="btn ghost" data-act="resume" data-repo="${esc(r.repo)}" data-n="${p.number}">Resume #${p.number}</button>
      <a class="lnk" data-noop role="button" tabindex="0">PR ↗</a></div>
  </div>`;
}
function mergeItem(m, now){
  return `<div class="litem lrow1"><span class="ck ok">✓</span> <span class="num">#${m.number}</span> ${esc(m.title)}
    <span class="dim">· <span class="tool-${m.builder}">${esc(m.builder)}</span> · ${rel(m.merged_at, now)}</span></div>`;
}
function laneHTML(r, now){
  const sessions = r._running;
  const rb = ratchetBar(r.ratchet || {});
  const w = Math.round(160 * rb.trustW / 100);
  const loosen = r.ratchet && r.ratchet.ready_to_loosen;
  const ratCtl = loosen
    ? `<button class="btn ghost green" data-act="loosen" data-repo="${esc(r.repo)}">▲ Loosen dial</button>`
    : `<button class="btn ghost" data-act="tighten" data-repo="${esc(r.repo)}" ${(r.ratchet && r.ratchet.samples) ? '' : 'disabled'}>Adjust dial</button>`;
  const ratBlock = `<div class="ratlane">
      <svg width="160" height="8" viewBox="0 0 160 8" aria-hidden="true">
        <rect x="0" y="2" width="160" height="4" rx="2" fill="#21262d"/>
        <rect x="0" y="2" width="${w}" height="4" rx="2" fill="${rb.barC}"/></svg>
      <div class="lmeta dim">${rb.stat}</div>
      <div class="lacts">${ratCtl}</div>
    </div>`;
  const merges = (r.recent_merges || []).slice().sort((a, b) => Date.parse(b.merged_at) - Date.parse(a.merged_at));
  return `<div class="exlane">
    <div class="exlane-top"><b>${esc(r.repo)}</b>
      <span class="mini-badge ${r.profile}">${r.profile}</span>
      <a class="lnk" data-noop role="button" tabindex="0">open repo ↗</a></div>
    <div class="lgrid">
      ${lsec('running', sessions.length, sessions.length ? sessions.map(s => sessItem(s, now)).join('') : lempty('no live session'))}
      ${lsec('in-flight', (r.in_flight || []).length, (r.in_flight || []).length ? r.in_flight.map(pr => flightItem(r, pr)).join('') : lempty('nothing in flight'))}
      ${lsec('ready queue', (r.ready || []).length, (r.ready || []).length ? r.ready.map(readyItem).join('') : lempty('queue empty'))}
      ${lsec('held', (r.held || []).length, (r.held || []).length ? r.held.map(heldItem).join('') : lempty('none held'))}
      ${lsec('parked', (r.parked || []).length, (r.parked || []).length ? r.parked.map(p => parkedItem(r, p)).join('') : lempty('none parked'))}
      ${lsec('recent merges', merges.length, merges.length ? merges.map(m => mergeItem(m, now)).join('') : lempty('none yet'))}
      ${lsec('trust ratchet', null, ratBlock)}
    </div>
  </div>`;
}
function fleetRow(r, i, now){
  const exp = STATE.fleet.expanded.has(r.repo);
  const needs = r._needs.length > 0;
  const ndot = needs
    ? `<span class="ndot on" title="needs you: ${r._needs.map(n => n.kind).join(', ')}">●</span>`
    : `<span class="ndot" aria-hidden="true">·</span>`;
  const data = `<tr class="drow ${needs ? 'needs' : ''} ${exp ? 'exp' : ''}" data-view="fleet" data-i="${i}" data-repo="${esc(r.repo)}" aria-expanded="${exp}">
    <td class="c-caret"><button class="caret ${exp ? 'open' : ''}" aria-label="${exp ? 'collapse' : 'expand'} ${short(r.repo)}">▸</button></td>
    <th scope="row" class="c-repo">${ndot}<span class="rname">${esc(short(r.repo))}</span></th>
    <td class="c-prof"><span class="mini-badge ${r.profile}">${r.profile}</span></td>
    <td class="c-run">${runningCell(r._running, now)}</td>
    <td class="c-num">${countCell((r.ready || []).length)}</td>
    <td class="c-flight">${inflightCell(r)}</td>
    <td class="c-num">${countCell((r.held || []).length, 'held')}</td>
    <td class="c-num">${countCell((r.parked || []).length, 'parked')}</td>
    <td class="c-rat">${ratchetCell(r.ratchet)}</td>
  </tr>`;
  const lane = exp ? `<tr class="lane-row"><td colspan="${FLEET_COLS.length}">${laneHTML(r, now)}</td></tr>` : '';
  return data + lane;
}
function renderFleet(snap, now){
  // annotate rows with derived running + needs
  for (const r of snap.repos){
    r._running = (snap.running || []).filter(s => s.repo === r.repo);
    r._needs = repoNeeds(r);
  }
  const needsCount = snap.repos.filter(r => r._needs.length).length;
  const total = snap.repos.length;

  let rows = snap.repos;
  if (STATE.fleet.needsOnly) rows = rows.filter(r => r._needs.length);
  rows = fleetSort(rows);
  STATE.rows.fleet = rows;                          // display order for j/k + Enter

  const el = document.getElementById('view-fleet');
  const f = STATE.fleet;
  const toolbar = `<div class="fleet-toolbar">
    <button id="filterToggle" class="${f.needsOnly ? 'on' : ''}" aria-pressed="${f.needsOnly}">
      <span class="fbox">${f.needsOnly ? '☑' : '☐'}</span> only rows that need me
      <span class="fcount">${needsCount}</span></button>
    <span id="rowNote">${f.needsOnly ? `showing ${rows.length} of ${total} repos` : `${total} repos · ${needsCount} need you`}</span>
    <span class="sorthint">· click a column to sort</span></div>`;

  const thead = '<tr>' + FLEET_COLS.map(c => {
    if (!c.key) return `<th class="${c.cls}"></th>`;
    const active = f.sort.key === c.key;
    const car = active ? (f.sort.dir === 'asc' ? ' ▲' : ' ▼') : '';
    return `<th class="${c.cls} sortable ${active ? 'sorted' : ''}" data-sort="${c.key}"
      title="${c.hint || ('sort by ' + c.label)}" aria-sort="${active ? (f.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}">${c.label}<span class="scar">${car}</span></th>`;
  }).join('') + '</tr>';

  let body;
  if (!rows.length){
    body = `<tr><td colspan="${FLEET_COLS.length}"><div class="allclear">
      <span class="ck ok big">✓</span>
      <div><b>Nothing needs you.</b> Every repo is running itself — no merge waiting,
      nothing held or parked, no dial ready to loosen. Toggle the filter off to see the full fleet.</div>
    </div></td></tr>`;
  } else {
    body = rows.map((r, i) => fleetRow(r, i, now)).join('');
  }
  el.innerHTML = `<div class="viewhead"><h2>Fleet</h2><span class="cnt">${total}</span>
      <span class="sub">repo × signals · expand a row for its lane</span></div>
    ${toolbar}
    <div class="fleet-tablewrap"><table class="grid"><thead>${thead}</thead><tbody>${body}</tbody></table></div>`;
}

/* ============================================================================
   HISTORY view (console): flat reverse-chron merged/audit feed.
   ========================================================================== */
function renderHistory(snap, nowMs){
  const merges = [];
  for (const r of snap.repos) for (const m of (r.recent_merges || []))
    merges.push({...m, repo:r.repo, profile:r.profile});
  merges.sort((a, b) => Date.parse(b.merged_at) - Date.parse(a.merged_at));
  STATE.rows.history = merges;
  const el = document.getElementById('view-history');
  const head = `<div class="viewhead"><h2>History</h2><span class="cnt">${merges.length}</span>
    <span class="sub">merged &amp; auto-merged · newest first · audit trail</span></div>`;
  if (!merges.length){
    el.innerHTML = head + `<div class="zero idle"><h3>No merges yet</h3>
      <p>When work ships, it lands here as an audit trail.</p></div>`;
    return;
  }
  const rows = merges.map((m, i) => {
    const rev = m.builder ? reviewerOf(m.builder) : null;
    const auto = m.profile === 'autonomous';
    return `<tr class="hrow" data-view="history" data-i="${i}">
      <td style="width:22px"><span class="chk">✓</span></td>
      <td style="width:52px"><span class="num">#${m.number}</span></td>
      <td class="htitle">${esc(m.title)}</td>
      <td style="width:150px" class="muted">${m.builder
        ? `<span class="tool-${m.builder}">${m.builder}</span> <span class="arrow">→</span> <span class="tool-${rev}">${rev}</span>`
        : ''}</td>
      <td style="width:120px" class="muted">${esc(short(m.repo))}${auto ? ' · <span class="chk">auto</span>' : ''}</td>
      <td class="ago">${rel(m.merged_at, nowMs)}</td>
    </tr>`;
  }).join('');
  el.innerHTML = head + `<div class="list" style="border:1px solid var(--border);border-radius:9px;background:var(--panel);padding:2px 8px">
    <table class="hist"><tbody>${rows}</tbody></table></div>`;
}

/* ============================================================================
   Shared drill-down drawer (rule 5) — opens from any row on any tab.
   ========================================================================== */
function openDrill(view, i){
  const it = STATE.rows[view][i];
  if (!it) return;
  const d = detailFor(view, it);
  const drawer = document.getElementById('drawer');
  drawer.innerHTML = `
    <div class="dhead">
      <div><div class="dkicker">${d.kicker}</div><div class="dtitle">${d.title}</div></div>
      <button class="x" id="drill-x" aria-label="Close">✕</button>
    </div>
    <div class="dbody">${d.body}</div>
    <div class="dfoot">${d.foot}</div>`;
  drawer.classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
  document.getElementById('scrim').classList.add('open');
  document.getElementById('drill-x').addEventListener('click', closeDrill);
  drawer.querySelectorAll('[data-act]').forEach(b =>
    b.addEventListener('click', () => doAction(b.dataset.act)));
}
function closeDrill(){
  const drawer = document.getElementById('drawer');
  drawer.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true');
  document.getElementById('scrim').classList.remove('open');
}
function drow(lbl, val){ return `<div class="ddrow"><div class="lbl">${lbl}</div><div class="val">${val}</div></div>`; }
function extLink(txt){ return `<a class="open-ext" href="#" onclick="return false">${txt} ↗</a>`; }

/* Illustrative review findings — the v2 snapshot doesn't carry findings yet, so the
   drill-down synthesizes a plausible set (marked "illustrative"). Real
   checks/builder/reviewer/profile come straight from the snapshot. */
function mockFindings(it){
  const n = it.number || 0;
  if (it.checks === 'failing') return [
    {sev:'block', g:'✕', t:`CI failing — ${['type check','integration test','lint'][n % 3]} did not pass`},
    {sev:'nit',   g:'·', t:'reviewer deferred until CI is green'}
  ];
  const nits = (n % 3);
  const out = [{sev:'pass', g:'✓', t:'no blocking findings — cross-tool review clean'}];
  for (let k = 0; k < nits; k++) out.push({sev:'nit', g:'·',
    t:['naming: prefer the domain term from CONTEXT.md','add a test through the public interface','tighten the PR body wording'][k]});
  return out;
}
function findingsBlock(it){
  const fs = mockFindings(it);
  return `<div class="dsection"><h4>Review <span class="il">illustrative — not in snapshot</span></h4>` +
    fs.map(f => `<div class="finding ${f.sev}"><span class="g">${f.g}</span><span>${esc(f.t)}</span></div>`).join('') +
    `</div>`;
}
function detailFor(view, it){
  if (view === 'live'){
    return {
      kicker:`${it.stage} · live session`,
      title:`<span class="num">#${it.number}</span> ${esc(it.title)}`,
      body:
        drow('repo', esc(it.repo)) +
        drow('stage', `<span class="stage ${it.stage}"><span class="pulse"></span>${esc(it.stage)}</span>`) +
        drow('runner', `<span class="badge ${it.tool}">${it.tool}</span><span class="model">${esc(it.model)}</span>`) +
        drow('elapsed', `<span class="elapsed" style="font-size:15px">${elapsed(it.started_at, STATE.now)}</span> <span class="muted">· started ${rel(it.started_at, STATE.now)}</span>`) +
        drow('pid', String(it.pid)) +
        `<div class="dsection"><h4>Worktree</h4>
          <div class="worktree">branch <span class="branchmono">${it.branch ? esc(it.branch) : '— (pending triage)'}</span><br>${esc(it.worktree)}</div></div>`,
      foot:`<button class="act ghost" data-act="Followed session logs">View logs</button>
            <button class="act reviewed" data-act="Opened worktree">Open worktree</button>
            ${extLink('open branch on GitHub')}`
    };
  }
  if (view === 'history'){
    const rev = it.builder ? reviewerOf(it.builder) : null;
    return {
      kicker:`merged · ${esc(short(it.repo))}`,
      title:`<span class="num">#${it.number}</span> ${esc(it.title)}`,
      body:
        drow('repo', `${esc(it.repo)} <span class="mini-badge ${it.profile}">${it.profile}</span>`) +
        drow('built by', it.builder ? `<span class="tool-${it.builder}">${it.builder}</span> <span class="arrow">→</span> reviewed by <span class="tool-${rev}">${rev}</span>` : '—') +
        drow('merged', `${rel(it.merged_at, STATE.now)} <span class="muted">· ${esc(it.merged_at)}</span>`) +
        (it.profile === 'autonomous' ? drow('mode', `<span class="chk">auto-merged</span> on green CI + clean review`) : ''),
      foot:`${extLink('open PR on GitHub')}`
    };
  }
  if (view === 'fleet'){
    const rb = ratchetBar(it.ratchet);
    const loosen = it.ratchet && it.ratchet.ready_to_loosen;
    const listOf = arr => (arr || []).length
      ? arr.map(x => `<div class="dmeta" style="color:var(--fg)"><span class="num">#${x.number}</span> ${esc(x.title)}</div>`).join('')
      : `<span class="muted">none</span>`;
    return {
      kicker:`repo · ${it.profile}`,
      title:esc(it.repo),
      body:
        drow('profile', `<span class="mini-badge ${it.profile}">${it.profile}</span>`) +
        drow('ratchet', `<div class="rat"><span class="rbar"><svg width="80" height="8" viewBox="0 0 80 8"><rect x="0" y="2" width="80" height="4" rx="2" fill="#21262d"/><rect x="0" y="2" width="${Math.round(80*rb.barW/100)}" height="4" rx="2" fill="${rb.barC}"/></svg></span> <span class="rnum">${rb.stat}</span></div>`) +
        `<div class="dsection"><h4>Ready (${(it.ready||[]).length})</h4>${listOf(it.ready)}</div>` +
        `<div class="dsection"><h4>In flight (${(it.in_flight||[]).length})</h4>${listOf(it.in_flight)}</div>` +
        `<div class="dsection"><h4>Held (${(it.held||[]).length})</h4>${listOf(it.held)}</div>` +
        `<div class="dsection"><h4>Parked (${(it.parked||[]).length})</h4>${listOf(it.parked)}</div>`,
      foot: loosen
        ? `<button class="act loosen" data-act="Loosened the dial on ${esc(short(it.repo))}">Loosen dial</button><button class="act ghost" data-act="Tightened the dial">Tighten</button>${extLink('open repo')}`
        : `<button class="act ghost" data-act="Tightened the dial on ${esc(short(it.repo))}">Tighten dial</button>${extLink('open repo')}`
    };
  }
  /* ---- inbox items (kind-based) ---- */
  if (it.kind === 'merge'){
    const care = it.profile === 'guarded'
      ? `<span class="caution">guarded — read the diff before merging</span>`
      : `reviewed — a quick glance, then merge`;
    return {
      kicker:`${it.profile} · awaiting your merge`,
      title:`<span class="num">#${it.number}</span> ${esc(it.title)}`,
      body:
        drow('repo', `${esc(it.repo)} <span class="mini-badge ${it.profile}">${it.profile}</span>`) +
        drow('built by', `<span class="tool-${it.builder}">${it.builder}</span> <span class="arrow">→</span> reviewed by <span class="tool-${it.reviewer}">${it.reviewer}</span>`) +
        `<div class="dsection"><h4>Checks</h4><div class="checkline">${CHECK_GLYPH[it.checks] || ''}</div><div class="dmeta">${esc(care)}</div></div>` +
        findingsBlock(it),
      foot:`<button class="act ${it.profile}" data-act="Merged #${it.number}">Merge #${it.number}</button>${extLink('open PR')}`
    };
  }
  if (it.kind === 'held'){
    const a = HELD[it.state] || {act:'Pickup'};
    return {
      kicker:`held · ${esc(it.state)}`,
      title:`<span class="num">#${it.number}</span> ${esc(it.title)}`,
      body:
        drow('repo', `${esc(it.repo)} <span class="mini-badge ${it.profile}">${it.profile}</span>`) +
        drow('held', `${rel(it.since, STATE.now)}`) +
        `<div class="dsection"><h4>Why it's held</h4><div class="finding warn"><span class="g">◷</span><span>${esc(it.reason)}</span></div>
          <div class="dmeta">No builder touches a held issue. Reply in a comment to auto-advance it, or pick it up to drive it now.</div></div>`,
      foot:`<button class="act held" data-act="Picked up #${it.number}">${esc(a.act)}</button>${extLink('open issue')}`
    };
  }
  if (it.kind === 'parked'){
    const a = PARKED[it.reason] || {act:'Open', why:it.reason};
    return {
      kicker:`parked · ${esc(it.reason)}`,
      title:`<span class="num">#${it.number}</span> ${esc(it.title)}`,
      body:
        drow('repo', `${esc(it.repo)} <span class="mini-badge ${it.profile}">${it.profile}</span>`) +
        drow('built by', `<span class="tool-${it.builder}">${it.builder}</span> <span class="arrow">→</span> reviewed by <span class="tool-${it.reviewer}">${it.reviewer}</span>`) +
        drow('parked', `${rel(it.since, STATE.now)}`) +
        `<div class="dsection"><h4>Why it's parked</h4><div class="finding block"><span class="g">⚠</span><span>${esc(a.why)}</span></div>
          <div class="dmeta">Autonomy parks doubt rather than forcing a merge — this is the escape valve, waiting on you.</div></div>`,
      foot:`<button class="act parked" data-act="Actioned #${it.number}">${esc(a.act)}</button>${extLink('open PR')}`
    };
  }
  // loosen
  const rate = Math.round((it.rate || 0) * 100);
  return {
    kicker:`ratchet · ready to loosen`,
    title:esc(short(it.repo)),
    body:
      drow('repo', esc(it.repo)) +
      drow('samples', `${it.samples} clean staged decisions`) +
      drow('corrected', `${rate}%`) +
      `<div class="dsection"><h4>Graduated autonomy</h4>
        <div class="finding pass"><span class="g">▲</span><span>Staged decisions have been consistently confirmed without correction.</span></div>
        <div class="dmeta">Loosening moves this repo one step toward autonomy. Deliberate, per-repo, and reversible.</div></div>`,
    foot:`<button class="act loosen" data-act="Loosened the dial on ${esc(short(it.repo))}">Loosen dial</button><button class="act ghost" data-act="Kept the dial where it is">Not yet</button>${extLink('open repo')}`
  };
}

/* ============================================================================
   Header / status chrome
   ========================================================================== */
function renderChrome(snap){
  const d = snap.daemon || {enabled:true};
  const on = d.enabled;
  const cyc = d.last_cycle_at ? rel(d.last_cycle_at, STATE.now) : '—';
  const poll = d.poll_seconds ? `${Math.round(d.poll_seconds/60)}m poll` : '';
  document.getElementById('daemon').outerHTML =
    `<span id="daemon" class="daemon ${on ? 'on' : 'off'}"><span class="dot"></span>
      daemon ${on ? 'running' : 'paused'}
      <span class="cyc">· cycle ${cyc} · ${poll}</span>
      <button class="toggle" id="daemonToggle">${on ? 'Pause' : 'Resume'}</button></span>`;
  document.getElementById('daemonToggle').addEventListener('click', e => {
    e.stopPropagation();
    STATE.daemonEnabled = !STATE.daemonEnabled;
    toast(STATE.daemonEnabled ? 'Daemon resumed (mock)' : 'Daemon paused (mock)');
    render();
  });
  // header pool chips — the single home for two-pool headroom (rule 3)
  document.getElementById('poolchips').innerHTML = (snap.pools || []).map(p => {
    const h = pct(p.headroom_pct);
    return `<span class="pchip"><span class="nm tool-${p.tool}">${p.tool}</span>
      <span class="mini"><i style="width:${h}%;background:${headroomColor(h)}"></i></span>
      <span class="hr">${h}%</span></span>`;
  }).join('');
  // status bar
  document.getElementById('sb-daemon').textContent = on ? '● daemon on' : '○ daemon paused';
  document.getElementById('sb-daemon').style.color = on ? 'var(--green)' : 'var(--amber)';
  const fresh = d.gh_fresh_at ? rel(d.gh_fresh_at, STATE.now) : '—';
  document.getElementById('sb-updated').textContent = `gh fresh ${fresh}`;
}
function updateCounts(snap){
  const inbox = deriveInbox(snap).length;
  const live = (snap.running || []).length;
  const fleet = snap.repos.length;
  let hist = 0; for (const r of snap.repos) hist += (r.recent_merges || []).length;
  const set = (id, v, alert) => {
    const el = document.getElementById('cnt-' + id);
    el.textContent = v;
    el.classList.toggle('alert', !!alert);
  };
  set('inbox', inbox, inbox > 0);
  set('live', live);
  set('fleet', fleet);
  set('history', hist);
}

/* ============================================================================
   Router + keyboard + driver
   ========================================================================== */
const STATE = {
  view:'inbox', selected:{inbox:-1, live:-1, fleet:-1, history:-1},
  rows:{inbox:[], live:[], fleet:[], history:[]},
  now:Date.now(), t0:Date.now(), anchor:Date.now(),
  mode:'typical', daemonEnabled:true, capture:null,
  fleet:{needsOnly:false, sort:{key:'needs', dir:'desc'}, expanded:new Set()},
};
/* per-view row selector for j/k highlight */
const ROW_SEL = {inbox:'.qrow', live:'.sess', fleet:'tr.drow', history:'tr.hrow'};
const liveNow = () => STATE.anchor + (Date.now() - STATE.t0);

function switchView(view){
  STATE.view = view;
  document.querySelectorAll('.tab').forEach(t => {
    const on = t.dataset.view === view;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');   // keep tab semantics in sync
  });
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.dataset.view === view));
  document.getElementById('sb-view').textContent = view;
  closeDrill();
  applySelection();
}
function applySelection(){
  const view = STATE.view, i = STATE.selected[view];
  document.querySelectorAll(`#view-${view} ${ROW_SEL[view]}`)
    .forEach(r => r.classList.toggle('sel', Number(r.dataset.i) === i));
  if (i >= 0){
    const el = document.querySelector(`#view-${view} ${ROW_SEL[view]}.sel`);
    if (el && el.scrollIntoView) el.scrollIntoView({block:'nearest'});
  }
}
function moveSel(delta){
  const view = STATE.view, n = STATE.rows[view].length;
  if (!n) return;
  let i = STATE.selected[view];
  i = i < 0 ? (delta > 0 ? 0 : n - 1) : Math.max(0, Math.min(n - 1, i + delta));
  STATE.selected[view] = i;
  applySelection();
}
function toast(msg){
  const t = document.getElementById('toast');
  t.innerHTML = `<span class="g">✓</span>${esc(msg)}`;
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 1900);
}
function doAction(label){ toast(label + ' (mock — no-op)'); closeDrill(); }
/* friendly toast label for an action button (fleet verbs carry data-act/-n/-repo;
   inbox qact buttons have an empty data-act and fall back to their text) */
function actionLabel(el){
  const a = el.dataset.act, n = el.dataset.n, repo = el.dataset.repo;
  const where = repo ? ' · ' + short(repo) : '';
  switch(a){
    case 'merge':   return `Merge #${n}${where}`;
    case 'pickup':  return `Pickup #${n}`;
    case 'resume':  return `Resume #${n}${where}`;
    case 'loosen':  return `Loosen dial${where}`;
    case 'tighten': return `Adjust dial${where}`;
  }
  return a || el.textContent.trim();
}

/* fleet expand-in-place */
function toggleExpand(repo){
  const e = STATE.fleet.expanded;
  if (e.has(repo)) e.delete(repo); else e.add(repo);
  render();
}

/* advance elapsed labels without a full re-render (keeps expand/selection state) */
function tick(){
  const n = liveNow();
  STATE.now = n;
  document.querySelectorAll('#view-live .sess-elapsed[data-started]').forEach(el => {
    el.textContent = elapsed(el.dataset.started, n);
  });
  document.querySelectorAll('#view-inbox [data-since]').forEach(el => {
    el.textContent = elapsed(el.dataset.since, n);
  });
}

function wireEvents(){
  document.getElementById('tabs').addEventListener('click', e => {
    const t = e.target.closest('.tab'); if (t) switchView(t.dataset.view);
  });
  // MOCK-ONLY state toggle
  document.getElementById('stateToggle').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    STATE.mode = b.dataset.state;
    [...e.currentTarget.children].forEach(c => c.classList.toggle('on', c === b));
    STATE.selected = {inbox:-1, live:-1, fleet:-1, history:-1};
    STATE.fleet.expanded.clear();
    if (STATE.mode === 'typical') STATE.fleet.expanded.add('ConnorGriffin/ciq-autotune');
    STATE.fleet.needsOnly = (STATE.mode === 'dense');
    render();
  });
  // event-delegated clicks across every view
  document.querySelector('main').addEventListener('click', e => {
    // action buttons → toast (drawer wires its own foot buttons separately)
    const act = e.target.closest('[data-act]');
    if (act){
      if (act.closest('.drawer')) return;
      e.stopPropagation();
      doAction(actionLabel(act));
      return;
    }
    // no-op links (open PR/issue ↗ etc.)
    const noop = e.target.closest('[data-noop]');
    if (noop){ e.preventDefault(); return; }
    // fleet: sort by column header
    const th = e.target.closest('th[data-sort]');
    if (th){
      const key = th.dataset.sort, s = STATE.fleet.sort;
      if (s.key === key) s.dir = s.dir === 'asc' ? 'desc' : 'asc';
      else { s.key = key; s.dir = (key === 'repo' || key === 'profile') ? 'asc' : 'desc'; }
      render();
      return;
    }
    // fleet: needs-only filter
    const filt = e.target.closest('#filterToggle');
    if (filt){ STATE.fleet.needsOnly = !STATE.fleet.needsOnly; render(); return; }
    // fleet: caret → expand the repo's lane in place
    const caret = e.target.closest('.caret');
    if (caret){ e.stopPropagation(); toggleExpand(caret.closest('tr').dataset.repo); return; }
    // any row → open the shared drawer (rule 5)
    const row = e.target.closest('[data-view][data-i]');
    if (row) openDrill(row.dataset.view, Number(row.dataset.i));
  });
  // keyboard: 1-4 views, j/k move, Enter open, Esc close
  document.addEventListener('keydown', e => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'Escape'){ closeDrill(); return; }
    const map = {'1':'inbox','2':'live','3':'fleet','4':'history'};
    if (map[e.key]){ switchView(map[e.key]); return; }
    if (e.key === 'j'){ e.preventDefault(); moveSel(1); return; }
    if (e.key === 'k'){ e.preventDefault(); moveSel(-1); return; }
    if (e.key === 'Enter'){
      const i = STATE.selected[STATE.view];
      if (i >= 0) openDrill(STATE.view, i);
    }
  });
  document.getElementById('scrim').addEventListener('click', closeDrill);
}

/* pick the active snapshot per MOCK state toggle, apply the daemon toggle, and
   recompute pools[].running from running[] so the Live correlation stays honest. */
function activeSnap(){
  let s = STATE.mode === 'quiet' ? quietize(STATE.capture)
        : STATE.mode === 'dense' ? densify(STATE.capture)
        : structuredClone(STATE.capture);
  s.daemon = {...s.daemon, enabled:STATE.daemonEnabled};
  for (const p of (s.pools || []))
    p.running = (s.running || []).filter(r => r.tool === p.tool).length;
  return s;
}
function render(){
  const snap = activeSnap();
  STATE.anchor = anchorNow(snap);
  STATE.t0 = Date.now();
  STATE.now = STATE.anchor;
  renderChrome(snap);
  updateCounts(snap);
  renderInbox(snap, STATE.now);
  renderLive(snap, STATE.now);
  renderFleet(snap, STATE.now);
  renderHistory(snap, STATE.now);
  applySelection();
}

async function boot(){
  try { STATE.capture = await (await fetch(CAPTURE_URL, {cache:'no-store'})).json(); }
  catch(e){ STATE.capture = FALLBACK; }   // MOCK-ONLY: file:// fetch of sibling JSON is CORS-blocked
  STATE.daemonEnabled = !!(STATE.capture.daemon && STATE.capture.daemon.enabled);
  STATE.fleet.expanded.add('ConnorGriffin/ciq-autotune');   // typical: one row drilled-in
  wireEvents();
  render();
  setInterval(tick, 1000);   // advance live elapsed between (real-app) polls
}

/* ============================================================================
   MOCK-ONLY FALLBACK — inline mirror of dashboard-v2.capture.json so this file
   renders when opened directly over file:// (where fetch of a sibling JSON is
   CORS-blocked). The real app fetches live from /api/snapshot; this is review-only.
   ========================================================================== */
const FALLBACK = {
  daemon:{enabled:true, last_cycle_at:'2026-07-13T14:02:11Z', poll_seconds:300, gh_fresh_at:'2026-07-13T14:03:58Z'},
  pools:[
    {tool:'claude', clear:false, spent_pct:71.0, headroom_pct:29.0, running:4},
    {tool:'codex',  clear:false, spent_pct:38.0, headroom_pct:62.0, running:3}
  ],
  running:[
    {repo:'ConnorGriffin/agentflow', number:63, title:'Re-platform the operator dashboard as a Svelte control plane',
     stage:'building', tool:'claude', model:'opus', branch:'agentflow/claude/issue-63-re-platform-dashboard',
     worktree:'~/Code/ConnorGriffin/worktrees/agentflow-63', pid:48213, started_at:'2026-07-13T13:47:02Z'},
    {repo:'ConnorGriffin/homelab', number:43, title:'Pin Grafana to 11.1 and add a readiness probe',
     stage:'building', tool:'codex', model:'sol', branch:'agentflow/codex/issue-43-grafana-pin',
     worktree:'~/Code/ConnorGriffin/worktrees/homelab-43', pid:48301, started_at:'2026-07-13T13:57:40Z'},
    {repo:'ConnorGriffin/dotfiles', number:24, title:'Add a git-worktree cleanup script to scripts/',
     stage:'building', tool:'claude', model:'sonnet', branch:'agentflow/claude/issue-24-worktree-cleanup',
     worktree:'~/Code/ConnorGriffin/worktrees/dotfiles-24', pid:48355, started_at:'2026-07-13T13:59:11Z'},
    {repo:'ConnorGriffin/agentflow-sandbox', number:12, title:'Add wrap(text, width) helper to sandboxlib',
     stage:'reviewing', tool:'codex', model:'sol', branch:'agentflow/claude/issue-12-wrap-helper',
     worktree:'~/Code/ConnorGriffin/worktrees/agentflow-sandbox-12', pid:48377, started_at:'2026-07-13T14:01:40Z'},
    {repo:'ConnorGriffin/home-depot-location-probe', number:8, title:'Retry store-locator on 429 with backoff',
     stage:'reviewing', tool:'claude', model:'sonnet', branch:'agentflow/codex/issue-8-locator-retry',
     worktree:'~/Code/ConnorGriffin/worktrees/home-depot-location-probe-8', pid:48390, started_at:'2026-07-13T14:00:05Z'},
    {repo:'ConnorGriffin/ciq-autotune', number:314, title:'Grounding pull for basal harm-layer thresholds',
     stage:'triaging', tool:'codex', model:'sol', branch:null,
     worktree:'~/Code/ConnorGriffin/worktrees/ciq-autotune-314', pid:48410, started_at:'2026-07-13T14:03:20Z'},
    {repo:'ConnorGriffin/agentflow', number:66, title:'Daemon: write live-session state file for the dashboard',
     stage:'triaging', tool:'claude', model:'opus', branch:null,
     worktree:'~/Code/ConnorGriffin/worktrees/agentflow-66', pid:48441, started_at:'2026-07-13T14:02:52Z'}
  ],
  repos:[
    {repo:'ConnorGriffin/agentflow-sandbox', profile:'autonomous',
     ready:[{number:15, title:'Add center(text, width) helper', complexity:'standard', effort:'low'}],
     held:[], in_flight:[{number:12, title:'Add wrap(text, width) helper to sandboxlib', builder:'claude', reviewer:'codex', stage:'reviewing', checks:'passing'}],
     parked:[],
     recent_merges:[
       {number:11, title:'Add pad(text, width) helper', builder:'codex', merged_at:'2026-07-13T12:40:00Z'},
       {number:9,  title:'Add slugify(text) helper', builder:'claude', merged_at:'2026-07-12T23:29:42Z'}],
     ratchet:{repo:'ConnorGriffin/agentflow-sandbox', samples:18, correction_rate:0.06, ready_to_loosen:true}},
    {repo:'ConnorGriffin/home-depot-location-probe', profile:'autonomous',
     ready:[],
     held:[{number:6, title:'Show nearest-store distance in miles or km?', state:'needs-grilling', reason:'unit convention undecidable from repo — intent gap', since:'2026-07-12T18:10:00Z'}],
     in_flight:[{number:8, title:'Retry store-locator on 429 with backoff', builder:'codex', reviewer:'claude', stage:'reviewing', checks:'passing'}],
     parked:[],
     recent_merges:[{number:5, title:'Cache geocode lookups for 24h', builder:'codex', merged_at:'2026-07-13T09:12:00Z'}],
     ratchet:{repo:'ConnorGriffin/home-depot-location-probe', samples:9, correction_rate:0.11, ready_to_loosen:false}},
    {repo:'ConnorGriffin/ciq-autotune', profile:'guarded',
     ready:[],
     held:[{number:309, title:'Patterns tab: confidence badge on rejected findings', state:'needs-mockup', reason:'user-facing surface — needs a locked mockup', since:'2026-07-11T16:44:00Z'}],
     in_flight:[{number:305, title:'ISF harm-layer arm: dominant-vs-nearest IOB', builder:'claude', reviewer:'codex', stage:'awaiting-merge', checks:'passing'}],
     parked:[{number:302, title:'Basal fold: clamp negative deltas at zero', reason:'open-question', builder:'claude', reviewer:'codex', since:'2026-07-13T08:22:00Z'}],
     recent_merges:[{number:298, title:'Patterns tab: reject a finding', builder:'claude', merged_at:'2026-07-12T18:05:00Z'}],
     ratchet:{repo:'ConnorGriffin/ciq-autotune', samples:0, correction_rate:0.0, ready_to_loosen:false}},
    {repo:'ConnorGriffin/agentflow', profile:'reviewed',
     ready:[{number:67, title:'Cache gh queries in server with a 15s TTL', complexity:'standard', effort:'medium'}],
     held:[],
     in_flight:[{number:63, title:'Re-platform the operator dashboard as a Svelte control plane', builder:'claude', reviewer:'codex', stage:'building', checks:'pending'}],
     parked:[{number:58, title:'Post mockup screenshots with URLs that load on private repos', reason:'failed-merge', builder:'codex', reviewer:'claude', since:'2026-07-13T11:05:00Z'}],
     recent_merges:[
       {number:60, title:'Post mockup screenshots with URLs that load on private repos', builder:'codex', merged_at:'2026-07-13T10:58:00Z'},
       {number:59, title:'Cover child session PID marker', builder:'claude', merged_at:'2026-07-13T09:41:00Z'}],
     ratchet:{repo:'ConnorGriffin/agentflow', samples:11, correction_rate:0.18, ready_to_loosen:false}},
    {repo:'ConnorGriffin/homelab', profile:'reviewed',
     ready:[], held:[],
     in_flight:[
       {number:41, title:'Pin Traefik to 3.1 and add health check', builder:'codex', reviewer:'claude', stage:'awaiting-merge', checks:'passing'},
       {number:43, title:'Pin Grafana to 11.1 and add a readiness probe', builder:'codex', reviewer:'claude', stage:'building', checks:'pending'}],
     parked:[{number:39, title:'Move Pi-hole to a dedicated tailnet subnet', reason:'drop-to-reviewed', builder:'claude', reviewer:'codex', since:'2026-07-13T07:30:00Z'}],
     recent_merges:[{number:37, title:'Rotate the Grafana admin secret via sops', builder:'codex', merged_at:'2026-07-12T20:15:00Z'}],
     ratchet:{repo:'ConnorGriffin/homelab', samples:7, correction_rate:0.14, ready_to_loosen:false}},
    {repo:'ConnorGriffin/dotfiles', profile:'reviewed',
     ready:[], held:[],
     in_flight:[{number:24, title:'Add a git-worktree cleanup script to scripts/', builder:'claude', reviewer:'codex', stage:'building', checks:'pending'}],
     parked:[],
     recent_merges:[{number:22, title:'install.sh: skip-and-warn on existing symlinks', builder:'claude', merged_at:'2026-07-12T14:03:00Z'}],
     ratchet:{repo:'ConnorGriffin/dotfiles', samples:4, correction_rate:0.0, ready_to_loosen:false}}
  ]
};

boot();
