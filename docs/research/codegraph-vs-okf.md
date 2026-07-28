# Slim codegraph vs codegraph plus an OKF layer

Research for [Wayfinder #5: compare a slim codegraph with a codegraph plus OKF](https://github.com/ConnorGriffin/agentflow/issues/229)
(map [#226](https://github.com/ConnorGriffin/agentflow/issues/226)), captured 2026-07-19.
The question: after removing daemon-worktree pollution from the code index, does a
curated **operational knowledge file (OKF)** layer — domain terms, invariants, ADR
summaries, stage contracts, operational policy — improve spend or quality beyond a
slim live code graph?

Measured under the [spend-per-success contract](spend-per-success-measurement-contract.md)
([ADR 0040](../adr/0040-spend-per-success-measurement-contract.md)) as written:
headroom-denominated spend, tracer-verified outcomes, dollars only for cross-tool
comparison. Operator budget: **no new paid daemon sessions** — historical analysis
and local probes only. The ruling is [ADR 0042](../adr/0042-codegraph-okf-complementary-layer.md).

## Decision: hybrid — keep the slim codegraph, add a small gated OKF layer, never merge them

- **Keep the slim codegraph as-is.** The `.cbmignore` cleanup ([#222](https://github.com/ConnorGriffin/agentflow/issues/222)/#234)
  did its job: the index dropped from 62,416 nodes of stale worktree copies to
  **2,741 nodes / 12,036 edges** of current tracked source, and it is `ready` and
  current. On structural questions it is precise (evidence below). Nothing about
  OKF changes this; OKF is **not** a replacement for symbol / call / data-flow
  analysis and was not evaluated as one.
- **Add a small OKF layer, retrieved separately and capped.** It answers a
  disjoint class of question the graph structurally cannot: *what a term means, what
  rule holds, why a decision was made, what a stage must produce.* Never inject the
  whole bundle (~56.7K tokens); gate retrieval by task shape and cap it to a few
  task-relevant concepts.
- **Never fold OKF into the code graph.** They are different retrieval surfaces with
  different freshness owners. Merging them would re-import exactly the pollution the
  cleanup removed and couple prose freshness to code re-indexing.

This is a recommendation into the map's terminal routing-policy decision, not a
production rollout on its own. It is **directional**, not a quantitative win: the
spend contract's ≥10-sample-per-cell bar cannot be met from history, because the
graph was never in the historical loop (see below). What a forward experiment must
add is stated at the end.

## Why the historical baseline can't answer the spend question directly

The pivotal finding. Across the coordinator's durable session store
(`~/.agentflow/coordinator/sessions`, 282 provider event streams, read-only), the
number of calls to the code graph is **zero**:

- `mcp__codebase-memory-mcp__*` tool calls across all sessions: **0**
  (`search_graph`, `search_code`, `trace_path`, `get_architecture`: none).
- The grounding that *did* happen went through the shell and the editor:
  **3,191 Bash**, 1,079 Read, 651 Edit, 198 Write tool calls.

So the historical daemon era is a **graph-free baseline**. Its spend figures in the
measurement contract reflect agents orienting themselves with `grep`/`cat`/`find`,
not with the graph. There is no historical arm-vs-arm data for "graph only" vs
"graph + OKF" because neither the graph nor an OKF was in the loop. Any spend delta
claim has to come from a forward experiment, not this window.

What history *does* show is the size of the grounding tax the graph is meant to
reduce. Of 5,786 shell commands sampled, orientation/search commands dominate:

| command | count |
|---|---|
| `grep` | 899 |
| `tail` | 640 |
| `ls`   | 392 |
| `head` | 198 |
| `cat`  | 144 |
| `rg`   | 84 |
| `find` | 62 |
| **total orientation** | **≈2,419 (42% of all shell commands)** |

Roughly two in five shell commands are the agent finding its way around, not
changing anything. That is the headroom a precise structural index can compress —
and, separately, the headroom a curated glossary can compress for the *why*/*what-rule*
questions grep answers badly.

## Local grounding probes: what each surface can and cannot answer

Run directly against the cleaned index (no paid session). Representative
Intake-style and Build-style questions, scored on whether the top results ground the
answer correctly.

**The codegraph is strong on structure.** It resolves "where / who-calls / what-is-defined"
precisely:

- *"Parse the review verdict for a target SHA"* → top code hit `parse_verdict`
  (`agentflow/reviewer.py:114`), plus `_review_verdict` and the exact SHA-binding
  tests. Correct.
- *"Admission drains PR-bound stages before issue-bound"* → `admission_demand` /
  `normalize_stage` (`agentflow/coordinator/admission.py`) and the exact test
  `test_cold_pr_bound_review_drains_before_issue_bound_build`. Correct.
- *"Headroom-weighted gate / spend ceiling"* → `ceiling_for`, `_gate_facts`
  (`agentflow/balancer.py`), `decide_merge` (`agentflow/gate.py`). Correct *file*,
  but note the miss below.

**The codegraph is blind to curated knowledge.** The same index cannot serve
definitions, invariants, rationale, or policy — because they are not code structure:

- *"What does the `reviewed` autonomy profile mean — cross-tool review, human merges?"*
  → `search_code` (grep-backed): **0 results**. The answer lives verbatim only in the
  `CONTEXT.md` glossary and `AGENTS.md`, as prose.
- *"Decision map / ubiquitous-language glossary / domain term"* → `search_graph`
  returns only code **symbols** that manipulate maps (`_append_map_decision`,
  `decision_line`), never the glossary **definition** of what a decision map *is*.
  Markdown is indexed structurally (508 `Section` nodes) but definitions are not
  retrievable as answers.
- The headroom probe finds the right file but not the **formula**
  (`input + 1.25×cache_creation + 5×output`) or *why those weights* — that invariant
  and its rationale live in ADR 0025 / ADR 0040 and the measurement contract, not in
  a docstring.

The pattern is consistent and the two surfaces do not overlap: the graph answers
*where the code is*; the OKF answers *what the words mean and which rules hold*. A
builder needs both, and grep serves the second badly (42% of shell commands, and
still a `reviewed`-profile miss).

One more relevant fact: codebase-memory-mcp already exposes a native curated-knowledge
slot (`manage_adr`: PURPOSE / STACK / ARCHITECTURE / PATTERNS / TRADEOFFS /
PHILOSOPHY). For this repo it is **empty**. The OKF layer is not a new mechanism to
build from scratch; the tool already has the shelf, and it is unused.

## The candidate OKF bundle and its retrieval cost

Built from the repo's real curated sources — no invented content:

| source | size | what it grounds |
|---|---|---|
| `CONTEXT.md` (glossary) | 17.7 KB / 289 lines | domain terms: autonomy profile, complexity vs effort vs pool, blocking finding, cross-tool review, decision map, milestone… |
| `docs/adr/` (41 ADRs) | ~203 KB | load-bearing decisions + their rationale (why cross-tool review, why headroom, why PRs drain first) |
| stage contracts (ADR 0028/0030 + `coordinator/tracer.py`) | within the ADRs | what each stage must durably produce to count as done |
| `docs/coordinator-operations.md` | 3.0 KB | operational policy: activate/pause/drain, log-line meanings, the "don't diagnose from the projection" rule |
| `standards/CHARTER.md` | 2.7 KB | the review bar (deep modules, no-jargon PR body, UI-through-mockups) |

**Whole bundle ≈ 56.7K tokens.** Injecting it wholesale every session is a
non-starter — it would dwarf a median build's own working context and inflate exactly
the input/cache-creation terms the headroom formula weights. So OKF, like the graph,
must be **retrieved on demand and capped**, never pasted in full. This is the core
design constraint, not an optimization.

## Retrieval policy

**Gate by task shape.** Retrieve OKF only for the concepts the task actually touches;
default to none.

- **Intake / scope** tasks → pull the glossary entries and ADRs for the terms in the
  issue (autonomy profile, complexity/effort, domain risk, the relevant stage
  contract). This is where a wrong mental model is most expensive downstream.
- **Build / Revise** tasks → pull the invariant or ADR behind the specific area
  being changed (e.g. touching the merge gate → ADR 0004 + the headroom formula from
  ADR 0025/0040; touching admission → ADR 0039). Pull the code *from the graph*, the
  *rule* from OKF.
- **Review** tasks → pull the charter bar and the ADR the diff implicates, so a
  blocking-finding call cites the settled decision instead of re-deriving it.
- **Respond** tasks → usually none; a maintainer reply rarely needs the invariant
  set.

**Per-task concept cap: at most 3–5 concepts.** Retrieval returns the matching
glossary entry or ADR summary (not full ADR bodies unless the task edits that
decision). A concept = one glossary term or one ADR. If a task appears to need more
than ~5, that is a signal the task is under-scoped, not that the cap is wrong.

**Retrieval mechanism.** Prefer the graph tool's existing curated slot
(`manage_adr`) plus concept-keyed lookup of glossary/ADR summaries, so OKF rides the
same MCP surface the builder already has open — no second tool to wire in. Keep it a
*separate query* from the structural graph query; do not blend the two result sets.

## Freshness ownership

OKF is only as good as its currency; a stale invariant is worse than none because it
grounds confidently wrong. Ownership must be explicit and cheap:

- **The ADR/CONTEXT files remain the single source of truth.** OKF is a *projection*
  of them, never a fork. When an ADR lands or `CONTEXT.md` changes, the OKF summary
  for that concept is regenerated from the file — same discipline as the code graph's
  reindex-on-commit hook, on the same trigger.
- **No hand-maintained parallel copy.** The moment OKF text can drift from its source
  ADR, it rots. The projection is derived, timestamped, and staleness is surfaced the
  way ADR 0036 surfaces map-projection age: fresh / stale / unavailable, never a
  silent claim.
- **Owner:** whoever owns the reindex hook owns the OKF regeneration; it is one more
  derived artifact on commit, not a new human chore.

## Migration cost

Low, and bounded:

- **Populate the curated slot once** from `CONTEXT.md` + the ADR index (the
  `manage_adr` PURPOSE/…/PHILOSOPHY doc plus concept-keyed ADR summaries). Mechanical,
  one pass.
- **Add OKF regeneration to the existing commit hook** so summaries follow their
  source files. Reuses the graph's freshness machinery.
- **Teach the stage prompts to issue a gated OKF lookup** by task shape with the
  concept cap. This is the only behavioral change to the pipeline, and it is additive
  — sessions that retrieve nothing behave exactly as today.
- **No index schema change, no merge of the two surfaces, no new store.**

## What a forward experiment must add for a quantitative verdict

This research is directional because the historical window is graph-free and OKF-free.
To earn a "materially reduces spend" claim under ADR 0040, a forward experiment needs:

1. **Both surfaces actually in the loop.** Turn on graph retrieval and gated OKF in
   real sessions; the historical 0 graph calls means today's baseline is still
   grep-grounded.
2. **[#223](https://github.com/ConnorGriffin/agentflow/issues/223) per-attempt
   telemetry** recording, per session: graph tool calls and bytes, OKF concepts
   retrieved and bytes, alongside the contract's headroom/outcome fields — so a
   grounding cost can be attributed to a stage at all (today it cannot; the graph
   leaves no trace in the store).
3. **Three interleaved arms within a cell** (graph-only / graph+gated-retrieval /
   graph+OKF), blinded, ≥10 completed stages per compared cell, per the contract.
4. **The grounding-correctness guardrail made first-class:** a wrong-mental-model
   Intake or a re-derived-instead-of-cited Review finding is a degradation even if it
   is cheaper, because it converts to downstream revise rounds and BLOCK verdicts —
   exactly the guardrails ADR 0040 already gates on.

The prediction the experiment would test: OKF's win concentrates in **Intake** and
**Review** (where the cost of a wrong mental model or a re-litigated decision is
highest), and is near-zero for **Respond**; the graph's win concentrates in **Build**
and **Revise** (structural navigation of a real diff). Hybrid captures both; neither
surface alone does.

## Alternatives rejected

- **Remove/skip OKF, rely on the slim graph alone.** Rejected: the probes show the
  graph structurally cannot answer definition/invariant/rationale/policy questions,
  and grep answers them badly (the 42% orientation tax, and a `reviewed`-profile miss
  on a core term). A slim graph is necessary, not sufficient.
- **OKF as a replacement for the code graph.** Out of scope by the ticket and wrong
  on the evidence: the graph is precise exactly where OKF is silent (symbol/call/data-flow).
- **Inject the whole OKF bundle every session.** Rejected: ~56.7K tokens inflates the
  headroom terms the contract weights and buries the task's own context; retrieval
  must be gated and capped like any other.
- **Merge OKF into the code index (one surface).** Rejected: re-imports the pollution
  the `.cbmignore` cleanup removed and couples prose freshness to code reindexing;
  two surfaces with two freshness owners is the correct seam.
- **Hand-maintain OKF as a separate curated document.** Rejected: it drifts from its
  source ADRs and grounds confidently wrong. OKF must be a derived, timestamped
  projection of `CONTEXT.md`/`docs/adr/`.

## Reproducible method

1. **Graph usage in history:** grep `~/.agentflow/coordinator/sessions/*.events`
   (read-only) for `mcp__codebase-memory-mcp__` tool-use names → 0; tally
   `"name":"<Tool>"` for the grounding-tool mix; tally shell orientation commands from
   the `"command":"…"` fields.
2. **Index health:** `index_status` / `get_architecture` on project
   `<local-project-key>` → 2,741 nodes, ready.
3. **Probes:** `search_graph` / `search_code` for the structural and domain questions
   above; score whether the top results ground the answer.
4. **Bundle size:** `wc -c` over `CONTEXT.md`, `docs/adr/*.md`,
   `docs/coordinator-operations.md`, `standards/CHARTER.md`; tokens ≈ chars/4.
5. **Curated slot:** `manage_adr(action=list)` → empty (the native OKF shelf exists,
   unused).
