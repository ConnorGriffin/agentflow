<script>
  import { short, elapsed, rel, ratchetBar, reviewerOf } from './lib/derive.js';

  // snap: the fleet snapshot. The repo row is the spine; columns are the signals.
  // A row highlights when it needs you, and expands IN PLACE into its lane (read-only —
  // per-reason controls arrive in a later slice).
  let { snap } = $props();

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  let needsOnly = $state(false);
  let sortKey = $state('needs');
  let sortDir = $state('desc');
  let expanded = $state(new Set());

  const COLS = [
    { key: null, label: '', cls: 'c-caret' },
    { key: 'repo', label: 'repo', cls: 'c-repo' },
    { key: 'profile', label: 'profile', cls: 'c-prof' },
    { key: 'running', label: 'running', cls: 'c-run' },
    { key: 'ready', label: 'ready', cls: 'c-num', hint: 'issues queued for an agent' },
    { key: 'inflight', label: 'in-flight', cls: 'c-flight', hint: 'open PRs in the pipeline' },
    { key: 'held', label: 'held', cls: 'c-num', hint: 'issues held: needs-grilling / needs-mockup' },
    { key: 'parked', label: 'parked', cls: 'c-num', hint: 'PRs parked for your decision' },
    { key: 'ratchet', label: 'ratchet', cls: 'c-rat', hint: 'trust ratchet — progress toward more autonomy' },
  ];

  const runningOf = (r) => (snap?.running || []).filter((s) => s.repo === r.repo);
  function needsOf(r) {
    const out = [];
    if (r.profile === 'reviewed' || r.profile === 'guarded')
      for (const pr of r.in_flight || []) out.push({ kind: 'merge' });
    for (const h of r.held || []) out.push({ kind: 'held' });
    for (const p of r.parked || []) out.push({ kind: 'parked' });
    if (r.ratchet && r.ratchet.ready_to_loosen) out.push({ kind: 'loosen' });
    return out;
  }

  const SORTS = {
    repo: (r) => short(r.repo).toLowerCase(),
    profile: (r) => ({ autonomous: 0, reviewed: 1, guarded: 2 }[r.profile]),
    running: (r) => runningOf(r).length,
    ready: (r) => (r.ready || []).length,
    inflight: (r) => (r.in_flight || []).length,
    held: (r) => (r.held || []).length,
    parked: (r) => (r.parked || []).length,
    ratchet: (r) => (r.ratchet ? r.ratchet.correction_rate : 1),
    needs: (r) => needsOf(r).length,
  };

  let repos = $derived(snap?.repos || []);
  let needsCount = $derived(repos.filter((r) => needsOf(r).length).length);
  let rows = $derived.by(() => {
    let rs = needsOnly ? repos.filter((r) => needsOf(r).length) : repos.slice();
    const f = SORTS[sortKey] || SORTS.needs;
    const dir = sortDir === 'asc' ? 1 : -1;
    return rs.sort((a, b) => {
      const va = f(a);
      const vb = f(b);
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return short(a.repo).toLowerCase() < short(b.repo).toLowerCase() ? -1 : 1;
    });
  });

  function sortBy(key) {
    if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    else {
      sortKey = key;
      sortDir = key === 'repo' || key === 'profile' ? 'asc' : 'desc';
    }
  }
  function toggle(repo) {
    const next = new Set(expanded);
    next.has(repo) ? next.delete(repo) : next.add(repo);
    expanded = next;
  }
  const mergesOf = (r) =>
    (r.recent_merges || []).slice().sort((a, b) => Date.parse(b.merged_at) - Date.parse(a.merged_at));
</script>

<div class="viewhead">
  <h2>Fleet</h2>
  <span class="cnt">{repos.length}</span>
  <span class="sub">repo × signals · expand a row for its lane</span>
</div>

<div class="fleet-toolbar">
  <button class:on={needsOnly} aria-pressed={needsOnly} onclick={() => (needsOnly = !needsOnly)}>
    <span class="fbox">{needsOnly ? '☑' : '☐'}</span> only rows that need me
    <span class="fcount">{needsCount}</span>
  </button>
  <span class="rownote">
    {needsOnly ? `showing ${rows.length} of ${repos.length} repos` : `${repos.length} repos · ${needsCount} need you`}
  </span>
  <span class="sorthint">· click a column to sort</span>
</div>

