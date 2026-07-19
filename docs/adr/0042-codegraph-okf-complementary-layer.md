# ADR 0042 — The OKF is a complementary curated layer, not a code-graph replacement

- Status: Accepted
- Date: 2026-07-19
- Complements: [ADR 0036](0036-bounded-repository-map-projection.md) (derived,
  timestamped projections with honest staleness),
  [ADR 0040](0040-spend-per-success-measurement-contract.md) (how any spend claim is
  measured)
- Evidence: [slim codegraph vs codegraph plus OKF](../research/codegraph-vs-okf.md),
  wayfinder ticket [#229](https://github.com/ConnorGriffin/agentflow/issues/229)
  (map [#226](https://github.com/ConnorGriffin/agentflow/issues/226))

## Context

Map #226 asks, after the `.cbmignore` cleanup ([#222](https://github.com/ConnorGriffin/agentflow/issues/222)/#234)
shrank the code index from 62,416 stale nodes to 2,741 current ones, whether a
curated operational-knowledge (OKF) layer earns its place beside the slim live code
graph. The answer must not treat OKF as a replacement for symbol / call / data-flow
analysis, and must respect the operator's no-new-paid-sessions budget.

Local probes against the cleaned index show the two surfaces answer disjoint
questions: the graph resolves *where the code is* precisely (`parse_verdict`,
`admission_demand`, the merge gate) but is structurally silent on *what a term
means, what rule holds, and why* — a query for the `reviewed` autonomy profile
returns zero from the graph; domain-term queries return code symbols, never the
glossary definition. The historical daemon era is graph-free (0 code-graph calls in
282 sessions; ~42% of shell commands are grep/find orientation), so no
arm-vs-arm spend delta exists in history — the decision is directional.

## Decision

- **Hybrid.** Keep the slim codegraph as-is; add a small OKF layer beside it; never
  merge the two surfaces.
- **OKF is a complementary curated layer**, not a replacement for the code graph. It
  grounds domain terms, invariants, ADR rationale, stage contracts, and operational
  policy — the class of question the graph cannot serve.
- **Gate retrieval by task shape and cap it** to at most 3–5 task-relevant concepts;
  never inject the whole bundle (~56.7K tokens). Intake/scope and Review pull the
  most; Respond usually pulls none. OKF is queried separately from the structural
  graph query and the result sets are not blended.
- **OKF is a derived projection of `CONTEXT.md` and `docs/adr/`**, regenerated on the
  same commit trigger as the code-graph reindex, timestamped, with staleness surfaced
  (fresh / stale / unavailable) the way ADR 0036 surfaces projection age. No
  hand-maintained parallel copy. Prefer the existing `manage_adr` curated slot, which
  is present but empty for this repo.
- **The claim is directional, not quantitative.** A "materially reduces spend" verdict
  under ADR 0040 waits on a forward experiment that puts both surfaces in the loop,
  captures graph/OKF retrieval cost via [#223](https://github.com/ConnorGriffin/agentflow/issues/223)
  per-attempt telemetry, and runs three interleaved arms (graph-only /
  graph+gated-retrieval / graph+OKF) at ≥10 stages per cell.

## Alternatives considered

- **Slim graph alone, no OKF.** Rejected: the graph cannot answer
  definition/invariant/rationale/policy questions and grep answers them badly.
- **OKF replaces the code graph.** Rejected and out of scope: the graph is precise
  exactly where OKF is silent.
- **Inject the whole OKF bundle every session.** Rejected: it inflates the headroom
  terms ADR 0040 weights and buries the task's own context.
- **Merge OKF into the code index.** Rejected: re-imports the pollution the cleanup
  removed and couples prose freshness to code reindexing.
- **Hand-maintained OKF document.** Rejected: drifts from its source ADRs and grounds
  confidently wrong.

## Consequences

- Populate the `manage_adr` curated slot once from `CONTEXT.md` + the ADR index, add
  OKF regeneration to the existing reindex-on-commit hook, and teach stage prompts to
  issue a gated, capped OKF lookup by task shape. Additive: sessions that retrieve
  nothing behave as today.
- No index schema change, no second store, no merge of the two surfaces.
- The forward experiment feeds map #226's terminal routing-policy decision; its
  grounding-correctness guardrail (wrong mental model, re-litigated decision) is
  measured as a degradation under ADR 0040 even when cheaper.
