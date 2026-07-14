<script>
  import { short, pct, headroomColor, elapsed, STAGES } from './lib/derive.js';

  // snap: the fleet snapshot (its running[] + pools[] + daemon block drive this board)
  let { snap } = $props();

  // A once-a-second tick so each session's elapsed timer counts up on its own.
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  let runs = $derived(snap?.running || []);
  let pools = $derived(snap?.pools || []);
  let counts = $derived.by(() => {
    const c = { triaging: 0, building: 0, reviewing: 0 };
    for (const s of runs) if (c[s.stage] != null) c[s.stage]++;
    return c;
  });
  let enabled = $derived(!!snap?.daemon?.enabled);
  let pollMin = $derived(snap?.daemon?.poll_seconds ? Math.round(snap.daemon.poll_seconds / 60) : 5);

  // longest-running on top — the session most likely to want a look
  const laneRows = (key) =>
    runs.filter((s) => s.stage === key).sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at));
  const toolColor = (tool) => (tool === 'claude' ? 'var(--amber)' : 'var(--blue)');
</script>

<div class="board-head">
  <span class="bh-title">Live sessions</span>
  <span class="flow">
    triaging<b>{counts.triaging}</b>
    <span class="arrow">→</span> building<b>{counts.building}</b>
    <span class="arrow">→</span> reviewing<b>{counts.reviewing}</b>
  </span>
  <span class="poolcorr">
    {#each pools as p}
      {@const h = pct(p.headroom_pct)}
      <span class="pc">
        <span class="nm tool-{p.tool}">{p.tool}</span>
        <span class="run"><b>{p.running || 0}</b> running</span>
        <span class="cap"><i style="width:{h}%;background:{headroomColor(h)}"></i></span>
        <span class="hr">{h}%</span>
      </span>
    {/each}
  </span>
</div>

{#if runs.length === 0}
  <!-- honest idle — the 0/0/0 per-stage counts read plainly, not three empty boxes -->
  <div class="board-idle">
    <div class="idle-mark">◍</div>
    <div class="idle-counts"><b>0 triaging · 0 building · 0 reviewing</b> — fleet idle</div>
    <div class="idle-p">
      Nothing is executing this second; the pipeline is clear.
      {#if enabled}The daemon polls every {pollMin}m for new work.{:else}The daemon is paused — no new sessions will start.{/if}
    </div>
  </div>
{:else}
  <div class="lanes">
    {#each STAGES as st}
      <section class="lane" data-stage={st.key}>
        <header class="lane-head">
          <span class="lane-glyph">{st.glyph}</span>
          <span class="lane-name">{st.label}</span>
          <span class="lane-count">{counts[st.key]}</span>
        </header>
        <div class="lane-rows">
          {#each laneRows(st.key) as s (s.worktree)}
            <div class="sess">
              <span class="pulse" style="--p:{toolColor(s.tool)}" aria-hidden="true"></span>
              <div class="sess-main">
                <div class="sess-id">
                  <span class="repo">{short(s.repo)}</span><span class="num">#{s.number}</span>
                  <span class="tbadge tool-{s.tool}">{s.tool}·{s.model}</span>
                </div>
                <div class="sess-title" title={s.title}>{s.title}</div>
                <div class="sess-foot">
                  <span class="branch" title={s.worktree || ''}>
                    {s.branch ? s.branch : `#${s.number} · not yet branched`}
                  </span>
                </div>
              </div>
              <div class="sess-elapsed">{elapsed(s.started_at, now)}</div>
            </div>
          {:else}
            <div class="lane-empty">— no sessions —</div>
          {/each}
        </div>
      </section>
    {/each}
  </div>
{/if}