<div class="fleet-tablewrap">
  <table class="grid">
    <thead>
      <tr>
        {#each COLS as c}
          {#if !c.key}
            <th class={c.cls}></th>
          {:else}
            {@const active = sortKey === c.key}
            <th
              class="{c.cls} sortable"
              class:sorted={active}
              title={c.hint || 'sort by ' + c.label}
              aria-sort={active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
              onclick={() => sortBy(c.key)}
            >
              {c.label}<span class="scar">{active ? (sortDir === 'asc' ? ' ▲' : ' ▼') : ''}</span>
            </th>
          {/if}
        {/each}
      </tr>
    </thead>
    <tbody>
      {#if rows.length === 0}
        <tr><td colspan={COLS.length}>
          <div class="allclear">
            <span class="ck ok big">✓</span>
            <div><b>Nothing needs you.</b> Every repo is running itself — no merge waiting,
              nothing held or parked, no dial ready to loosen. Toggle the filter off to see the full fleet.</div>
          </div>
        </td></tr>
      {:else}
        {#each rows as r (r.repo)}
          {@const runs = runningOf(r)}
          {@const needs = needsOf(r)}
          {@const exp = expanded.has(r.repo)}
          {@const rb = ratchetBar(r.ratchet || {})}
          <tr class="drow" class:needs={needs.length} class:exp aria-expanded={exp}
            onclick={() => toggle(r.repo)}>
            <td class="c-caret">
              <button class="caret" class:open={exp} aria-label="{exp ? 'collapse' : 'expand'} {short(r.repo)}">▸</button>
            </td>
            <th scope="row" class="c-repo">
              {#if needs.length}
                <span class="ndot on" title="needs you: {needs.map((n) => n.kind).join(', ')}">●</span>
              {:else}
                <span class="ndot" aria-hidden="true">·</span>
              {/if}
              <span class="rname">{short(r.repo)}</span>
            </th>
            <td class="c-prof"><span class="mini-badge {r.profile}">{r.profile}</span></td>
            <td class="c-run">
              {#if runs.length}
                {@const s = runs[0]}
                <span class="run">
                  <span class="pulse tool-{s.tool}"></span>
                  <b class="rstage">{s.stage}</b>
                  <span class="tool-{s.tool}">{s.tool}</span>
                  <span class="dim num">· {elapsed(s.started_at, now)}</span>{#if runs.length > 1}
                    <span class="dim"> +{runs.length - 1}</span>{/if}
                </span>
              {:else}<span class="dim">idle</span>{/if}
            </td>
            <td class="c-num">
              {#if (r.ready || []).length}<span class="cnt-cell">{(r.ready || []).length}</span>
              {:else}<span class="dim">·</span>{/if}
            </td>
            <td class="c-flight">
              {#if (r.in_flight || []).length}
                {@const pr = r.in_flight[0]}
                <span class="inflight">
                  <span class="num">#{pr.number}</span>
                  <span class="ttl">{pr.title}</span>{#if r.in_flight.length > 1}
                    <span class="dim"> +{r.in_flight.length - 1}</span>{/if}
                </span>
              {:else}<span class="dim">—</span>{/if}
            </td>
            <td class="c-num">
              {#if (r.held || []).length}<span class="cnt-cell held">⚠ {(r.held || []).length}</span>
              {:else}<span class="dim">·</span>{/if}
            </td>
            <td class="c-num">
              {#if (r.parked || []).length}<span class="cnt-cell parked">⏸ {(r.parked || []).length}</span>
              {:else}<span class="dim">·</span>{/if}
            </td>
            <td class="c-rat">
              <span class="rat" title={rb.stat}>
                <svg class="rbar" width="46" height="8" viewBox="0 0 46 8" aria-hidden="true">
                  <rect x="0" y="2" width="46" height="4" rx="2" fill="#21262d" />
                  <rect x="0" y="2" width={Math.round((46 * rb.trustW) / 100)} height="4" rx="2" fill={rb.barC} />
                </svg>
                {#if r.ratchet && r.ratchet.ready_to_loosen}<span class="loosen-pill">▲ loosen</span>
                {:else}<span class="rnum">{rb.short}</span>{/if}
              </span>
            </td>
          </tr>
          {#if exp}
            <tr class="lane-row"><td colspan={COLS.length}>
              <div class="exlane">
                <div class="exlane-top">
                  <b>{r.repo}</b><span class="mini-badge {r.profile}">{r.profile}</span>
                </div>
                <div class="lgrid">
                  <div class="lsec"><div class="lhead">running <span class="lcount">{runs.length}</span></div>
                    <div class="lbody">
                      {#each runs as s}
                        <div class="litem">
                          <div class="lrow1"><span class="pulse tool-{s.tool}"></span> <b>{s.stage}</b>
                            <span class="tool-{s.tool}">{s.tool}</span> <span class="dim">{s.model}</span>
                            <span class="dim">· {elapsed(s.started_at, now)}</span></div>
                          <div class="lmeta">#{s.number} {s.title}</div>
                        </div>
                      {:else}<div class="lempty">no live session</div>{/each}
                    </div>
                  </div>
                  <div class="lsec"><div class="lhead">in-flight <span class="lcount">{(r.in_flight || []).length}</span></div>
                    <div class="lbody">
                      {#each r.in_flight || [] as pr}
                        <div class="litem">
                          <div class="lrow1"><span class="num">#{pr.number}</span> {pr.title}</div>
                          <div class="lmeta"><span class="tool-{pr.builder}">{pr.builder}</span> built ·
                            <span class="tool-{reviewerOf(pr.builder)}">{reviewerOf(pr.builder)}</span> reviews</div>
                        </div>
                      {:else}<div class="lempty">nothing in flight</div>{/each}
                    </div>
                  </div>
                  <div class="lsec"><div class="lhead">ready queue <span class="lcount">{(r.ready || []).length}</span></div>
                    <div class="lbody">
                      {#each r.ready || [] as q}
                        <div class="litem lrow1"><span class="num">#{q.number}</span> {q.title}
                          <span class="tag2">{q.complexity}</span><span class="tag2 alt">{q.effort} effort</span></div>
                      {:else}<div class="lempty">queue empty</div>{/each}
                    </div>
                  </div>
                  <div class="lsec"><div class="lhead">held <span class="lcount">{(r.held || []).length}</span></div>
                    <div class="lbody">
                      {#each r.held || [] as h}
                        <div class="litem">
                          <div class="lrow1"><span class="num">#{h.number}</span> {h.title}
                            <span class="badge {h.state}">{h.state}</span></div>
                          <div class="lmeta dim">{h.reason} · held {rel(h.since, now)}</div>
                        </div>
                      {:else}<div class="lempty">none held</div>{/each}
                    </div>
                  </div>
                  <div class="lsec"><div class="lhead">parked <span class="lcount">{(r.parked || []).length}</span></div>
                    <div class="lbody">
                      {#each r.parked || [] as p}
                        <div class="litem">
                          <div class="lrow1"><span class="num">#{p.number}</span> {p.title}
                            <span class="badge park">{p.reason}</span></div>
                          <div class="lmeta dim"><span class="tool-{p.builder}">{p.builder}</span> built ·
                            <span class="tool-{p.reviewer || reviewerOf(p.builder)}">{p.reviewer || reviewerOf(p.builder)}</span> reviews ·
                            parked {rel(p.since, now)}</div>
                        </div>
                      {:else}<div class="lempty">none parked</div>{/each}
                    </div>
                  </div>
                  <div class="lsec"><div class="lhead">recent merges <span class="lcount">{mergesOf(r).length}</span></div>
                    <div class="lbody">
                      {#each mergesOf(r) as m}
                        <div class="litem lrow1"><span class="ck ok">✓</span> <span class="num">#{m.number}</span> {m.title}
                          <span class="dim">· {rel(m.merged_at, now)}</span></div>
                      {:else}<div class="lempty">none yet</div>{/each}
                    </div>
                  </div>
                  <div class="lsec"><div class="lhead">trust ratchet</div>
                    <div class="lbody">
                      <div class="ratlane">
                        <svg width="160" height="8" viewBox="0 0 160 8" aria-hidden="true">
                          <rect x="0" y="2" width="160" height="4" rx="2" fill="#21262d" />
                          <rect x="0" y="2" width={Math.round((160 * rb.trustW) / 100)} height="4" rx="2" fill={rb.barC} />
                        </svg>
                        <div class="lmeta dim">{rb.stat}</div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </td></tr>
          {/if}
        {/each}
      {/if}
    </tbody>
  </table>
</div>
