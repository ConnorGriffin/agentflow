<script>
  // The Project workspace surface (ADR 0033/0034), built to the LOCKED visual spec
  // mockups/workspace-combined.html (Wayfinder #128). For tracer #1 it renders the
  // urgency-ordered shelf (the "in a conversation" background weight only), the anchored Ask
  // composer, and the dialogue view with honest turn states — never a faked live-typing stream.
  // It reads the daemon-published projection (GET /api/workspace) and transports operator actions
  // as commands (POST /api/command); it never applies a transition itself.

  const POLL_MS = 4000;

  let data = $state(null);
  let projectId = $state(null);
  let view = $state({ name: 'home', param: null }); // home | dialogue | approval
  let pending = $state({}); // convId -> [operator prompts sent this session, awaiting the projection]
  let notice = $state('');
  // The second beat of a hash-bound approval is UI-local: arming reveals the confirm, which sends
  // the approve command bound to the EXACT version hash on screen (never "latest").
  let armed = $state(false);

  const uuid = () =>
    (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());

  let project = $derived(
    data && data.projects.find((p) => p.id === projectId),
  );

  // ---------- time / text helpers (honest ages against the read model) ----------
  let now = $derived(data ? Date.parse(data.workspace.read_model_at) || Date.now() : Date.now());
  function ago(iso) {
    if (!iso) return '';
    const mins = Math.max(0, Math.round((now - Date.parse(iso)) / 60000));
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    const h = Math.round(mins / 60);
    return h < 24 ? h + 'h ago' : Math.round(h / 24) + 'd ago';
  }
  const elapsedMin = (iso) => Math.max(0, Math.floor((now - Date.parse(iso)) / 60000));
  const convById = (p, id) => p.conversations.find((c) => c.id === id);
  const proposalById = (p, id) =>
    (p.proposals || []).find((x) => x.conversation_id === id);
  const shortHash = (h) => (h || '').replace(/^sha256:/, '').slice(0, 8);
  const shortSha = (s) => (s || '').slice(0, 7);

  // ---------- published card: coarse pipeline mirror (read from the daemon join) ----------
  // The shelf weights are the locked spec's: teal for a live pipeline, ok(green) once landed.
  function pipeLabel(pipe) {
    if (!pipe) return 'Handed to the pipeline';
    const n = pipe.pr_number;
    if (pipe.state === 'merged') return 'PR #' + n + ' merged';
    if (pipe.state === 'in_review') return 'PR #' + n + ' in review';
    if (pipe.state === 'pr_open') return 'PR #' + n + ' open';
    return 'Building in the pipeline…';
  }
  function pipeClass(pipe) {
    if (!pipe) return '';
    if (pipe.state === 'merged') return 'pipe-item--ok';
    if (pipe.state === 'building') return '';
    return 'pipe-item--teal';
  }

  // ---------- routing: every view has a URL (#/<project>/ask/<id>) ----------
  function parseHash() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    let pid = parts[0];
    if (data && !data.projects.some((p) => p.id === pid)) pid = data.projects[0]?.id;
    projectId = pid;
    if (parts[1] === 'ask' && parts[2]) view = { name: 'dialogue', param: parts[2] };
    else if (parts[1] === 'approve' && parts[2]) view = { name: 'approval', param: parts[2] };
    else view = { name: 'home', param: null };
    armed = false; // each view entry starts disarmed — approval is a deliberate two-beat
  }
  function go(hash) {
    if (location.hash !== hash) location.hash = hash;
    else parseHash();
  }
  const openConv = (id) => go('#/' + projectId + '/ask/' + id);
  const openApproval = (cid) => go('#/' + projectId + '/approve/' + cid);
  const toHome = () => go('#/' + projectId);

  // ---------- polling ----------
  async function poll() {
    try {
      const res = await fetch('/api/workspace');
      const next = await res.json();
      data = next;
      if (!projectId) parseHash();
      // Drop optimistic prompts the durable projection has now caught up on.
      const p = data.projects.find((x) => x.id === projectId);
      if (p) {
        for (const c of p.conversations) {
          if (pending[c.id]) pending[c.id] = pending[c.id].slice(c.turns_count);
        }
      }
    } catch (e) {
      /* keep last projection; the workspace ages honestly */
    }
  }

  $effect(() => {
    poll();
    const id = setInterval(poll, POLL_MS);
    const onHash = () => parseHash();
    window.addEventListener('hashchange', onHash);
    return () => {
      clearInterval(id);
      window.removeEventListener('hashchange', onHash);
    };
  });

  // ---------- commands (transport only; the daemon is the writer) ----------
  async function send(command) {
    try {
      const res = await fetch('/api/command', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(command),
      });
      if (res.status === 503) {
        notice = 'The daemon is not running — your message could not be submitted yet.';
        return false;
      }
      notice = '';
      return true;
    } catch (e) {
      notice = 'Could not reach the workspace.';
      return false;
    }
  }

  let composer = $state('');
  function sendTurn() {
    const text = composer.trim();
    if (!text || !project) return;
    if (view.name === 'dialogue') {
      const conv = convById(project, view.param);
      if (conv) {
        (pending[conv.id] = pending[conv.id] || []).push(text);
        send({
          key: uuid(), kind: 'send_turn', repo: project.repo,
          conversation_id: conv.id, prompt: text, expected_revision: conv.revision,
        });
      }
    } else {
      const cid = uuid();
      pending[cid] = [text];
      send({
        key: uuid(), kind: 'open_ask', repo: project.repo,
        conversation_id: cid, prompt: text, title: text.slice(0, 80),
      });
      openConv(cid);
    }
    composer = '';
  }
  function onComposerKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendTurn();
    } else if (e.key === 'Escape' && view.name === 'dialogue') {
      toHome();
    }
  }

  function starter(text) {
    composer = text;
    sendTurn();
  }

  // ---------- Build-Issue Proposal decisions (approve is hash-bound; the daemon publishes) ------
  function approveProposal(prop) {
    if (!project || !prop || !prop.content_hash) return;
    // Bind to the EXACT version hash the operator is looking at (ADR 0034), not "latest".
    send({
      key: uuid(), kind: 'approve_proposal', repo: project.repo,
      conversation_id: prop.conversation_id, content_hash: prop.content_hash,
    });
    armed = false;
  }
  function discardProposal(prop) {
    if (!project || !prop) return;
    send({
      key: uuid(), kind: 'discard_proposal', repo: project.repo,
      conversation_id: prop.conversation_id,
    });
    toHome();
  }

  // ---------- derived view data ----------
  let conversations = $derived(project ? project.conversations : []);
  let proposals = $derived(project && project.proposals ? project.proposals : []);
  let stagedProposals = $derived(proposals.filter((x) => x.state === 'staged'));
  let publishedProposals = $derived(proposals.filter((x) => x.state === 'published'));
  let dockPlaceholder = $derived(
    view.name === 'dialogue' ? 'Reply to Wayfinder…' : 'What should we work on?',
  );
