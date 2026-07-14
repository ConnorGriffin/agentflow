<script>
  import { short } from './lib/derive.js';

  // items: the derived worklist; selected: j/k highlight index (-1 = none)
  let { items, selected } = $props();

  const pad = (i) => String(i + 1).padStart(2, '0');
</script>

<div class="viewhead">
  <h2>Needs you</h2>
  <span class="cnt">{items.length}</span>
  <span class="sub">exceptions only · most urgent first · one action each</span>
</div>

{#if items.length === 0}
  <div class="zero">
    <div class="mark">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--green)"
        stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 12.5l5 5L20 6" />
      </svg>
    </div>
    <h3>Inbox zero</h3>
    <p>Nothing awaits your merge and no dial is ready to loosen. The fleet is running
      itself — check Live for what it's doing now.</p>
  </div>
{:else}
  <div class="list">
    {#each items as it, i (it.kind + (it.number ?? it.repo))}
      <div class="qrow {it.accent}" class:sel={i === selected} data-i={i}>
        <div class="qidx">{pad(i)}</div>
        {#if it.kind === 'merge'}
          <div class="qglyph k-{it.accent}">↑</div>
          <div class="qbody">
            <div class="qtitle"><span class="num">#{it.number}</span> {it.title}</div>
            <div class="qmeta">
              <b class="k-{it.accent}">{it.profile}</b> merge · {short(it.repo)} ·
              <span class="tool-{it.builder}">{it.builder}</span>→<span class="tool-{it.reviewer}">{it.reviewer}</span>
            </div>
          </div>
          <div class="qaside">
            {#if it.profile === 'guarded'}
              <span class="care">read before merging</span>
            {:else}
              <span class="qmuted">quick glance</span>
            {/if}
          </div>
          <button class="qact {it.accent}" aria-label="Merge #{it.number} in {short(it.repo)}">Merge #{it.number}</button>
        {:else}
          <div class="qglyph k-loosen">◉</div>
          <div class="qbody">
            <div class="qtitle">{short(it.repo)} — trust ratchet ready to loosen</div>
            <div class="qmeta">
              <b class="k-loosen">ratchet</b> · {it.samples} clean samples ·
              {Math.round((it.rate || 0) * 100)}% corrected — consistently confirmed. Move toward more autonomy?
            </div>
          </div>
          <div class="qaside"><span class="k-loosen">{it.samples} clean</span></div>
          <button class="qact loosen" aria-label="Loosen the dial on {short(it.repo)}">Loosen dial</button>
        {/if}
      </div>
    {/each}
  </div>
{/if}
