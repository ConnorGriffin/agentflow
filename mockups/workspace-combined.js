/* workspace-combined.js — LOCKED visual spec for the agentflow project
 * workspace (Wayfinder #128), locked 2026-07-16. Shelf topology (three visual
 * weights) + anchored Ask composer + queue prioritization, honest to ADR 0034.
 * Written against real workspace.capture.json fields only, so it lifts toward
 * the Svelte app. Build to this file; don't re-derive it.
 *
 * Binds ONLY to fields present in workspace.capture.json (the ADR 0033
 * projection, extended for ADR 0034): projects[].conversations (turn state /
 * skill / priority / started_at / can_prioritize / note; last_turns with
 * skill + duration_s) / proposals (versions, content_hash,
 * publication.receipts) / decision_maps (tickets with state/blocked_by) /
 * milestones / build_issues / landed (evidence, provenance), plus top-level
 * daemon / pools / workspace.
 *
 * Queue instinct: one urgency ordering governs the shelf —
 *   staged approval > operator-claimed frontier > next unblocked > background.
 * The dock is only the Ask composer; prioritization lives in the shelf ordering.
 *
 * MOCK-ONLY associations: the capture carries no proposal→effort or
 * conversation→effort edges yet, so (a) the staged build-issue proposal is
 * shelved with the proposed milestone whose loop it starts (ms-04), and
 * (b) background conversation turns are surfaced under the effort whose
 * ticket their title matches (conv-022 ↔ map #123 ticket #128, conv-021 ↔
 * map #132 ticket #136).
 */
'use strict';