</script>

<nav class="mockbar" aria-label="Workspace">
  <span><strong>agentflow</strong> · project workspace</span>
  <span>{data ? (project ? project.repo : '') : 'loading…'}</span>
</nav>

<header class="top">
  <div class="brand">
    <span class="brand-name">agentflow</span>
    <span class="brand-sub">workspace</span>
  </div>
  <div class="top-links">
    <a class="quiet-link" href="./">Fleet console ↗</a>
  </div>
</header>

<p class="visually-hidden" aria-live="polite" role="status">{notice}</p>

<main id="app-main">
  {#if !data}
    <p class="load-error">Loading the workspace…</p>
  {:else if !project}
    <p class="load-error">No enrolled project yet.</p>
  {:else if view.name === 'dialogue'}
    {@const conv = convById(project, view.param)}
    <div class="view" tabindex="-1">
      <div class="backrow">
        <button class="btn btn--ghost btn--sm" onclick={toHome}
          >← Park conversation — back to the shelf</button>
      </div>
      <div class="convo">
        {#if conv}
          <div class="page-head-meta">
            <span class="pill pill--teal">Ask · {conv.state}</span>
            <span>opened {ago(conv.opened_at)} · revision {conv.revision}</span>
          </div>
          <h1 class="doc-h1">{conv.title || 'Ask'}</h1>
          <div role="log" aria-label="Conversation transcript">
            {#each conv.turns as t}
              <div class="turn turn--operator">
                <div class="turn-meta"><span class="turn-who">You</span></div>
                <p class="turn-text">{t.prompt}</p>
              </div>
              {#if t.reply}
                <div class="turn turn--wayfinder">
                  <div class="turn-meta">
                    <span class="turn-who">Wayfinder</span>
                    <span class="turn-skill">{t.skill}</span>
                  </div>
                  <p class="turn-text">{t.reply}</p>
                </div>
              {/if}
            {/each}
            {#each pending[conv.id] || [] as text}
              <div class="turn turn--operator">
                <div class="turn-meta">
                  <span class="turn-who">You</span><span class="dim">just now</span>
                </div>
                <p class="turn-text">{text}</p>
              </div>
            {/each}
            <!-- the current turn's honest state — a calm block, never a typing indicator -->
            {#if conv.turn && conv.turn.state === 'working'}
              <div class="turnstate turnstate--run">
                <span class="turnstate-body">
                  <span class="turnstate-head">{conv.turn.skill} turn running</span>
                  <span class="turnstate-sub"
                    >A bounded background session is doing the work — the answer lands here as a
                    new turn.</span>
                </span>
                <span class="tag">interactive</span>
              </div>
            {:else if conv.turn && conv.turn.state === 'paused'}
              <div class="turnstate turnstate--parked">
                <span class="turnstate-body">
                  <span class="turnstate-head">Turn parked — needs you</span>
                  <span class="turnstate-sub"
                    >{conv.turn.note || 'Your message is preserved.'} Your next message submits the
                    next turn.</span>
                </span>
              </div>
            {:else if (pending[conv.id] || []).length}
              <div class="turnstate">
                <span class="turnstate-body">
                  <span class="turnstate-head">Turn submitted</span>
                  <span class="turnstate-sub"
                    >Queued through the coordinator as a bounded session — the reply lands here.</span>
                </span>
              </div>
            {/if}
          </div>
        {:else}
          <div class="page-head-meta">
            <span class="pill pill--teal">New Ask</span><span>{project.repo}</span>
          </div>
          <h1 class="doc-h1">New conversation</h1>
          <p class="context-note">
            Send a message to begin. Each reply runs as a bounded background session — interactive
            turns take admission priority; you can park this and come back.
          </p>
          {#each pending[view.param] || [] as text}
            <div class="turn turn--operator">
              <div class="turn-meta"><span class="turn-who">You</span></div>
              <p class="turn-text">{text}</p>
            </div>
          {/each}
        {/if}
      </div>
    </div>
  {:else if view.name === 'approval'}
    {@const prop = proposalById(project, view.param)}
    <div class="view" tabindex="-1">
      <div class="backrow">
        <button class="btn btn--ghost btn--sm" onclick={toHome}>← Back to the shelf</button>
      </div>
      {#if !prop}
        <p class="load-error">This proposal is no longer staged — it was published or discarded.</p>
      {:else}
        <div class="doc-page">
          <div class="page-head-meta">
            {#if prop.state === 'published'}
              <span class="pill pill--ok">Published — verified</span>
            {:else if prop.state === 'approved'}
              <span class="pill pill--teal">Publishing…</span>
            {:else}
              <span class="pill pill--copper">Waiting on you</span>
            {/if}
            <span
              >staged {ago(prop.staged_at)} · v{prop.version} ·
              <span class="ref">{shortHash(prop.content_hash)}</span></span>
          </div>
          <h1 class="doc-h1">{prop.title || 'Build issue'}</h1>
          <p class="kind-line">Build-issue proposal for <span class="ref">{project.repo}</span></p>
          <p class="prose">{prop.summary}</p>

          {#if prop.acceptance && prop.acceptance.length}
            <div class="sect">
              <h2 class="sect-h">Acceptance criteria</h2>
              <ul class="accept">
                {#each prop.acceptance as a}
                  <li><span>{a}</span></li>
                {/each}
              </ul>
            </div>
          {/if}

          <div class="sect">
            <h2 class="sect-h">Versions</h2>
            <ul class="vers">
              {#each prop.versions as v}
                <li class={v.current ? 'ver--current' : ''}>
                  <span class="ref">v{v.version} · {shortHash(v.content_hash)}</span>
                  <span>staged {ago(v.staged_at)}</span>
                  <span class={v.current ? 'ver-note-current' : 'dim'}
                    >{v.current ? 'current — awaiting decision' : 'superseded'}</span>
                </li>
              {/each}
            </ul>
          </div>

          <div class="decision">
            {#if prop.state === 'published'}
              <div class="decision-note">
                <span
                  ><strong>Publication verified.</strong> v{prop.version} is now published truth for
                  <span class="ref">{project.repo}</span> — the build issue is created and handed to
                  the pipeline.</span>
              </div>
              {#if prop.publication}
                <div class="receipt">
                  {#if prop.publication.issue_number}
                    <span class="ev">issue #{prop.publication.issue_number}</span>
                  {/if}
                  <span>provenance <span class="ref">{shortHash(prop.content_hash)}</span></span>
                </div>
              {/if}
              <div class="decision-row">
                {#if prop.publication && prop.publication.issue_url}
                  <a class="btn btn--teal" href={prop.publication.issue_url}>See the build issue ↗</a>
                {/if}
                <button class="linklike" onclick={toHome}>Back to the shelf →</button>
              </div>
            {:else if prop.state === 'approved'}
              <div class="decision-note pubwait">
                <span
                  >Publishing v{prop.version} ·
                  <span class="ref">{shortHash(prop.approved_hash)}</span> to
                  <span class="ref">{project.repo}</span>… waiting for the publication receipt to
                  verify. The daemon creates the issue idempotently — a replay never files a
                  duplicate.</span>
              </div>
            {:else if armed}
              <p class="decision-q">
                Publish version {prop.version} (<span class="ref"
                  >{shortHash(prop.content_hash)}</span>) to
                <span class="ref">{project.repo}</span>? This creates a real GitHub build issue and
                hands it to the pipeline.
              </p>
              <div class="decision-row">
                <button class="btn btn--copper" onclick={() => approveProposal(prop)}
                  >Confirm — approve &amp; publish</button>
                <button class="linklike" onclick={() => (armed = false)}>Cancel</button>
              </div>
            {:else}
              <div class="decision-row">
                <button class="btn btn--copper" onclick={() => (armed = true)}
                  >Approve &amp; publish…</button>
                <button class="btn btn--danger-quiet" onclick={() => discardProposal(prop)}
                  >Discard proposal</button>
              </div>
              <p class="dim decision-sub">
                Approving creates the GitHub issue and hands it to the pipeline. It binds to version
                {prop.version} (<span class="ref">{shortHash(prop.content_hash)}</span>) and counts
                only once the publication is verified. Discarding drops the draft; the conversation
                keeps its history.
              </p>
            {/if}
          </div>
        </div>
      {/if}
    </div>
  {:else}
    <div class="view" tabindex="-1">
      <div class="room-head">
        <h1 class="room-title">{project.repo}</h1>
        <p class="room-status">
          {project.profile || 'workspace'}
          {#if conversations.length}· {conversations.length}
            {conversations.length === 1 ? 'conversation' : 'conversations'}{/if}
        </p>
      </div>

      {#if !conversations.length && !proposals.length}
        <div class="empty2">
          <p class="empty2-lead">
            Start with an Ask. Nothing reaches GitHub until you approve it.
          </p>
          <div class="sugs">
            {#each ['What does agentflow see in this repo right now?', 'Chart the first effort for this repository.', 'Draft a small first build issue.'] as s}
              <button class="sug" type="button" onclick={() => starter(s)}>
                <span class="sug-text"><span class="sug-q">{s}</span></span>
              </button>
            {/each}
          </div>
        </div>
      {:else}
        <section class="shelf" aria-label="Efforts, sharpest decision first">
          <!-- Copper = "awaiting your decision" only: a staged build-issue proposal awaiting you. -->
          {#each stagedProposals as prop}
            <article class="obj obj--awaiting">
              <div class="obj-tags">
                <span class="pill pill--copper">Waiting on you</span>
                <span class="obj-kind">Staged build issue · from your Ask</span>
              </div>
              <h2 class="obj-title obj-title--sm">{prop.title || 'Build issue'}</h2>
              <p class="obj-meta">
                <span class="dim"
                  >v{prop.version} · <span class="ref">{shortHash(prop.content_hash)}</span> ·
                  staged {ago(prop.staged_at)}</span>
              </p>
              <div class="obj-actions">
                <button class="btn btn--copper" onclick={() => openApproval(prop.conversation_id)}
                  >Review &amp; approve</button>
                <button class="linklike" onclick={() => openConv(prop.conversation_id)}
                  >read the Ask</button>
              </div>
            </article>
          {/each}
          <!-- The verified change lands back on the shelf, then mirrors the coarse pipeline it was
               handed to (building → PR open → in review → merged) and, once merged, its landed
               Acceptance Evidence — all read from the daemon-published projection. -->
          {#each publishedProposals as prop}
            <article class="obj obj--published">
              <div class="obj-tags">
                {#if prop.evidence}
                  <span class="pill pill--ok">Landed ✓</span>
                {:else}
                  <span class="pill pill--teal">Handed to the pipeline</span>
                {/if}
                <span class="obj-kind"
                  >Build issue{#if prop.publication && prop.publication.issue_number}{' · issue #' +
                      prop.publication.issue_number}{/if}</span>
              </div>
              <h2 class="obj-title obj-title--sm">{prop.title || 'Build issue'}</h2>
              {#if prop.pipeline}
                <div class="pipeline">
                  <span class="pipe-item {pipeClass(prop.pipeline)}"
                    ><span>{pipeLabel(prop.pipeline)}</span></span>
                </div>
              {/if}
              {#if prop.evidence}
                <div class="ev-row">
                  <span class="dim">Acceptance Evidence</span>
                  <span class="landed-ev">
                    {#if prop.evidence.pr_number}
                      <span class="ev">merged · PR #{prop.evidence.pr_number}</span>
                    {/if}
                    {#if prop.evidence.merge_commit}
                      <span class="ev">commit {shortSha(prop.evidence.merge_commit)}</span>
                    {/if}
                    {#if prop.evidence.review}
                      <span class="ev">review · {prop.evidence.review.replace('_', ' ')}</span>
                    {/if}
                    {#if prop.evidence.ci}
                      <span class="ev">CI · {prop.evidence.ci}</span>
                    {/if}
                  </span>
                </div>
              {/if}
              <p class="obj-meta">
                <span class="dim">provenance <span class="ref"
                    >{shortHash(prop.content_hash)}</span></span>
              </p>
              <div class="obj-actions">
                {#if prop.pipeline && prop.pipeline.pr_url}
                  <a class="btn btn--teal" href={prop.pipeline.pr_url}
                    >{prop.evidence ? 'See the merged PR ↗' : 'Open the PR ↗'}</a>
                {:else if prop.publication && prop.publication.issue_url}
                  <a class="quiet-link" href={prop.publication.issue_url}
                    >Watch it build in the fleet console ↗</a>
                {/if}
                <button class="linklike" onclick={() => openConv(prop.conversation_id)}
                  >read the Ask</button>
              </div>
            </article>
          {/each}
          {#each conversations as c}
            <article class="obj--ledger">
              <div class="obj-tags">
                {#if c.turn && c.turn.state === 'working'}
                  <span class="pill pill--teal">In a conversation</span>
                {:else if c.turn && c.turn.state === 'paused'}
                  <span class="pill pill--copper">Needs you</span>
                {:else}
                  <span class="pill pill--ghost">Replied</span>
                {/if}
                <span class="obj-kind">Ask · {c.turns_count} {c.turns_count === 1 ? 'turn' : 'turns'}</span>
              </div>
              <h2 class="obj-title obj-title--sm">{c.title || 'Ask'}</h2>
              <div class="turnline {c.turn && c.turn.state === 'working' ? 'turnline--run' : ''}">
                <span class="turnline-body">
                  <span class="turnline-head">
                    {#if c.turn && c.turn.state === 'working'}
                      {c.turn.skill} turn running · {elapsedMin(c.turn.started_at)}m
                    {:else if c.turn && c.turn.state === 'paused'}
                      Turn parked — waiting on you
                    {:else}
                      Turn complete · last turn {ago(c.last_turn_at)}
                    {/if}
                  </span>
                  <span class="dim"> — </span>
                  <button class="linklike" onclick={() => openConv(c.id)}
                    >open the conversation</button>
                </span>
              </div>
            </article>
          {/each}
        </section>
      {/if}
    </div>
  {/if}
</main>

<div id="dock" role="region" aria-label="Ask">
  <div class="dock-inner">
    <div class="dock-row">
      <label class="visually-hidden" for="composer">Ask</label>
      <textarea
        id="composer"
        rows="1"
        placeholder={dockPlaceholder}
        bind:value={composer}
        onkeydown={onComposerKey}></textarea>
      <button class="dock-send" type="button" onclick={sendTurn}>Ask</button>
    </div>
    <p class="dock-hint">{notice || 'Enter sends · Shift+Enter newline · Esc parks'}</p>
  </div>
</div>

<style>
  /* The workspace surface owns its own scroll (the console's global body sets overflow:hidden). */
  :global(body:has(#dock)) {
    overflow: auto;
  }
  #app-main {
    width: 100%;
    max-width: var(--content-max);
    margin: 0 auto;
    padding: 10px var(--gutter) calc(var(--dock-h) + 36px);
  }
</style>
