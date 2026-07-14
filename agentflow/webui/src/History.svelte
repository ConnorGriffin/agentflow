<script>
  import { short, rel, reviewerOf } from './lib/derive.js';

  // snap: the fleet snapshot. History is the flattened, reverse-chronological feed of
  // everything that has merged across the fleet — an audit trail (read from the live
  // snapshot; the durable store is deferred, ADR 0023).
  let { snap } = $props();

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(id);
  });

  let merges = $derived.by(() => {
    const out = [];
    for (const r of snap?.repos || [])
      for (const m of r.recent_merges || []) out.push({ ...m, repo: r.repo, profile: r.profile });
    return out.sort((a, b) => Date.parse(b.merged_at) - Date.parse(a.merged_at));
  });
</script>

<div class="viewhead">
  <h2>History</h2>
  <span class="cnt">{merges.length}</span>
  <span class="sub">merged &amp; auto-merged · newest first · audit trail</span>
</div>

{#if merges.length === 0}
  <div class="zero idle">
    <h3>No merges yet</h3>
    <p>When work ships, it lands here as an audit trail.</p>
  </div>
{:else}
  <div class="list histwrap">
    <table class="hist">
      <tbody>
        {#each merges as m (m.repo + '#' + m.number)}
          {@const rev = m.builder ? reviewerOf(m.builder) : null}
          <tr class="hrow">
            <td class="hck"><span class="chk">✓</span></td>
            <td class="hnum"><span class="num">#{m.number}</span></td>
            <td class="htitle">{m.title}</td>
            <td class="hby muted">
              {#if m.builder}
                <span class="tool-{m.builder}">{m.builder}</span>
                <span class="arrow">→</span> <span class="tool-{rev}">{rev}</span>
              {/if}
            </td>
            <td class="hrepo muted">
              {short(m.repo)}{#if m.profile === 'autonomous'} · <span class="chk">auto</span>{/if}
            </td>
            <td class="ago">{rel(m.merged_at, now)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}
