# README and documentation split audit

Status: research artifact for [#610](https://github.com/ConnorGriffin/agentflow/issues/610).
This is a structure recommendation, not a rewrite of `README.md` or any product document.

## Audit boundary

- Baseline: exact local `origin/main` worktree, `HEAD` = `origin/main` =
  `5fa5e867ffde8326a94d9cb2db55e24df8500f0f`, clean at audit start.
- Repository sources only: `README.md`, checked-in Markdown/JSON, Python/Svelte source,
  tests, `CONTEXT.md`, `PRODUCT.md`, `DESIGN.md`, `AGENTS.md`, and ADRs.
- README size: 344 lines, one H1, 14 H2 headings, and 5 H3 headings.
- Documentation tree: 112 Markdown files; 84 ADR/research-history records under `docs/adr`,
  22 research notes under `docs/research`, 20 evidence JSON fixtures/contracts, 75 screenshot
  artifacts, and the small operational homes `docs/capabilities.md`,
  `docs/coordinator-operations.md`, `docs/evidence/README.md`, and `docs/public-beta.md`.
- The tree has no single current architecture/pipeline guide. `CONTEXT.md` is glossary-only;
  `docs/adr/` is the decision/rationale authority; research notes are evidence for decisions,
  not a current-user manual.

## Compact current-state inventory

| Area | Current authority | README relationship | Audit result |
| --- | --- | --- | --- |
| Product boundary and mental model | `CONTEXT.md`, ADR 0027, ADR 0035 | Inline in “The mental model” and Wayfinder section | Keep a short front-door version; point to the authorities. |
| Intake/routes and pipeline stages | `agentflow/intake.py`, `agentflow/routing.py`, `agentflow/pipeline.py`, coordinator stage modules, ADRs 0016/0028/0030/0039/0047 | Inline in “Issue intake” and “How the engine works” | Move the explanatory contract to a new `docs/pipeline.md`; update from merged behavior only. |
| Profiles and merge/review policy | ADRs 0001–0008, 0018, 0020, 0047; `CONTEXT.md` | Inline in “Autonomy profiles” | Keep a compact decision summary; link to ADR index and glossary. |
| Wayfinder boundary | ADRs 0027/0037; Wayfinder skill; `CONTEXT.md` | Inline in two README sections | Keep the one-paragraph boundary and pointer; do not duplicate tracker mechanics. |
| Skills/capabilities | `agentflow/capabilities.toml`, `agentflow/provider_skills.py`, `docs/capabilities.md`, enrolled skill | Inline in “How skills are invoked” and “Repository capabilities” | Move setup/generated-contract detail to existing capabilities doc; keep one short conceptual pointer. |
| Installation/enrollment/recovery | `agentflow/cli.py`, `enroll.py`, `repo_facts.py`, `docs/capabilities.md`, ADR 0049 | Large “Quick start” plus recovery section | Move to a new `docs/getting-started.md`; README retains a minimal path. |
| Service, pause/drain, diagnosis, rollback | `agentflow/macos_service.py`, `daemon.py`, `pool_control.py`, `docs/coordinator-operations.md`, ADR 0051 | Split between Start, notifications, and Operations | Existing operations doc is the authority; README keeps commands and links only. |
| Evidence contracts | `agentflow/evidence*.py`, `docs/evidence/README.md`, contract JSON, ADRs 580/581/596 | Not yet described coherently in README | Existing evidence index is the authority; link it from the future pipeline/learning section. |
| Public beta/policy/support | `docs/public-beta.md`, `COMPATIBILITY.md`, `SECURITY.md`, `SUPPORT.md`, `GOVERNANCE.md`, LICENSE/NOTICE | README “Project policy” repeats boundary facts | Keep a concise policy link list; detailed promises stay in their existing homes. |
| UI/console | `agentflow/webui/`, `agentflow/webapp.py`, `PRODUCT.md`, `DESIGN.md`, ADRs 0023/0026/0048 | Mentioned in mental model, current state, enrollment | README states read-only projection only; UI behavior/design stays in product/design/ADR sources. |
| Learning pipeline future surface | `agentflow/evidence_pipeline.py` and related modules plus ADR/issue records | Absent or only implied by current evidence wording | Do not invent a guide now. Reserve a later `docs/learning-pipeline.md` only after the merged contract exists. |

## README heading disposition

Actions mean: **keep** = remains concise in README; **update in-place** = remains but must be
corrected against shipped behavior; **move** = README gets a short pointer and the named document
owns the explanation. “New” names are proposals only; this audit does not create them.

| README heading | Action | Canonical destination | Reason |
| --- | --- | --- | --- |
| `# AgentFlow` | Keep | README | Front door, badges, one-sentence product identity. |
| `## The mental model` | Update in-place | `README.md` | Keep the durable boundary and projection statement; link `CONTEXT.md` for terms and ADR 0027/0035 for rationale. |
| `## Issue intake and routing` | Move | New `docs/pipeline.md` | A route table is a behavior contract; the guide links code and ADR 0016/0022 instead of duplicating them in README. |
| `## How the engine works an issue` | Move | New `docs/pipeline.md` | The seven-stage summary belongs with lifecycle semantics and recovery rules, with stage-code and ADR links as evidence. |
| `## Autonomy profiles` | Update in-place | `README.md` | Keep the user choice summary; link `CONTEXT.md` for terms and ADR 0001/0002/0008/0018 for rationale. |
| `## Wayfinder and AgentFlow` | Update in-place | `README.md` | Keep only the planning-vs-execution boundary and one handoff rule; ADR 0027/0037 remain the linked rationale. |
| `## How skills are invoked` | Move | `docs/capabilities.md` | Capability discovery and unattended availability are operational/generated facts owned by the manifest and explained here. |
| `## Where the project is today` | Update in-place | `README.md` | Keep only status proved by the merged revision and explicitly labelled open boundaries; link the map rather than restating it. |
| `## Quick start` | Move | New `docs/getting-started.md` | Requirements, install, enroll, capacity, start, and recovery form one runnable procedure. |
| `### Requirements` | Move | New `docs/getting-started.md` | Version/runtime prerequisites need one maintained home. |
| `### Install` | Move | New `docs/getting-started.md` | Clone and dependency setup belong to onboarding. |
| `### Enroll a repository` | Move | New `docs/getting-started.md` | Getting-started owns the enrollment procedure and links to `docs/capabilities.md` for generated-contract details. |
| `### Calibrate provider capacity` | Move | New `docs/getting-started.md` | Getting-started owns first calibration; it links to `docs/coordinator-operations.md` for later diagnosis without repeating that procedure. |
| `### Start` | Move | Existing `docs/coordinator-operations.md` | Service install, pause/resume, pool controls, paths, and restart semantics are operations. |
| `## Repository capabilities` | Move | Existing `docs/capabilities.md` | This document already explains enrollment, generated skills, and optional integrations. |
| `## Recover on a new machine` | Move | New `docs/getting-started.md` | It is the second onboarding path and should share prerequisites/enrollment instructions. |
| `## Foreground notifications` | Move | Existing `docs/coordinator-operations.md` | Environment/service inheritance and sensitive notification configuration are operating rules. |
| `## Operations and development` | Move and split | `docs/coordinator-operations.md` for operating behavior; `CONTRIBUTING.md` for development | The mixed heading becomes two README pointers. Each target owns one non-overlapping reader task. |
| `## Support AgentFlow` | Keep | `README.md` | Small sponsorship entry remains at the front door and points to `SUPPORT.md` for the full policy. |
| `## Project policy` | Update in-place | `README.md` | Keep only the clone-only beta sentence and exact policy links; detailed promises stay in their linked files. |

## Exact proposed smaller doc set

This is the target set of user-facing entry points, not a proposal to flatten the historical
tree:

1. `README.md` — product identity, mental model, one-paragraph boundary, tiny start path,
   current shipped status, and links to every entry-point document below.
2. **New `docs/pipeline.md`** — current issue-to-PR lifecycle, route meanings, stage completion,
   review/revise/merge boundaries, recovery, and the GitHub/repository authority split. It must
   be written from merged code and ADRs after the deferred pipeline checklist is settled.
3. **New `docs/getting-started.md`** — requirements, install, enrollment, capacity calibration,
   first start, pause-before-maintenance, and new-machine recovery.
4. `docs/capabilities.md` — generated repository contract, skills, UI runtime, optional tools,
   doctor/check, and capability drift.
5. `docs/coordinator-operations.md` — service lifecycle, observe/diagnose, pause/drain, upgrade,
   rollback, notifications, and Wayfinder holds/parks.
6. `docs/evidence/README.md` — normative evidence contract index and version namespaces; later
   link to the merged learning-pipeline contract without copying it.
7. **Deferred new `docs/learning-pipeline.md`** — only if the merged Evidence/evaluation/
   promotion/containment/executor/scheduler/self-healing behavior warrants a user-facing guide.
   Until then, issue/ADR records and `docs/evidence/README.md` remain the source index.
8. `docs/public-beta.md`, `COMPATIBILITY.md`, `SECURITY.md`, `SUPPORT.md`, `GOVERNANCE.md`,
   `CONTRIBUTING.md`, `CONTEXT.md`, `PRODUCT.md`, `DESIGN.md`, and `docs/adr/README.md` remain
   specialized authorities linked from the front door rather than merged into a mega-guide.

Historical `docs/research/*`, `docs/adr/*`, evidence fixtures, and screenshots remain searchable
evidence/history. They should not all be promoted to README navigation; the ADR index and evidence
index are the controlled gateways.

The rewritten README uses these exact pointer labels and targets:

| README pointer | Exact target |
| --- | --- |
| Get started | `docs/getting-started.md` |
| Understand the pipeline | `docs/pipeline.md` |
| Operate AgentFlow | `docs/coordinator-operations.md` |
| Repository capabilities | `docs/capabilities.md` |
| Evidence contracts | `docs/evidence/README.md` |
| Learning pipeline | `docs/learning-pipeline.md`, added only when that deferred guide lands |
| Contribute | `CONTRIBUTING.md` |
| Product and design | `PRODUCT.md` and `DESIGN.md` |
| Domain glossary | `CONTEXT.md` |
| Decisions | `docs/adr/README.md` |
| Policy and support | `docs/public-beta.md`, `COMPATIBILITY.md`, `SECURITY.md`, `SUPPORT.md`, and `GOVERNANCE.md` |

## One-authority and pointer rules

1. README is an index and first read, not a second specification. It may state a boundary in one
   or two sentences and must link to the authority for commands, schemas, values, or guarantees.
2. `CONTEXT.md` owns domain terms and “avoid” vocabulary. Other docs use those terms and link
   back; they do not redefine them.
3. ADRs own load-bearing decisions, alternatives, and consequences. A guide may summarize the
   ruling and link to the ADR, but must not restate the rationale or create a competing rule.
4. Python source, checked-in manifests, JSON contracts, and tests own executable behavior. Docs
   describe observed behavior and link to the relevant source/test; prose never upgrades a
   hypothesis or ADR proposal into a fact.
5. `agentflow/capabilities.toml` owns generated-capability facts and pins;
   `docs/capabilities.md` explains that manifest and its workflows; `docs/coordinator-operations.md`
   owns service/operator procedures; `docs/evidence/README.md` and its manifests own evidence
   wire contracts; `docs/public-beta.md` owns beta promises and publication/rollback boundaries.
6. Product and design language stays in `PRODUCT.md` and `DESIGN.md`; the README may link to the
   console but must not become a UI specification.
7. Research notes preserve findings, limits, and rejected alternatives. They are not current
   operation manuals unless a merged ADR or code path makes the fact current; label inferences and
   deferred proposals explicitly.
8. Every extracted README section gets one canonical link target. The README pointer should be
   one sentence or a link list, not a shortened duplicate of the target section.
9. When a fact changes, update the authority first, then repair pointers and links in the same
   change. Do not patch a stale README summary alone.
10. Learning-pipeline documents must not describe Evidence, evaluation, promotion, containment,
    executor, scheduler, briefing, or self-healing behavior until the corresponding behavior is
    merged and testable on the audited branch.

## Deferred-content checklist: fill only from merged behavior

The later documentation rewrite must fill each row with exact shipped interfaces, state
transitions, authority, failure handling, and tests. A blank row is intentional; do not infer it
from issue bodies, draft ADRs, research recommendations, or future-map language.

| Deferred area | Required merged-behavior facts before documenting |
| --- | --- |
| Evidence | Event/envelope types and versions; immutable subject/revision identity; redaction and bounded retention; producer/consumer boundaries; authoritative source and public tests. |
| Evaluation | Input manifest/corpus, scoring, missingness, adjudication, eligibility, reproducibility, and the exact persisted outcome. |
| Promotion | Approval authority, exact revision/hash binding, idempotent receipt, policy-version semantics, and rollback/rejection behavior. |
| Briefing | What is projected, bounds, freshness, lineage links, unavailable state, and explicit non-authority/status rules. |
| Containment | The actually allowed deterministic rerun, route-cell quarantine, canary rollback, triggers, bounds, audit record, and release conditions. |
| Executor | Isolation of source/config/cache, sealed inputs/outputs, transport, cleanup, failure classification, and no-leak tests. |
| Scheduler | Pairing/order/admission rules, permits, retries/waits, idempotency, crash replay, and the durable record that proves completion. |
| Service operation | Install/start/pause/resume/drain/upgrade/rollback semantics, launch paths, logs, notifications, health/hold diagnosis, and restart behavior. |
| Self-healing pipeline | The complete merged sequence from evidence through evaluation, nomination, approval, containment/promotion, canary observation, rollback, and operator-visible outcome; no silent prompt/skill/routing/autonomy mutation. |

For every row, the rewrite should add source links to code, tests, and the governing ADR, state what
is intentionally unsupported, and avoid claiming a capability merely because a module, issue, or
research note exists.

## Rejected alternatives

- **Keep growing one README:** rejected; it would continue mixing onboarding, operations,
  architecture, policy, and future learning behavior under competing update cadences.
- **Create one giant `docs/architecture.md`:** rejected; it would duplicate ADR rationale and
  make commands, schemas, and operational procedures shallow pointers in a second manual.
- **Move every README paragraph into separate micro-docs:** rejected; it creates navigation tax
  and duplicate facts. The proposed set groups by reader task and existing authority.
- **Document the full learning loop now from map/issue plans:** rejected; the ticket explicitly
  defers description until merged behavior, and current repository sources do not prove one
  complete self-healing contract.
- **Treat research notes as the public manual:** rejected; they contain limits, hypotheses, and
  historical alternatives, while current behavior belongs to code, tests, manifests, and ADRs.