(function () {
  const app = document.getElementById('app');
  const projectSelect = document.getElementById('project-select');
  const themeToggle = document.getElementById('theme-toggle');
  const dockEl = document.getElementById('dock');
  const hintEl = document.getElementById('dock-hint');
  const sendBtn = document.getElementById('dock-send');
  const composer = document.getElementById('composer');

  let data = null;
  let NOW = Date.now();
  let programmatic = false; // true while we set location.hash ourselves (vs Back/Forward)

  const state = {
    projectId: null,
    view: { name: 'home', param: null },
    /* approval flow: staged → armed → publishing → verified | discarded */
    approval: 'staged',
    /* dialogue: null | { mode:'existing', convId } | { mode:'new', scope, turns:[] } */
    convo: null,
    parkedNew: null,
    mockTurns: {},     // convId -> operator turns typed in the mock
    prioritized: {},   // convId -> true (MOCK: flips the queued note)
    published: {}      // proposalId -> true once its publication verified (MOCK)
  };

  /* Screen-reader status line for async outcomes (publish → verify). A persistent
     aria-live region announces even though views re-render wholesale. */
  function announce(msg) {
    const el = document.getElementById('live-status');
    if (el) el.textContent = msg;
  }

  /* MOCK-ONLY conversation→effort association (see file header). */
  const CONV_EFFORT = {
    'conv-022': { map: 123, ticket: 128 },
    'conv-021': { map: 132, ticket: 136 }
  };

  /* ---------- helpers ---------- */

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  function ago(iso) {
    const mins = Math.max(0, Math.round((NOW - Date.parse(iso)) / 60000));
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    const hours = Math.round(mins / 60);
    if (hours < 24) return hours + 'h ago';
    return Math.round(hours / 24) + 'd ago';
  }

  /* honest elapsed for a running turn: minutes between its start and the
     capture's read model — not the viewer's wall clock */
  const elapsedMin = (iso) => Math.max(0, Math.floor((NOW - Date.parse(iso)) / 60000));

  const durLabel = (s) => (s < 90 ? s + 's' : Math.round(s / 60) + 'm');

  const hash8 = (h) => esc(String(h).replace(/^sha256:/, '').slice(0, 8));
  const ref = (t) => '<span class="ref">' + esc(t) + '</span>';

  function andList(nums) {
    const parts = nums.map((n) => ref('#' + n));
    if (parts.length <= 2) return parts.join(' and ');
    return parts.slice(0, -1).join(', ') + ' and ' + parts[parts.length - 1];
  }

  /* ---------- icons (state is always icon + words, never color alone) ---------- */

  const svg = (inner, filled) =>
    '<span class="icon" aria-hidden="true"><svg viewBox="0 0 12 12" ' +
    (filled ? 'fill="currentColor"'
            : 'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"') +
    '>' + inner + '</svg></span>';

  const I = {
    seal:   svg('<path d="M6 .9 11.1 6 6 11.1.9 6Z"/>', true),
    check:  svg('<path d="M2 6.6 4.7 9.3 10 3.4"/>', false),
    dot:    svg('<circle cx="6" cy="6" r="3.4"/>', true),
    ring:   svg('<circle cx="6" cy="6" r="3.6"/>', false),
    half:   svg('<circle cx="6" cy="6" r="3.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M6 2.4a3.6 3.6 0 0 1 0 7.2Z" fill="currentColor" stroke="none"/>', true),
    pause:  svg('<rect x="3.1" y="2.7" width="1.9" height="6.6" rx=".6"/><rect x="7" y="2.7" width="1.9" height="6.6" rx=".6"/>', true),
    lock:   svg('<rect x="2.7" y="5.3" width="6.6" height="4.4" rx="1"/><path d="M4.2 5.3V4a1.8 1.8 0 0 1 3.6 0v1.3"/>', false),
    compass: svg('<circle cx="6" cy="6" r="4.6"/><path d="M7.9 4.1 6.7 6.7 4.1 7.9 5.3 5.3Z" fill="currentColor" stroke="none"/>', false)
  };

  /* ---------- quiet hand-drawn progress arc ---------- */

  function wobblePath(cx, cy, r, a0, a1, seed) {
    const steps = Math.max(10, Math.round(Math.abs(a1 - a0) / 10));
    let d = '';
    for (let i = 0; i <= steps; i++) {
      const deg = a0 + ((a1 - a0) * i) / steps;
      const rad = (deg * Math.PI) / 180;
      const rr = r + Math.sin(seed * 3.1 + i * 1.7) * 0.65;
      const x = cx + rr * Math.cos(rad);
      const y = cy + rr * Math.sin(rad);
      d += (i ? ' L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
    }
    return d;
  }

  function arc(frac, seed, size, label) {
    size = size || 40;
    const c = size / 2;
    const r = c - 4;
    const track = wobblePath(c, c, r, -90, 269.5, seed + 5);
    let prog = '';
    if (frac > 0) {
      const sweep = Math.max(16, frac * 360);
      prog = '<path d="' + wobblePath(c, c, r, -90, -90 + sweep, seed) + '" class="arc-prog"/>';
    }
    return '<svg class="arc" viewBox="0 0 ' + size + ' ' + size + '" width="' + size +
      '" height="' + size + '" role="img" aria-label="' + esc(label) + '">' +
      '<path d="' + track + '" class="arc-track"/>' + prog + '</svg>';
  }

  /* ---------- data access ---------- */

  const project = () => data.projects.find((p) => p.id === state.projectId);
  const stagedProposals = (p) => p.proposals.filter((x) => x.state === 'staged');
  const convById = (p, id) => p.conversations.find((c) => c.id === id);
  const mapResolved = (m) => m.tickets.filter((t) => t.state === 'resolved').length;

  const isBgTurn = (c) => c.turn && (c.turn.state === 'running' || c.turn.state === 'queued');

  const bgConvsFor = (p, mapNumber) => p.conversations.filter((c) =>
    isBgTurn(c) && CONV_EFFORT[c.id] && CONV_EFFORT[c.id].map === mapNumber);

  const convForTicket = (p, ticketNumber) => p.conversations.find((c) =>
    isBgTurn(c) && CONV_EFFORT[c.id] && CONV_EFFORT[c.id].ticket === ticketNumber);

  /* the queued note, with the MOCK prioritize override applied */
  const turnNote = (c) => (state.prioritized[c.id]
    ? 'prioritized — next admission slot'
    : (c.turn.note || ''));

  function turnHeadline(c) {
    const t = c.turn;
    if (t.state === 'running') return esc(t.skill) + ' turn running · ' + elapsedMin(t.started_at) + 'm';
    if (t.state === 'queued') return esc(t.skill) + ' turn queued';
    return 'Turn complete — waiting on you';
  }

  function prioBtn(c, small) {
    if (!c.turn || c.turn.state !== 'queued' || !c.turn.can_prioritize || state.prioritized[c.id]) return '';
    return '<button class="btn btn--quiet' + (small ? ' btn--sm' : '') +
      '" data-act="prioritize:' + esc(c.id) + '">Prioritize</button>';
  }

  function evLabel(e) {
    if (e.kind === 'adr') {
      const m = /adr\/(\d+)/.exec(e.ref);
      return '<span class="ev">ADR ' + (m ? esc(m[1]) : esc(e.ref)) + '</span>';
    }
    if (e.kind === 'research') return '<span class="ev">research doc</span>';
    if (e.kind === 'ci') return '<span class="ev">CI · ' + esc(e.ref) + '</span>';
    if (e.kind === 'glossary') return '<span class="ev">glossary · ' + esc(e.ref) + '</span>';
    if (e.kind === 'merged_pr') return '<span class="ev">merged · ' + esc(e.ref.split('/').pop()) + '</span>';
    return '<span class="ev">' + esc(e.kind) + ' · ' + esc(e.ref) + '</span>';
  }

  /* ---------- home: head with honest instrument line ---------- */

  function homeHead(p, staged) {
    const maps = p.decision_maps.length;
    const active = p.milestones.filter((m) => m.state === 'active').length;
    const bits = [esc(p.profile)];
    if (maps) bits.push(maps + (maps === 1 ? ' map' : ' maps'));
    if (active) bits.push(active + ' building');
    const calm = (!staged.length && (p.decision_maps.length || p.milestones.length))
      ? '<div class="urgency--calm">' + I.check + '<span>Nothing waiting on you.</span></div>'
      : '';
    return '<div class="room-head"><h1 class="room-title">' + esc(p.repo) + '</h1>' +
      '<p class="room-status">' + bits.join(' · ') + '</p></div>' + calm;
  }

  /* ---------- home: background-turn lines inside shelf objects ---------- */

  function objTurnLine(c) {
    const t = c.turn;
    if (t.state === 'running') {
      return '<div class="turnline turnline--run">' + I.half +
        '<span class="turnline-body"><span class="turnline-head">' + turnHeadline(c) + '</span>' +
        ' <span class="dim">— “' + esc(c.title) + '” · ' +
        '<button class="linklike" data-nav="conv:' + esc(c.id) + '">open the conversation</button></span></span>' +
        '</div>';
    }
    if (t.state === 'queued') {
      return '<div class="turnline">' + I.pause +
        '<span class="turnline-body"><span class="turnline-head">' + turnHeadline(c) + '</span>' +
        ' <span class="dim">· ' + esc(turnNote(c)) + ' — “' + esc(c.title) + '” · ' +
        '<button class="linklike" data-nav="conv:' + esc(c.id) + '">open the conversation</button></span></span>' +
        prioBtn(c, true) +
        '</div>';
    }
    return '';
  }

  /* ---------- home: shelf objects, each rendered by its own state ---------- */

  function frontierItems(p, m, withType) {
    const items = [];
    for (const t of m.tickets) {
      if (t.state === 'in_conversation') {
        const you = t.claimed_by === 'operator';
        items.push({
          tone: you ? 'copper' : 'teal',
          icon: you ? 'seal' : 'half',
          head: cap(t.title),
          sub: (you ? 'In conversation' : 'Researching') +
            ' · ' + ref('#' + t.number) + (withType ? ' · ' + esc(t.type) : ''),
          ticket: t.number
        });
      } else if (t.state === 'blocked') {
        items.push({
          tone: 'mute',
          icon: 'lock',
          head: cap(t.title),
          sub: 'Blocked · needs ' + andList(t.blocked_by) + ' · ' + ref('#' + t.number) +
            (withType ? ' · ' + esc(t.type) : ''),
          ticket: t.number
        });
      }
    }
    return items;
  }

  const nextItem = (it) =>
    '<li class="next-item next-item--' + it.tone + '">' + I[it.icon] +
    '<span><span class="next-head">' + esc(it.head) + '</span>' +
    '<span class="next-sub">' + it.sub + '</span>' + (it.extra || '') + '</span></li>';

  /* A proposed milestone holding a staged approval — the copper object.
     The title is the decision (the build issue); the destination line names
     the milestone it advances, so the map → milestone → issue path is legible. */
  function objAwaiting(p, ms, prop) {
    const conv = convById(p, prop.conversation_id);
    return '<article class="obj obj--awaiting">' +
      '<div class="obj-tags"><span class="pill pill--copper">' + I.seal + 'Waiting on you</span>' +
      '<span class="obj-kind">Staged build issue' +
      (conv ? ' · from your Ask' : '') + '</span></div>' +
      '<h2 class="obj-title obj-title--sm">' + esc(prop.title) + '</h2>' +
      '<p class="dest">' + I.compass + '<span>Starts <strong>' + esc(ms.title) + '</strong>' +
      (ms.from_map ? ' · map ' + ref('#' + ms.from_map) : '') + '</span></p>' +
      '<p class="obj-meta"><span class="dim">v' + prop.version + ' · ' +
      ref(hash8(prop.content_hash)) + ' · staged ' + ago(prop.staged_at) + '</span></p>' +
      '<div class="obj-actions"><button class="btn btn--copper" data-nav="approval:' + esc(prop.id) + '">Review &amp; approve</button>' +
      (conv ? '<button class="linklike" data-nav="conv:' + esc(conv.id) + '">read the Ask</button>' : '') +
      '</div></article>';
  }

  /* Once its publication verified (in-session), the same object shows the
     change landed on the shelf — the peak returns to where the operator lives. */
  function objPublished(p, ms, prop) {
    return '<article class="obj obj--published">' +
      '<div class="obj-tags"><span class="pill pill--ok">' + I.check + 'Published just now</span>' +
      '<span class="obj-kind">Build issue · handed to the pipeline</span></div>' +
      '<h2 class="obj-title obj-title--sm">' + esc(prop.title) + '</h2>' +
      '<p class="dest">' + I.compass + '<span>Started <strong>' + esc(ms.title) + '</strong>' +
      (ms.from_map ? ' · map ' + ref('#' + ms.from_map) : '') + '</span></p>' +
      '<div class="obj-actions"><a class="quiet-link" href="dashboard-v2-combined.html">Watch it build in the fleet console ↗</a></div>' +
      '</article>';
  }

  /* A deciding map mid-loop — arc, frontier, background turns, one continue action. */
  function objMidloop(p, m) {
    const total = m.tickets.length;
    const done = mapResolved(m);
    const items = frontierItems(p, m, false).slice(0, 3);
    return '<article class="obj obj--panel">' +
      '<div class="obj-tags"><span class="pill pill--teal">' + I.compass + 'Deciding</span>' +
      '<span class="obj-kind">Decision map ' + ref('#' + m.number) + '</span></div>' +
      '<h2 class="obj-title">' + esc(cap(m.title)) + '</h2>' +
      '<div class="obj-progress">' + arc(done / total, m.number, 40, done + ' of ' + total + ' settled') +
      '<span class="frac">' + done + ' of ' + total + ' settled</span></div>' +
      '<p class="list-label">Next up</p>' +
      '<ul class="next">' + items.map(nextItem).join('') + '</ul>' +
      bgConvsFor(p, m.number).map(objTurnLine).join('') +
      '<div class="obj-actions"><button class="btn btn--teal" data-nav="effort:' + m.number + '">Open the map</button></div>' +
      '</article>';
  }

  /* A map nobody has started — a flatter, quieter folio. */
  function objDormant(p, m) {
    const total = m.tickets.length;
    const open = m.tickets.filter((t) => t.state === 'open');
    const blocked = m.tickets.filter((t) => t.state === 'blocked');
    return '<article class="obj--ledger">' +
      '<div class="obj-tags"><span class="pill pill--ghost">' + I.ring + 'Not started</span>' +
      '<span class="obj-kind">Decision map ' + ref('#' + m.number) + '</span></div>' +
      '<h2 class="obj-title obj-title--sm">' + esc(cap(m.title)) + '</h2>' +
      '<div class="obj-meta">' + arc(0, m.number, 28, '0 of ' + total + ' settled') +
      '<span class="frac">0 of ' + total + ' settled · opened ' + ago(m.opened_at) +
      (blocked.length ? ' · ' + blocked.length + ' blocked' : '') + '</span></div>' +
      '<p class="list-label">First threads</p>' +
      '<ul class="next">' + open.slice(0, 2).map((t) =>
        '<li class="next-item">' + I.ring +
        '<span><span class="next-head">' + esc(cap(t.title)) + '</span>' +
        '<span class="next-sub">Open · ' + ref('#' + t.number) + ' · ' + esc(t.type) + '</span></span></li>'
      ).join('') + '</ul>' +
      (open.length > 2 ? '<p class="obj-note">' + I.dot + '<span>+ ' + (open.length - 2) + ' more open threads</span></p>' : '') +
      bgConvsFor(p, m.number).map(objTurnLine).join('') +
      '<div class="obj-actions"><button class="btn btn--quiet" data-nav="effort:' + m.number + '">Pull the first thread</button></div>' +
      '</article>';
  }

  /* An active milestone the machines are carrying — wide, low, no copper. */
  function objActive(p, ms) {
    const activeBy = new Map(p.build_issues.map((b) => [b.number, b]));
    const rows = ms.build_issues.map((n) => activeBy.get(n) || { number: n, stage: 'landed' });
    const landedCount = rows.filter((r) => r.stage === 'landed').length;
    const pipe = rows.map((r) => {
      if (r.stage === 'building') {
        return '<span class="pipe-item pipe-item--teal">' + I.dot + '<span>' + ref('#' + r.number) +
          ' building now <span class="dim">· ' + esc(r.tool) + ' — ' + esc(r.model) +
          ' · started ' + ago(r.started_at) + '</span></span></span>';
      }
      if (r.stage === 'ready') {
        return '<span class="pipe-item">' + I.ring + '<span>' + ref('#' + r.number) + ' ready</span></span>';
      }
      if (r.stage === 'held') {
        const why = (r.note || 'held').replace(/^held:\s*/, '');
        return '<span class="pipe-item">' + I.pause + '<span>' + ref('#' + r.number) +
          ' held <span class="dim">· ' + esc(why) + '</span></span></span>';
      }
      return '<span class="pipe-item pipe-item--ok">' + I.check + '<span>' + ref('#' + r.number) + ' landed</span></span>';
    }).join('');
    return '<article class="obj--ledger">' +
      '<div class="obj-tags"><span class="pill pill--teal">' + I.half + 'Building in the background</span>' +
      '<span class="obj-kind">Milestone · active</span></div>' +
      '<h2 class="obj-title obj-title--sm">' + esc(ms.title) + '</h2>' +
      '<div class="obj-progress">' + arc(landedCount / rows.length, 31, 34, landedCount + ' of ' + rows.length + ' landed') +
      '<span class="frac">' + landedCount + ' of ' + rows.length + ' landed</span></div>' +
      '<div class="pipeline">' + pipe + '</div>' +
      (ms.evidence.length ? '<div class="ev-row"><span class="dim">Evidence</span>' +
        ms.evidence.map(evLabel).join('') + '</div>' : '') +
      '<div class="obj-actions"><a class="quiet-link" href="dashboard-v2-combined.html">Watch in the fleet console ↗</a></div>' +
      '</article>';
  }

  /* Queue instinct: staged approval (0) > operator-claimed frontier (1) >
     next unblocked (2) > background-only (3). One ordering, sharpest first. */
  function shelf(p, staged) {
    const objs = [];
    for (const ms of p.milestones) {
      const prop = ms.state === 'proposed' ? staged.find((x) => x.kind === 'build_issue') : null;
      if (prop) {
        objs.push(state.published[prop.id]
          ? { rank: 0, html: objPublished(p, ms, prop) }
          : { rank: 0, html: objAwaiting(p, ms, prop) });
      } else if (ms.state === 'active') objs.push({ rank: 3, html: objActive(p, ms) });
    }
    for (const m of p.decision_maps) {
      const yours = m.tickets.some((t) => t.state === 'in_conversation' && t.claimed_by === 'operator');
      const live = m.tickets.some((t) => t.state === 'in_conversation');
      if (yours) objs.push({ rank: 1, html: objMidloop(p, m) });
      else if (mapResolved(m) > 0 || live) objs.push({ rank: 1.5, html: objMidloop(p, m) });
      else objs.push({ rank: 2, html: objDormant(p, m) });
    }
    objs.sort((a, b) => a.rank - b.rank);
    return '<section class="shelf" aria-label="Efforts, sharpest decision first">' +
      objs.map((o) => o.html).join('') + '</section>';
  }

  /* ---------- home: recently landed ---------- */

  function provenance(prov) {
    if (!prov) return '';
    const bits = [];
    if (prov.decision_ticket) bits.push('ticket ' + ref('#' + prov.decision_ticket));
    if (prov.map) bits.push('map ' + ref('#' + prov.map));
    return bits.length ? '<span class="landed-meta">from ' + bits.join(' · ') + '</span>' : '';
  }

  function landedStrip(p) {
    if (!p.landed.length) return '';
    return '<section class="landed" aria-label="Recently landed">' +
      '<p class="list-label">Recently landed</p>' +
      p.landed.map((l) =>
        '<div class="landed-row">' +
        ref('PR #' + l.pr) +
        '<span class="landed-title">' + esc(l.title) + '</span>' +
        '<span class="landed-meta">landed ' + ago(l.merged_at) + ' · built by ' + esc(l.builder) + '</span>' +
        '<span class="landed-ev">' + l.evidence.map(evLabel).join('') + '</span>' +
        provenance(l.provenance) +
        '</div>'
      ).join('') +
      '</section>';
  }

  /* ---------- empty state (a project with no efforts yet) ---------- */

  function emptyShelf(p) {
    const starters = [
      'What does agentflow see in this repo right now?',
      'Chart the first effort for this repository.',
      'Draft a small first build issue.'
    ];
    return '<div class="room-head"><h1 class="room-title">' + esc(p.repo) + '</h1>' +
      '<p class="room-status">' + esc(p.profile) + ' · enrolled ' + ago(p.enrolled_at) + ' · nothing asked yet</p></div>' +
      '<div class="empty2"><p class="empty2-lead">Start with an Ask. Nothing reaches GitHub until you approve it.</p>' +
      '<div class="sugs">' + starters.map((s) =>
        '<button class="sug" type="button" data-act="starter" data-text="' + esc(s) + '">' + I.compass +
        '<span class="sug-text"><span class="sug-q">' + esc(s) + '</span></span></button>').join('') +
      '</div></div>';
  }

  /* ---------- views ---------- */

  function parkedChip() {
    if (!state.parkedNew) return '';
    const first = state.parkedNew.pending ||
      (state.parkedNew.turns[0] && state.parkedNew.turns[0].text) || 'your draft';
    const snip = first.length > 52 ? first.slice(0, 52) + '…' : first;
    return '<button class="resume-chip" type="button" data-act="resume-parked">' + I.compass +
      '<span>Parked draft — “' + esc(snip) + '”</span><span class="resume-go">Resume</span></button>';
  }

  function viewHome(p) {
    const staged = stagedProposals(p);
    const hasEfforts = p.decision_maps.length || p.milestones.length;
    if (!hasEfforts) return emptyShelf(p) + (state.parkedNew ? parkedChip() : '');
    return homeHead(p, staged) + parkedChip() + shelf(p, staged) + landedStrip(p);
  }

  /* -- approval: two-beat hash-bound approve, then verified publication -- */

  function decisionPanel(p, prop) {
    const h = ref(String(prop.content_hash).replace(/^sha256:/, '').slice(0, 8));
    if (state.approval === 'verified') {
      return '<div class="decision-note">' + I.check +
        '<span><strong>Publication verified.</strong> v' + prop.version +
        ' is now published truth for ' + ref(p.repo) + ' — the issue is created and handed to the pipeline.</span></div>' +
        '<div class="receipt"><span class="ev">issue · ' + esc(p.repo) + '</span>' +
        '<span>receipt verified just now</span>' +
        '<span class="dim">mock — nothing real was published</span></div>' +
        '<div class="decision-row"><button class="btn btn--teal" data-nav="home">See it on the shelf →</button></div>';
    }
    if (state.approval === 'publishing') {
      return '<div class="decision-note pubwait">' + I.half +
        '<span>Publishing v' + prop.version + ' · ' + h + ' to ' + ref(p.repo) +
        '… waiting for the publication receipt to verify.</span></div>';
    }
    if (state.approval === 'discarded') {
      return '<div class="decision-note"><span>Mockup — nothing was discarded. In the app, discarding retires the staged proposal without publishing; the conversation keeps its history.</span></div>';
    }
    if (state.approval === 'armed') {
      return '<p class="decision-q">Publish version ' + prop.version + ' (' + h + ') to ' + ref(p.repo) +
        '? This creates the GitHub issue and hands it to the pipeline.</p>' +
        '<div class="decision-row">' +
        '<button class="btn btn--copper" data-act="confirm">Confirm — approve &amp; publish</button>' +
        '<button class="btn btn--quiet" data-act="disarm">Keep reviewing</button>' +
        '</div>';
    }
    return '<div class="decision-row">' +
      '<button class="btn btn--copper" data-act="arm">Approve &amp; publish…</button>' +
      '<button class="btn btn--danger-quiet" data-act="discard">Discard proposal</button>' +
      '</div>' +
      '<p class="dim decision-sub">Approving creates the GitHub issue and hands it to the pipeline. ' +
      'It binds to version ' + prop.version + ' and counts only once the publication is verified.</p>';
  }

  function approvalPill() {
    if (state.approval === 'verified') return '<span class="pill pill--ok">' + I.check + 'Published — verified</span>';
    if (state.approval === 'publishing') return '<span class="pill pill--teal">' + I.half + 'Publishing…</span>';
    if (state.approval === 'discarded') return '<span class="pill pill--ghost">' + I.ring + 'Discarded</span>';
    return '<span class="pill pill--copper">' + I.seal + 'Waiting on you</span>';
  }

  function viewApproval(p, id) {
    const prop = p.proposals.find((x) => x.id === id);
    if (!prop) return '<p class="load-error">Proposal not found in the capture.</p>';
    const conv = convById(p, prop.conversation_id);
    const ms = p.milestones.find((m) => m.state === 'proposed');
    return '<div class="backrow"><button class="btn btn--ghost btn--sm" data-nav="home">← Back to the shelf</button></div>' +
      '<div class="doc-page">' +
      '<div class="page-head-meta">' + approvalPill() +
      '<span>staged ' + ago(prop.staged_at) + ' · v' + prop.version + ' · ' +
      ref(hash8(prop.content_hash)) + '</span></div>' +
      '<h1 class="doc-h1">' + esc(prop.title) + '</h1>' +
      '<p class="kind-line">Build-issue proposal for ' + ref(p.repo) + '</p>' +
      (ms ? '<p class="dest dest--doc">' + I.compass + '<span>Approving starts <strong>' + esc(ms.title) +
        '</strong>' + (ms.from_map ? ' · map ' + ref('#' + ms.from_map) : '') +
        ', then hands this issue to the pipeline.</span></p>' : '') +
      '<p class="prose">' + esc(prop.summary) + '</p>' +
      '<div class="sect"><h2 class="sect-h">Acceptance criteria</h2>' +
      '<ul class="accept">' + prop.acceptance.map((a) =>
        '<li>' + I.ring + '<span>' + esc(a) + '</span></li>').join('') + '</ul></div>' +
      '<div class="sect"><h2 class="sect-h">Versions</h2>' +
      '<ul class="vers">' + prop.versions.map((v) => {
        const current = v.note === 'current';
        return '<li class="' + (current ? 'ver--current' : '') + '">' +
          ref('v' + v.version + ' · ' + String(v.content_hash).replace(/^sha256:/, '').slice(0, 8)) +
          '<span>staged ' + ago(v.staged_at) + '</span>' +
          '<span class="' + (current ? 'ver-note-current' : 'dim') + '">' + esc(v.note) + '</span></li>';
      }).join('') + '</ul></div>' +
      (conv ? '<div class="sect"><h2 class="sect-h">Where it came from</h2>' +
        '<p class="prose">Staged from the Ask “' + esc(conv.title) + '” — ' +
        '<button class="linklike" data-nav="conv:' + esc(conv.id) + '">read the conversation</button>.</p></div>' : '') +
      '<div class="decision">' + decisionPanel(p, prop) + '</div>' +
      '</div>';
  }

  /* -- effort detail: the map, frontier highlighted, background turns surfaced -- */

  function ticketTurnExtra(p, ticketNumber) {
    const c = convForTicket(p, ticketNumber);
    if (!c) return '';
    const noteBit = c.turn.state === 'queued' ? ' · ' + esc(turnNote(c)) : '';
    return '<span class="next-sub">' + turnHeadline(c) + noteBit +
      ' — <button class="linklike" data-nav="conv:' + esc(c.id) + '">open the conversation</button>' +
      (prioBtn(c, true) ? ' ' : '') + '</span>' +
      (prioBtn(c, true) ? '<span class="next-sub">' + prioBtn(c, true) + '</span>' : '');
  }

  function viewEffort(p, num) {
    const m = p.decision_maps.find((x) => x.number === Number(num));
    if (!m) return '<p class="load-error">Map not found in the capture.</p>';
    const total = m.tickets.length;
    const done = mapResolved(m);
    const frontier = frontierItems(p, m, true).map((it) => {
      it.extra = ticketTurnExtra(p, it.ticket);
      return it;
    });
    const settled = m.tickets.filter((t) => t.state === 'resolved');
    const open = m.tickets.filter((t) => t.state === 'open');
    const landedFor = (n) => p.landed.filter((l) => l.provenance && l.provenance.decision_ticket === n);

    /* conversation history bound through landed provenance + map-publishing receipts */
    const convIds = new Set();
    for (const l of p.landed) {
      if (l.provenance && l.provenance.map === m.number && l.provenance.conversation) {
        convIds.add(l.provenance.conversation);
      }
    }
    for (const pr of p.proposals) {
      if (pr.kind === 'decision_map' && pr.publication &&
          pr.publication.receipts.some((r) => r.ref.endsWith('#' + m.number))) {
        convIds.add(pr.conversation_id);
      }
    }
    const history = p.conversations.filter((c) => convIds.has(c.id));

    return '<div class="backrow"><button class="btn btn--ghost btn--sm" data-nav="home">← Back to the shelf</button></div>' +
      '<div class="detail-head">' +
      '<div class="page-head-meta"><span class="pill pill--teal">' + I.compass + 'Deciding</span>' +
      '<span>Decision map ' + ref('#' + m.number) + ' · opened ' + ago(m.opened_at) + '</span></div>' +
      '<h1 class="doc-h1">' + esc(cap(m.title)) + '</h1>' +
      '<div class="detail-meta">' + arc(done / total, m.number, 52, done + ' of ' + total + ' settled') +
      '<span class="frac">' + done + ' of ' + total + ' tickets settled</span></div>' +
      '</div>' +

      (frontier.length ? '<div class="sect"><h2 class="sect-h">The frontier — what moves this next</h2>' +
        '<ul class="next">' + frontier.map(nextItem).join('') + '</ul></div>' : '') +

      (open.length ? '<div class="sect"><h2 class="sect-h">Open threads</h2>' +
        '<ul class="next">' + open.map((t) =>
          '<li class="next-item">' + I.ring +
          '<span><span class="next-head">' + esc(cap(t.title)) + '</span>' +
          '<span class="next-sub">Open · ' + ref('#' + t.number) + ' · ' + esc(t.type) + '</span>' +
          ticketTurnExtra(p, t.number) + '</span></li>'
        ).join('') + '</ul></div>' : '') +

      (settled.length ? '<div class="sect"><h2 class="sect-h">Decisions so far</h2>' +
        '<ul class="settled-list">' + settled.map((t) => {
          const lands = landedFor(t.number);
          return '<li class="settled-item">' + I.check +
            '<span><span class="settled-head">' + esc(cap(t.title)) + '</span>' +
            '<span class="next-sub">' + ref('#' + t.number) + ' · ' + esc(t.type) +
            ' · resolved ' + ago(t.resolved_at) + (t.adr ? ' · ADR ' + esc(t.adr) : '') + '</span>' +
            lands.map((l) =>
              '<span class="settled-ev">landed as ' + ref('PR #' + l.pr) + ' — ' +
              l.evidence.map(evLabel).join(' ') + '</span>').join('') +
            '</span></li>';
        }).join('') + '</ul></div>' : '') +

      (history.length ? '<div class="sect"><h2 class="sect-h">How it got here</h2>' +
        '<ul class="hist">' + history.map((c) =>
          '<li><span class="hist-title">' + esc(c.title) + '</span>' +
          '<p class="hist-meta">' + esc(cap(c.verb)) + ' · ' + c.turns + ' turns · ' +
          esc(c.state) + ' · last turn ' + ago(c.last_turn_at) + '</p>' +
          (c.outcome ? '<p class="hist-outcome">' + esc(c.outcome) + '</p>' : '') + '</li>'
        ).join('') + '</ul></div>' : '');
  }

  /* -- dialogue: the room yields to conversation; the composer never moves -- */

  function turnHtml(t) {
    const who = t.role === 'operator' ? 'You' : 'Wayfinder';
    const skill = (t.skill && t.role !== 'operator')
      ? '<span class="turn-skill">' + esc(t.skill) + ' · ' + durLabel(t.duration_s) + '</span>'
      : '';
    return '<div class="turn turn--' + esc(t.role) + '">' +
      '<div class="turn-meta"><span class="turn-who">' + who + '</span>' + skill +
      '<span class="dim">' + ago(t.at) + '</span></div>' +
      '<p class="turn-text">' + esc(t.text) + '</p></div>';
  }

  function mockTurnHtml(t) {
    return '<div class="turn turn--operator">' +
      '<div class="turn-meta"><span class="turn-who">You</span>' +
      '<span class="dim">just now · mock turn</span></div>' +
      '<p class="turn-text">' + esc(t.text) + '</p></div>';
  }

  /* the current turn as a calm block after the last message (ADR 0034) */
  function turnStateBlock(conv) {
    const t = conv.turn;
    if (!t) return '';
    if (t.state === 'parked') {
      const tag = t.priority === 'interactive' ? '<span class="tag">interactive — priority</span>' : '';
      return '<div class="turnstate turnstate--parked">' + I.seal +
        '<span class="turnstate-body"><span class="turnstate-head">Turn complete — waiting on you</span>' +
        '<span class="turnstate-sub">The staged proposal below is this turn’s outcome. Your next message submits the next turn.</span></span>' +
        tag + '</div>';
    }
    if (t.state === 'running') {
      return '<div class="turnstate turnstate--run">' + I.half +
        '<span class="turnstate-body"><span class="turnstate-head">' + esc(t.skill) + ' turn running · ' +
        elapsedMin(t.started_at) + 'm</span>' +
        '<span class="turnstate-sub">A bounded background session is doing the work — the answer lands here as a new turn. ' +
        'Elapsed is measured against the read model, not live.</span></span>' +
        '<span class="tag">background</span></div>';
    }
    if (t.state === 'queued') {
      return '<div class="turnstate">' + I.pause +
        '<span class="turnstate-body"><span class="turnstate-head">' + esc(t.skill) + ' turn queued</span>' +
        '<span class="turnstate-sub">' + esc(turnNote(conv)) + '</span></span>' +
        prioBtn(conv, false) +
        '<span class="tag">background</span></div>';
    }
    return '';
  }

  /* after a mock reply, the honest state is "a new turn was submitted" */
  function mockSubmittedBlock() {
    return '<div class="turnstate">' + I.ring +
      '<span class="turnstate-body"><span class="turnstate-head">Turn submitted</span>' +
      '<span class="turnstate-sub">In the real workspace this queues a bounded session through the coordinator. ' +
      'Mock — no session runs and turns aren’t persisted.</span></span></div>';
  }

  function viewDialogue(p) {
    const c = state.convo;
    if (!c) return viewHome(p);
    const park = '<div class="backrow"><button class="btn btn--ghost btn--sm" data-act="park">← Park conversation — back to the shelf</button></div>';

    if (c.mode === 'existing') {
      const conv = convById(p, c.convId);
      if (!conv) return '<p class="load-error">Conversation not found in the capture.</p>';
      const mocks = state.mockTurns[conv.id] || [];
      const staged = (conv.staged_proposal_ids || [])
        .map((pid) => p.proposals.find((x) => x.id === pid))
        .filter((x) => x && x.state === 'staged');
      const shown = conv.last_turns ? conv.last_turns.length : 0;

      return park +
        '<div class="convo">' +
        '<div class="page-head-meta"><span class="pill pill--teal">' + I.compass + esc(cap(conv.verb)) + ' · ' + esc(conv.state) + '</span>' +
        '<span>opened ' + ago(conv.opened_at) + ' · last turn ' + ago(conv.last_turn_at) + '</span></div>' +
        '<h1 class="doc-h1">' + esc(conv.title) + '</h1>' +
        (shown
          ? '<p class="convo-note">Showing the last ' + shown + ' of ' + conv.turns +
            ' turns · revision ' + conv.revision + '</p>'
          : '<p class="convo-note">' + conv.turns + ' turns · revision ' + conv.revision +
            ' — earlier turns aren’t part of this projection.</p>') +
        '<div role="log" aria-label="Conversation transcript">' +
        (conv.last_turns || []).map(turnHtml).join('') +
        mocks.map(mockTurnHtml).join('') +
        (mocks.length ? mockSubmittedBlock() : turnStateBlock(conv)) +
        '</div>' +
        staged.map((pr) =>
          '<div class="attach">' +
          '<span class="pill pill--copper">' + I.seal + 'Staged — waiting on you</span>' +
          '<p class="attach-title">' + esc(pr.title) + '</p>' +
          '<p class="attach-meta">Build issue · v' + pr.version + ' · ' +
          ref(String(pr.content_hash).replace(/^sha256:/, '').slice(0, 8)) + ' · staged ' + ago(pr.staged_at) + '</p>' +
          '<div class="attach-actions"><button class="btn btn--copper btn--sm" data-nav="approval:' + esc(pr.id) + '">Review &amp; approve</button></div>' +
          '</div>'
        ).join('') +
        '</div>';
    }

    /* new (possibly scoped) conversation */
    let title = 'New conversation';
    let scopeLine = '';
    if (c.scope) {
      const m = p.decision_maps.find((x) => x.number === c.scope.map);
      if (m && c.scope.ticket) {
        const t = m.tickets.find((x) => x.number === c.scope.ticket);
        if (t) { title = cap(t.title); scopeLine = 'Scoped to ticket ' + ref('#' + t.number) + ' on decision map ' + ref('#' + m.number) + '. '; }
      } else if (m) {
        title = cap(m.title);
        scopeLine = 'Scoped to decision map ' + ref('#' + m.number) + '. ';
      }
    }
    return park +
      '<div class="convo">' +
      '<div class="page-head-meta"><span class="pill pill--teal">' + I.compass + 'New Ask</span>' +
      '<span>' + ref(p.repo) + '</span></div>' +
      '<h1 class="doc-h1">' + esc(title) + '</h1>' +
      '<div role="log" aria-label="Conversation transcript">' +
      '<p class="context-note">' + scopeLine +
      'Send a message to begin. Each reply runs as a bounded background session — interactive turns take admission priority; you can park this and come back.</p>' +
      c.turns.map(mockTurnHtml).join('') +
      (c.turns.length ? mockSubmittedBlock() : '') +
      '</div></div>';
  }

  /* ---------- the dock: just the composer ---------- */

  function updateDock() {
    /* The dock is only the Ask panel now — prioritization lives in the shelf. */
    let placeholder, hint, label;
    if (state.view.name === 'dialogue') {
      const existing = state.convo && state.convo.mode === 'existing';
      placeholder = existing ? 'Reply to Wayfinder…' : 'Say what you’re after…';
      hint = 'Enter sends · Shift+Enter newline · Esc parks';
      label = 'Send';
    } else if (state.view.name === 'effort') {
      placeholder = 'Ask about this map…';
      hint = 'Press / to ask';
      label = 'Ask';
    } else if (state.view.name === 'approval') {
      placeholder = 'Ask about this proposal…';
      hint = 'Press / to ask';
      label = 'Ask';
    } else {
      placeholder = 'What should we work on?';
      hint = 'Press / to ask';
      label = 'Ask';
    }
    composer.placeholder = placeholder;
    hintEl.textContent = hint;
    sendBtn.textContent = label;
  }

  /* ---------- routing: every view has a URL, so Back and direct links work ---------- */

  function encodeHash() {
    const pid = state.projectId;
    const v = state.view;
    if (v.name === 'effort') return '#/' + pid + '/map/' + v.param;
    if (v.name === 'approval') return '#/' + pid + '/approve/' + v.param;
    if (v.name === 'dialogue') {
      if (state.convo && state.convo.mode === 'existing') return '#/' + pid + '/ask/' + state.convo.convId;
      return '#/' + pid + '/ask/new';
    }
    return '#/' + pid;
  }

  function setHash(h) {
    if (location.hash === h) return; /* no-op for in-place renders (arm, send, prioritize) */
    programmatic = true;
    location.hash = h;
  }

  /* Reconstruct state from the URL — used by Back/Forward and direct loads. */
  function applyRoute() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    let pid = parts[0];
    if (!data.projects.some((p) => p.id === pid)) pid = data.projects[0].id;
    if (pid !== state.projectId) { state.projectId = pid; projectSelect.value = pid; }
    state.approval = 'staged';
    state.convo = null;
    const kind = parts[1], param = parts[2];
    if (kind === 'map') state.view = { name: 'effort', param: param };
    else if (kind === 'approve') state.view = { name: 'approval', param: param };
    else if (kind === 'ask') {
      if (param && param !== 'new' && convById(project(), param)) {
        state.convo = { mode: 'existing', convId: param };
        state.view = { name: 'dialogue', param: param };
      } else {
        state.convo = { mode: 'new', scope: null, turns: [] };
        state.view = { name: 'dialogue', param: null };
      }
    } else state.view = { name: 'home', param: null };
    render();
  }

  /* ---------- render loop ---------- */

  function render(opts) {
    opts = opts || {};
    const p = project();
    let html;
    switch (state.view.name) {
      case 'approval': html = viewApproval(p, state.view.param); break;
      case 'effort': html = viewEffort(p, state.view.param); break;
      case 'dialogue': html = viewDialogue(p); break;
      default: html = viewHome(p);
    }
    app.innerHTML = '<div class="view" tabindex="-1">' + html + '</div>';
    updateDock();
    /* In-place state changes (arming an approval, prioritizing) keep the operator
       where they were; only real navigation returns to the top. */
    if (!opts.keepScroll) window.scrollTo(0, 0);
    /* Keep the URL in sync so Back/Forward and direct links land on this view.
       No-ops when the hash is unchanged (in-place approval/send renders). */
    setHash(encodeHash());
    if (opts.focus) {
      const el = app.querySelector(opts.focus);
      if (el) { el.focus(); return; }
    }
    /* Navigation moves screen-reader focus into the freshly rendered view —
       preventScroll so it doesn't fight the scrollTo(0,0) above. */
    if (!opts.keepScroll && !opts.noFocus) {
      const v = app.querySelector('.view');
      if (v) v.focus({ preventScroll: true });
    }
  }

  /* ---------- conversation lifecycle ---------- */

  function openConversation(convId) {
    state.convo = { mode: 'existing', convId: convId };
    state.view = { name: 'dialogue', param: convId };
    render();
    composer.focus();
  }

  function startNew(scope) {
    state.convo = { mode: 'new', scope: scope || null, turns: [] };
    state.view = { name: 'dialogue', param: null };
    render();
    composer.focus();
  }

  function park() {
    const pending = composer.value.trim();
    if (state.convo && state.convo.mode === 'new' && (state.convo.turns.length || pending)) {
      state.convo.pending = pending; /* the unsent draft, so Esc never eats it */
      state.parkedNew = state.convo;
    }
    state.convo = null;
    composer.value = '';
    autoresize();
    state.view = { name: 'home', param: null };
    render();
  }

  function sendTurn() {
    const text = composer.value.trim();
    if (!text) return;
    if (state.view.name !== 'dialogue') {
      startNew(state.view.name === 'effort' ? { map: Number(state.view.param) } : null);
    }
    const turn = { role: 'operator', at: new Date(NOW).toISOString(), text: text };
    if (state.convo.mode === 'existing') {
      (state.mockTurns[state.convo.convId] = state.mockTurns[state.convo.convId] || []).push(turn);
    } else {
      state.convo.turns.push(turn);
    }
    composer.value = '';
    autoresize();
    render();
    composer.focus();
  }

  function autoresize() {
    composer.style.height = 'auto';
    composer.style.height = Math.min(composer.scrollHeight, 160) + 'px';
  }

  /* ---------- events ---------- */

  document.addEventListener('click', (e) => {
    const nav = e.target.closest('[data-nav]');
    if (nav) {
      const i = nav.dataset.nav.indexOf(':');
      const name = i === -1 ? nav.dataset.nav : nav.dataset.nav.slice(0, i);
      const param = i === -1 ? null : nav.dataset.nav.slice(i + 1);
      if (name === 'conv') { openConversation(param); return; }
      state.convo = null;
      state.view = { name: name, param: param };
      if (name === 'approval') state.approval = 'staged';
      render();
      return;
    }
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const raw = act.dataset.act;
    const j = raw.indexOf(':');
    const a = j === -1 ? raw : raw.slice(0, j);
    const arg = j === -1 ? null : raw.slice(j + 1);

    if (a === 'arm') { state.approval = 'armed'; render({ keepScroll: true, focus: '[data-act="confirm"]' }); }
    else if (a === 'disarm') { state.approval = 'staged'; render({ keepScroll: true }); }
    else if (a === 'confirm') {
      /* verified two-step: publishing… then a verified receipt — never instant */
      state.approval = 'publishing';
      announce('Publishing to GitHub…');
      render({ keepScroll: true });
      window.setTimeout(() => {
        if (state.approval === 'publishing' && state.view.name === 'approval') {
          state.approval = 'verified';
          if (arg == null && state.view.param) state.published[state.view.param] = true;
          announce('Publication verified. The build issue is created and handed to the pipeline.');
          render({ keepScroll: true });
        }
      }, 1100);
    }
    else if (a === 'discard') { state.approval = 'discarded'; render({ keepScroll: true }); }
    else if (a === 'prioritize') {
      state.prioritized[arg] = true; /* MOCK: flips the queued note */
      render({ keepScroll: true });
    }
    else if (a === 'park') park();
    else if (a === 'starter') { composer.value = act.dataset.text; startNew(null); autoresize(); }
    else if (a === 'resume-parked') {
      state.convo = state.parkedNew;
      const pending = state.parkedNew.pending || '';
      state.parkedNew = null;
      state.view = { name: 'dialogue', param: null };
      render({ noFocus: true });
      composer.value = pending;
      autoresize();
      composer.focus();
    }
  });

  /* the transformation: typing makes the room yield to dialogue —
     the composer itself never moves */
  composer.addEventListener('input', () => {
    autoresize();
    if (state.view.name !== 'dialogue' && composer.value.trim()) {
      startNew(state.view.name === 'effort' ? { map: Number(state.view.param) } : null);
    }
  });
  composer.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTurn(); }
  });
  document.getElementById('dock-form').addEventListener('submit', (e) => {
    e.preventDefault();
    sendTurn();
  });

  document.addEventListener('keydown', (e) => {
    const tag = document.activeElement && document.activeElement.tagName;
    const typing = tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT';
    if (e.key === '/' && !typing) {
      e.preventDefault();
      composer.focus();
    } else if (e.key === 'Escape' && state.view.name === 'dialogue') {
      park();
    } else if (e.key === 'Escape' && state.view.name === 'approval') {
      state.convo = null; state.view = { name: 'home', param: null }; render();
    } else if (!typing && state.view.name === 'approval') {
      /* power-user approve: a arms, y confirms — same guarded two beats */
      if (e.key === 'a' && state.approval === 'staged') {
        e.preventDefault(); state.approval = 'armed';
        render({ keepScroll: true, focus: '[data-act="confirm"]' });
      } else if (e.key === 'y' && state.approval === 'armed') {
        e.preventDefault();
        const btn = app.querySelector('[data-act="confirm"]');
        if (btn) btn.click();
      }
    }
  });

  /* Back/Forward or a hand-edited URL: rebuild state from the hash. Our own
     setHash writes are flagged so they don't trigger a redundant re-parse. */
  window.addEventListener('hashchange', () => {
    if (programmatic) { programmatic = false; return; }
    applyRoute();
  });

  /* keep main clear of the dock, whatever height it settles at */
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      document.documentElement.style.setProperty('--dock-h', dockEl.offsetHeight + 'px');
    }).observe(dockEl);
  }

  /* ---------- chrome (MOCK-ONLY wiring) ---------- */

  function currentTheme() {
    return document.documentElement.dataset.theme ||
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function themeLabel() {
    themeToggle.textContent = currentTheme() === 'dark' ? 'Light theme' : 'Dark theme';
  }
  themeToggle.addEventListener('click', () => {
    document.documentElement.dataset.theme = currentTheme() === 'dark' ? 'light' : 'dark';
    themeLabel();
  });
  themeLabel();

  projectSelect.addEventListener('change', () => {
    state.projectId = projectSelect.value;
    state.view = { name: 'home', param: null };
    state.approval = 'staged';
    state.convo = null;
    state.parkedNew = null;
    composer.value = '';
    autoresize();
    render();
  });

  /* ---------- boot ---------- */

  fetch('./workspace.capture.json')
    .then((r) => r.json())
    .then((json) => {
      data = json;
      /* honest ages: everything is relative to the projection's own read-model
         timestamp, not the viewer's wall clock */
      NOW = Date.parse(data.workspace.read_model_at);
      state.projectId = data.projects[0].id;
      projectSelect.innerHTML = data.projects.map((p) =>
        '<option value="' + esc(p.id) + '">' + esc(p.repo) + '</option>').join('');
      /* Honor a direct link on load; otherwise seed the home URL without an
         extra history entry. */
      if (location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).length) {
        applyRoute();
      } else {
        history.replaceState(null, '', '#/' + state.projectId);
        render();
      }
    })
    .catch(() => {
      app.innerHTML = '<p class="load-error">Could not load <span class="ref">workspace.capture.json</span> — serve this directory over HTTP.</p>';
    });
})();
