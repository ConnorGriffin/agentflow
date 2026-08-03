<script>
  import Inbox from './Inbox.svelte';
  import Live from './Live.svelte';
  import Fleet from './Fleet.svelte';
  import History from './History.svelte';
  import Briefing from './Briefing.svelte';
  import { deriveInbox, deriveBriefing, pct, headroomColor, rel } from './lib/derive.js';

  // Briefing (ADR 0035/0036, #183) is additive: a new tab reading the same schema-v2
  // snapshot, alongside the retained v1 console until the final retirement slice.
  const TABS = [
    { key: 'inbox', label: 'Inbox', n: 1 },
    { key: 'live', label: 'Live', n: 2 },
    { key: 'fleet', label: 'Fleet', n: 3 },
    { key: 'history', label: 'History', n: 4 },
    { key: 'briefing', label: 'Briefing', n: 5 },
  ];
  const POLL_MS = 4000; // 3–5s browser refresh; the server just re-reads the daemon's file

  let snap = $state(null);
  let view = $state('inbox');
  let selected = $state(-1); // j/k highlight within the Inbox
  let updated = $state('loading…');
  let error = $state('');

  let items = $derived(snap ? deriveInbox(snap) : []);
  let dispatchOn = $derived(!!(snap && snap.dispatch && snap.dispatch.enabled));
  let historyCount = $derived(
    (snap?.repos || []).reduce((n, r) => n + (r.recent_merges || []).length, 0),
  );

  function tabCount(key) {
    if (key === 'inbox') return items.length;
    if (key === 'live') return (snap?.running || []).length;
    if (key === 'fleet') return (snap?.repos || []).length;
    if (key === 'briefing') return snap ? deriveBriefing(snap).attention.total : 0;
    return historyCount;
  }

  async function poll() {
    try {
      const res = await fetch('/api/snapshot');
      snap = await res.json();
      error = '';
      // The data's own age (when the daemon last read GitHub), not this fetch's
      // clock — with the daemon down the stamp ages honestly instead of lying
      // "just updated" about stale data (ADR 0026).
      const fresh = snap?.daemon?.gh_fresh_at;
      updated = fresh ? 'updated ' + rel(fresh, Date.now()) : 'no snapshot yet — daemon has not run';
    } catch (e) {
      error = String(e);
    }
  }

  $effect(() => {
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => clearInterval(id);
  });

  function switchView(v) {
    view = v;
    selected = -1;
  }

  function moveSel(delta) {
    if (view !== 'inbox') return;
    const n = items.length;
    if (!n) return;
    selected =
      selected < 0 ? (delta > 0 ? 0 : n - 1) : Math.max(0, Math.min(n - 1, selected + delta));
  }

  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tab = TABS.find((t) => String(t.n) === e.key);
    if (tab) return switchView(tab.key);
    if (e.key === 'j') { e.preventDefault(); moveSel(1); }
    else if (e.key === 'k') { e.preventDefault(); moveSel(-1); }
  }
</script>

<svelte:window on:keydown={onKey} />

<header>
  <div class="brand"><h1>agentflow</h1><span class="v">control plane · over GitHub</span></div>
  <span class="daemon {dispatchOn ? 'on' : 'off'}">
    <span class="dot"></span>{dispatchOn ? 'dispatch enabled' : 'dispatch dormant'}
  </span>
  <div class="poolchips">
    {#each snap?.pools || [] as p}
      {@const h = pct(p.headroom_pct)}
      <span class="pchip">
        <span class="nm tool-{p.tool}">{p.tool}</span>
        <span class="mini"><i style="width:{h}%;background:{headroomColor(h)}"></i></span>
        <span class="hr">{h}%</span>
      </span>
    {/each}
  </div>
  <span class="spacer"></span>
</header>

<nav role="tablist" aria-label="Console views">
  {#each TABS as t}
    <button
      class="tab"
      class:active={view === t.key}
      role="tab"
      aria-selected={view === t.key}
      onclick={() => switchView(t.key)}
    >
      <span class="tabnum">{t.n}</span> {t.label}
      <span class="cnt" class:alert={t.key === 'inbox' && items.length > 0}>
        {tabCount(t.key)}
      </span>
    </button>
  {/each}
  <span class="navhint"><kbd>1</kbd>–<kbd>5</kbd> views · <kbd>j</kbd>/<kbd>k</kbd> move</span>
</nav>

<main>
  {#if view === 'inbox'}
    <section class="view" role="tabpanel" aria-label="Inbox">
      <Inbox {items} {selected} />
    </section>
  {:else if view === 'live'}
    <section class="view live" role="tabpanel" aria-label="Live">
      <Live {snap} />
    </section>
  {:else if view === 'fleet'}
    <section class="view fleet" role="tabpanel" aria-label="Fleet">
      <Fleet {snap} />
    </section>
  {:else if view === 'history'}
    <section class="view" role="tabpanel" aria-label="History">
      <History {snap} />
    </section>
  {:else}
    <section class="view" role="tabpanel" aria-label="Briefing">
      <Briefing {snap} />
    </section>
  {/if}
</main>

<div class="statusbar">
  <span>agentflow console</span>
  <span>{view}</span>
  <span style="color:{dispatchOn ? 'var(--green)' : 'var(--amber)'}">
    {dispatchOn ? '● dispatch on' : '○ dispatch dormant'}
  </span>
  <span class="sb-r">
    <span>{error ? 'error: ' + error : updated}</span>
    <span><kbd>j</kbd>/<kbd>k</kbd> move</span>
  </span>
</div>
