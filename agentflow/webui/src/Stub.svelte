<script>
  // A deliberately empty view. Live / Fleet / History arrive in later slices;
  // this slice ships the console shell + Inbox only (ADR 0023, built in slices).
  let { title, blurb } = $props();
</script>

<div class="viewhead">
  <h2>{title}</h2>
  <span class="cnt">0</span>
  <span class="sub">arrives in a later slice</span>
</div>
<div class="zero idle">
  <div class="mark">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--muted)"
      stroke-width="2" stroke-linecap="round"><path d="M5 12h14" /></svg>
  </div>
  <h3>{title} — not yet</h3>
  <p>{blurb}</p>
</div>
