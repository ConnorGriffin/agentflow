<script>
  // The read-only operator briefing (ADR 0035/0036), built to the LOCKED visual spec
  // mockups/operator-surface-finalist.html (#183). Additive alongside the existing console:
  // this tab reads the same daemon-published `/api/snapshot`, deriving its own continuous
  // reading path — Attention, then Decision Maps, then Fleet health — from the schema-v2
  // fields. All actions are explicit external GitHub links; the browser changes nothing.
  import { deriveBriefing } from './lib/derive.js';

  let { snap } = $props();

  let theme = $state('light');
  let briefing = $derived(deriveBriefing(snap));
</script>

<section class="briefing" data-theme={theme}>
  <header class="mast">
    <div class="wordmark">agent<span>flow</span></div>
    <div class="freshness">{briefing.freshness.label}</div>
    <button class="theme" type="button" aria-label="Toggle colour theme"
      onclick={() => (theme = theme === 'dark' ? 'light' : 'dark')}>
      {theme === 'dark' ? 'Light theme' : 'Dark theme'}
    </button>
  </header>

  {#if briefing.freshness.state === 'stale' || briefing.freshness.state === 'incomplete'}
    <div class="banner {briefing.freshness.state}" role="status">{briefing.freshness.message}</div>
  {/if}

  <section aria-labelledby="briefing-title">
    <p class="eyebrow">Read-only operator briefing</p>
    <h1 id="briefing-title">What needs you, then what is moving.</h1>
    <p class="lede">A bounded daemon projection. Actions open their GitHub authority; this console changes nothing.</p>
  </section>

  <section aria-labelledby="attention-title">
    <div class="section-head">
      <h2 id="attention-title">Attention</h2>
      <span class="count">{briefing.attention.length} {briefing.attention.length === 1 ? 'item' : 'items'}</span>
    </div>
    <div class="rows">
      {#each briefing.attention as item}
        <article class="attention">
          <span class="kind">{item.kind}</span>
          <div><h3>{item.title}</h3><p>{item.detail}</p></div>
          <a class="action" href={item.url} target="_blank" rel="noreferrer">Open in GitHub ↗</a>
        </article>
      {:else}
        <div class="empty">No operator actions in this projection.</div>
      {/each}
    </div>
  </section>

  <section aria-labelledby="maps-title">
    <div class="section-head">
      <h2 id="maps-title">Decision Maps</h2>
      <span class="count">{briefing.maps.length} {briefing.maps.length === 1 ? 'active map' : 'active maps'}</span>
    </div>
    {#each briefing.maps as map}
      <article class="map">
        <div class="map-top">
          <div><h3>{map.title}</h3><p class="meta">{map.repository} · {map.progress}</p></div>
          <span class="status">{map.status}</span>
        </div>
        <p class="frontier"><b>Frontier:</b> {map.frontier}</p>
        <details>
          <summary>Ticket outline · {map.tickets.length} records</summary>
          <ul>{#each map.tickets as t}<li>{t}</li>{/each}</ul>
        </details>
        <details>
          <summary>Supporting records</summary>
          <div class="support">
            {#each map.support as s}<a href={s.url} target="_blank" rel="noreferrer">{s.label}</a>{/each}
          </div>
        </details>
      </article>
    {:else}
      <div class="empty">No active Decision Maps in this bounded projection.</div>
    {/each}
  </section>

  <section aria-labelledby="fleet-title">
    <div class="section-head">
      <h2 id="fleet-title">Fleet health</h2>
      <span class="count">{briefing.fleet.length} {briefing.fleet.length === 1 ? 'repository' : 'repositories'}</span>
    </div>
    <div class="capacity">
      {#each briefing.capacity as pool}<span><b>{pool.name}</b> {pool.detail}</span>{/each}
    </div>
    <div class="fleet">
      {#each briefing.fleet as repo}
        <article class="repo">
          <strong>{repo.name}</strong><span>{repo.profile}</span><span>{repo.work}</span>
          <span class={repo.health === 'healthy' ? 'healthy' : ''}>{repo.health}</span>
        </article>
      {:else}
        <div class="empty">No repositories were included in this projection.</div>
      {/each}
    </div>
  </section>
</section>

<style>
  /* Successor tokens for ADR 0035/0036 (#183), scoped to `.briefing` rather than `:root` —
     this surface deliberately reopens the console's dark-mono visual system, without
     touching it, since App.svelte mounts this alongside the retained console tabs. */
  .briefing {
    --paper: #f7f7f5; --surface: #fff; --line: #d8d9d5; --ink: #202320; --muted: #656963;
    --quiet: #eef0ec; --accent: #176e68; --warn: #9a5b14; --danger: #a94037; --ok: #397148;
    --link: #145e91;
    background: var(--paper); color: var(--ink);
    font: 15px/1.48 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 1120px; margin: 0 auto; padding: 0 28px 48px; display: block;
  }
  .briefing[data-theme='dark'] {
    --paper: #181a19; --surface: #222523; --line: #414640; --ink: #edf0eb; --muted: #b5bbb2;
    --quiet: #2a2e2b; --accent: #72c9c0; --warn: #e6ae65; --danger: #ed9389; --ok: #8ac89a;
    --link: #8bc9f1;
  }
  .briefing * { box-sizing: border-box; }
  .briefing button, .briefing a { font: inherit; }
  .briefing a { color: var(--link); text-underline-offset: 3px; }
  .briefing button { color: inherit; background: transparent; }
  .briefing button:focus-visible, .briefing a:focus-visible, .briefing summary:focus-visible {
    outline: 3px solid var(--accent); outline-offset: 3px; border-radius: 2px;
  }
  .mast {
    display: flex; align-items: center; gap: 16px; min-height: 84px; border-bottom: 1px solid var(--line);
  }
  .wordmark { font-size: 19px; font-weight: 700; letter-spacing: -.04em; }
  .wordmark span { color: var(--accent); }
  .freshness { margin-left: auto; color: var(--muted); font-size: 13px; }
  .theme { border: 1px solid var(--line); padding: 7px 10px; border-radius: 4px; cursor: pointer; }
  .banner {
    margin: 18px 0 4px; padding: 11px 13px; border: 1px solid currentColor; color: var(--warn);
    background: color-mix(in srgb, var(--warn) 8%, transparent); font-size: 14px;
  }
  .banner.incomplete { color: var(--danger); }
  .briefing section { padding-top: 23px; }
  .briefing h1, .briefing h2, .briefing h3, .briefing p { margin: 0; }
  h1 { font-size: 30px; letter-spacing: -.04em; font-weight: 650; text-wrap: balance; }
  .lede { margin-top: 5px; color: var(--muted); max-width: 60ch; text-wrap: pretty; }
  .eyebrow {
    color: var(--muted); text-transform: uppercase; letter-spacing: .11em; font-size: 11px;
    font-weight: 700; margin-bottom: 6px;
  }
  .section-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 7px; }
  h2 { font-size: 19px; letter-spacing: -.025em; text-wrap: balance; }
  .count { color: var(--muted); font-size: 13px; }
  .rows { border-top: 1px solid var(--line); }
  .attention {
    display: grid; grid-template-columns: 104px minmax(0, 1fr) auto; gap: 17px; align-items: center;
    padding: 9px 0; border-bottom: 1px solid var(--line);
  }
  .kind { font-size: 12px; font-weight: 700; color: var(--warn); }
  .attention h3 { font-size: 15px; font-weight: 620; text-wrap: balance; }
  .attention p, .meta { font-size: 13px; color: var(--muted); margin-top: 1px; }
  .action {
    white-space: nowrap; border: 1px solid var(--line); border-radius: 4px; padding: 7px 10px;
    text-decoration: none; color: var(--ink);
  }
  .action:hover { border-color: var(--accent); color: var(--accent); }
  .map { border-top: 1px solid var(--line); padding: 10px 0; }
  .map:last-child { border-bottom: 1px solid var(--line); }
  .map-top { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 20px; align-items: start; }
  .map h3 { font-size: 16px; font-weight: 650; text-wrap: balance; }
  .status { color: var(--accent); font-size: 13px; font-weight: 650; white-space: nowrap; }
  .frontier { margin-top: 5px; font-size: 14px; }
  .frontier b { color: var(--warn); }
  .briefing details { margin-top: 5px; color: var(--muted); font-size: 13px; }
  .briefing summary { cursor: pointer; color: var(--link); width: max-content; max-width: 100%; }
  .briefing details ul { margin: 7px 0 0; padding-left: 18px; }
  .support { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 7px; }
  .fleet { border-top: 1px solid var(--line); }
  .repo {
    display: grid; grid-template-columns: minmax(170px, 1.6fr) 110px 1fr 1fr; gap: 18px;
    padding: 12px 0; border-bottom: 1px solid var(--line); align-items: center;
  }
  .repo strong { font-size: 14px; }
  .repo span { font-size: 13px; color: var(--muted); }
  .healthy { color: var(--ok) !important; }
  .capacity {
    display: flex; gap: 18px; padding: 13px 0; border-bottom: 1px solid var(--line);
    color: var(--muted); font-size: 13px;
  }
  .capacity b { color: var(--ink); }
  .empty {
    padding: 28px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
    color: var(--muted);
  }
  @media (max-width: 700px) {
    .briefing { padding: 0 18px 34px; }
    .mast { min-height: 68px; flex-wrap: wrap; padding: 12px 0; gap: 8px; }
    .freshness { margin-left: 0; order: 3; width: 100%; }
    h1 { font-size: 26px; }
    .attention { grid-template-columns: 1fr; gap: 5px; }
    .action { width: max-content; margin-top: 3px; }
    .map-top { grid-template-columns: 1fr; gap: 5px; }
    .repo { grid-template-columns: 1fr 1fr; gap: 5px 14px; }
    .repo strong { grid-column: 1 / -1; }
    .capacity { flex-wrap: wrap; gap: 5px 16px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .briefing * { scroll-behavior: auto !important; transition: none !important; }
  }
</style>
