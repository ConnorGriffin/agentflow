# Open-source prior art worth incorporating into agentflow

_Research date: 2026-07-16_

## Executive conclusion

The best prior art does not suggest replacing agentflow's execution substrate. Its
GitHub-native intake, isolated worktrees, cross-tool review, risk-based merge policy,
durable continuations, evidence gate, and daemon-published snapshot are already more
coherent than most open-source orchestrators. The useful gaps are upstream and across
the seams:

1. a typed, versioned **Proposal package** staged from exploration, explicitly approved,
   then published into durable project truth and build tickets;
2. a **method router** that chooses the smallest adequate engineering method rather
   than forcing every idea through one ceremony;
3. explicit **traceability** from intent and decisions through acceptance criteria,
   implementation, review, CI, and visual evidence;
4. a durable **project event/provenance ledger** from which maps, activity, and evidence
   views are derived;
5. separate factual dimensions for session, runtime, PR, and evidence state rather than
   one overloaded status;
6. a ranked, token-bounded repository map for the future **Ask** surface; and
7. a stable feature workspace that groups the Conversation, Proposal, execution, and
   evidence without replacing GitHub as source of truth.

The strongest sources are OpenSpec for Proposal publication, BMAD and Spec Kit for
method selection and quality gates, OpenHands for provenance, Agent Orchestrator for
state modeling, Aider for repository context, and Shep for the user-facing feature
workspace. Borrow these mechanisms, not their products or vocabularies.

## Scope and evidence standard

This is a narrowed implementation-oriented review of seven mechanisms from seven
projects. It uses only first-party repositories, source files, project documentation,
and licenses. Statements under **Verified mechanism** are source facts. Statements under
**Agentflow inference** are recommendations derived from those facts.

The comparison is grounded in two agentflow horizons:

- **Current substrate:** autonomous GitHub issue intake, Agent Briefs, Claude/Codex
  builds, isolated worktrees, cross-tool review, CI and merge gates, autonomy profiles,
  evidence requirements, durable stage continuations, and a daemon-owned fleet snapshot.
  See [ADR 0016](../adr/0016-intake-stage.md), [ADR 0018](../adr/0018-two-dials-review-by-evidence.md),
  [ADR 0026](../adr/0026-daemon-owned-snapshot.md), and the persisted
  [`Record`](../../agentflow/coordinator/record.py).
- **Original seed:** repo-bound project workspaces; an Ask surface that can explore an
  idea or codebase; temporary decision maps; selection among research, grilling,
  modeling, interface design, mockups, and direct implementation; explicit Proposal
  approval and Publication; and a continuous idea-to-landed loop with decision-to-evidence lineage.
  The planning/execution boundary is already recorded in
  [ADR 0027](../adr/0027-wayfinder-planning-boundary.md); the fuller seed is in
  [issue 123](https://github.com/ConnorGriffin/agentflow/issues/123).

## 1. OpenSpec: change packages and explicit Publication

**Sources:** [repository and workflow example](https://github.com/Fission-AI/OpenSpec),
[artifact dependency schema](https://github.com/Fission-AI/OpenSpec/blob/main/schemas/spec-driven/schema.yaml),
[OPSX workflow](https://github.com/Fission-AI/OpenSpec/blob/main/docs/opsx.md),
[license](https://github.com/Fission-AI/OpenSpec/blob/main/LICENSE).

### Verified mechanism

OpenSpec separates current truth from proposed change. Current capability specifications
live under `openspec/specs/`; an active change lives under
`openspec/changes/<change>/` with:

```text
proposal.md
specs/<capability>/spec.md
design.md
tasks.md
```

Its checked-in `spec-driven` schema declares an artifact graph rather than only a prompt
sequence: `proposal` has no prerequisites; `specs` requires `proposal`; optional `design`
requires `proposal`; `tasks` requires `specs` and `design`; apply requires and tracks
`tasks.md`. Capability deltas are typed as `ADDED`, `MODIFIED`, `REMOVED`, or `RENAMED`
requirements. Each requirement has scenarios. Archiving moves the completed change into
an archive and updates current specs. The repository is MIT-licensed.

### What agentflow lacks

Wayfinder currently uses GitHub map and decision issues, then files separate build issues.
Intake turns each issue into a durable Agent Brief. There is no first-class object between
conversation and issue that says:

- which project truth is proposed to change;
- which decisions and artifacts belong to one Proposal version;
- which prerequisites are satisfied;
- whether the Proposal is staged, approved, published, or discarded, and what its
  Publication authorized;
- what durable truth should be updated when the work lands.

### Recommendation: **adapt**

Introduce a small persisted Proposal manifest, but keep Markdown artifacts and GitHub
issues as the human interfaces. A plausible shape is:

```yaml
id: prop_add-dark-mode
project: ConnorGriffin/example
conversation: conv_123
version: 3
state: staged          # staged | approved | published | discarded
method: ui-change
artifacts:
  decisions: []
  research: []
  visual_spec: null
approval:
  approved_at: null
  approved_sha256: null
publication:
  key: null
  target_ref: null
```

Do not copy OpenSpec's specification language wholesale. Agentflow's domain is a mixed
method environment: a bug diagnosis, an interface-design exercise, and a visual mockup do
not all need requirement deltas. Borrow the artifact dependency graph, active-versus-
durable separation, and explicit approval/Publication transition.

**Why it fits:** this supplies the missing persisted object at the exact boundary preserved
by ADR 0027. Publication can produce ordinary standalone GitHub issues, after which existing intake
and execution remain unchanged.

**Sequence and weight:** first among seed work; **medium**. Define Proposal persistence,
versioning, approval, and Publication invariants before building the Project UI or Ask.
Ticket #126 decides the durable store and writer boundary; this prior-art report does not
pre-empt that decision.

**License:** the idea and data shape can be reimplemented freely. Copying MIT code or
templates requires preserving its copyright and permission notice. A clean Python
implementation is preferable because OpenSpec's TypeScript CLI would add an unnecessary
runtime and product vocabulary.

## 2. BMAD Method: route by uncertainty and scale

**Sources:** [workflow map](https://github.com/bmad-code-org/BMAD-METHOD/blob/main/docs/reference/workflow-map.md),
[established-project guidance](https://docs.bmad-method.org/how-to/established-projects/),
[solutioning threshold](https://docs.bmad-method.org/explanation/why-solutioning-matters/),
[repository and license](https://github.com/bmad-code-org/BMAD-METHOD).

### Verified mechanism

BMAD publishes a workflow map with four progressive phases—optional analysis, planning,
solutioning, and implementation—and named artifacts flowing between them. It also has a
parallel Quick Flow for small, understood work that skips phases 1–3 and produces a
technical specification plus code. Its guidance makes solutioning optional for simpler
work and required for complex/enterprise work; `bmad-help` inspects existing project
artifacts and recommends what is next. The project uses a shared `project-context.md` and
is MIT-licensed, while its name and marks are separately protected.

### What agentflow lacks

Agentflow intake currently has three routes—ready, grilling, or mockup—and Wayfinder has
ticket types. The original seed calls for a richer engineering-method palette, but there
is not yet one component that decides whether an ask needs direct intake, diagnosis,
research, decision mapping, domain modeling, interface design, UI mockups, or a full
project-planning path.

### Recommendation: **adapt**

Create a small, declarative method catalog and a router that chooses the least expensive
method capable of resolving the current uncertainty. Route on observable properties, not
story counts or agent personas:

```yaml
id: ui-change
when:
  user_surface: true
requires: [grounding, visual-spec]
may_add: [research, decision-map]
publishes_when: [intent-settled, visual-spec-locked]
handoff: agentflow-intake
```

The essential BMAD lesson is **variable ceremony**. Preserve direct issue intake for
clear work; use a map only when decisions can invalidate multiple downstream changes;
insert research or design only when their uncertainty is present.

Do not import BMAD's full persona/phase system. Agentflow already has strong skills and a
planning boundary; the router should compose them, not recreate a second methodology.

**Why it fits:** method selection is central to the seed and can become agentflow's
distinctive upstream capability. It also protects the current fleet from planning every
small bug like a greenfield product.

**Sequence and weight:** after the Proposal contract; **medium**. Methods need a common
contract for inputs, artifacts, completion conditions, and Publication blockers. Begin with
four routes proven by the repository: direct intake, research, Wayfinder, and UI mockups.
Add others only after a second real use.

**License:** BMAD is MIT, so code/templates can be reused with notice. Prefer borrowing the
routing concept and writing agentflow-native definitions. Do not reuse BMAD branding or
present agentflow routes as BMAD roles; the project explicitly reserves trademarks.

## 3. GitHub Spec Kit: traceable artifacts and executable quality gates

**Sources:** [repository workflow and customization stack](https://github.com/github/spec-kit),
[spec template](https://github.com/github/spec-kit/blob/main/templates/spec-template.md),
[plan template](https://github.com/github/spec-kit/blob/main/templates/plan-template.md),
[tasks template](https://github.com/github/spec-kit/blob/main/templates/tasks-template.md),
[constitution template](https://github.com/github/spec-kit/blob/main/templates/constitution-template.md),
[license](https://github.com/github/spec-kit/blob/main/LICENSE).

### Verified mechanism

Spec Kit materializes a chain of repo artifacts: constitution → feature spec → plan →
tasks → implementation. The spec template requires prioritized, independently testable
user stories, Given/When/Then acceptance scenarios, functional requirements, edge cases,
and measurable outcomes. The plan links back to the spec and contains a **Constitution
Check** that must pass before research and be rechecked after design. Tasks are organized
by user story so each slice can be implemented and tested independently. Optional
`clarify`, `analyze`, and `checklist` commands examine underspecification and
cross-artifact consistency before implementation.

Spec Kit also resolves templates through a precedence stack: project-local overrides,
presets, extensions, then core. It can convert task artifacts into GitHub issues. The
project is MIT-licensed.

### What agentflow lacks

Agentflow's Agent Brief and review are deliberately anchored to acceptance criteria, and
the Charter is enforced during review. But the system does not persist links at the level
of decision → requirement/scenario → handoff issue → PR → review finding → CI or screenshot
evidence. Charter enforcement is primarily downstream; a Proposal can reach approval
without a machine-readable preflight showing that every acceptance criterion is testable
and every required artifact exists.

### Recommendation: **adapt**

Add a Publication preflight and traceability index, not Spec Kit's entire document suite.
Each published handoff should carry stable criterion IDs, provenance links, and required
evidence kinds:

```yaml
criteria:
  - id: AC-1
    text: Operator can see why Proposal publication is blocked
    decided_by: decision-42
    build_issue: 180
    evidence_required: [test, screenshot]
```

Before approval and Publication, validate that no open decision invalidates the handoff, all required locked
artifacts exist, criteria are observable, and load-bearing rulings are durable. At review,
attach actual evidence to those same criterion IDs. This is the continuous lineage the
seed wants.

The template precedence stack is also useful later for method definitions: agentflow core
defaults, repository overrides, then optional installed packs. Do not implement a catalog
or marketplace until at least one external method pack exists.

**Why it fits:** this deepens agentflow's existing acceptance-anchored review and screenshot
gate instead of introducing a parallel spec product.

**Sequence and weight:** criterion IDs and Publication preflight after Proposal persistence;
**small–medium**. Full cross-artifact analysis is **medium** and can follow. Method-template
overrides are later and require a real second method pack.

**License:** MIT permits code/template reuse with notice. The templates are easy to imitate
conceptually; agentflow should keep its concise Agent Brief rather than copy a large generic
template that conflicts with decisive intake.

## 4. OpenHands SDK: append-only, branchable provenance

**Sources:** [`EventLog` implementation](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/event_store.py),
[`ConversationState` persistence and recovery](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/state.py),
[license](https://github.com/OpenHands/software-agent-sdk/blob/main/LICENSE).

### Verified mechanism

The OpenHands SDK persists conversation events separately from a base state snapshot.
`EventLog` stores typed events with stable IDs, builds ID↔index maps, rejects duplicate
event IDs, locks concurrent appends, and can reconstruct a branch from a leaf to the root
using `parent_id`. Legacy events without parents are treated as a linear chain. The
conversation state can create or resume from a `FileStore`, rebuild its derived view from
events, verify tool compatibility, detect action events with no matching observation, and
persist environment observations separately. The SDK is MIT-licensed.

### What agentflow lacks

Agentflow has two useful but disconnected durable shapes:

- coordinator records capture operational continuation state; and
- GitHub issues/PRs plus the dashboard snapshot capture durable outcomes and current view.

There is no Project-level provenance ledger connecting an Ask turn, a decision-map branch,
a Proposal revision, approval, Publication, build issue, PR, review, CI result, and visual
evidence. Reconstructing that chain requires chasing comments and conventions.

### Recommendation: **adapt**

Add a narrow append-only Project event log with typed references, then derive map activity
and Proposal history from it. Do not event-source the daemon or duplicate GitHub entities.
Events should record transitions and provenance links, while GitHub remains authoritative
for issues, PRs, reviews, and merges:

```json
{
  "id": "evt_...",
  "type": "proposal.published",
  "project": "ConnorGriffin/example",
  "subject": "proposal/prop_add-dark-mode/v3",
  "parent": "evt_...",
  "at": "...",
  "refs": {"issues": [180], "visual_spec": "mockups/dark-mode.html"}
}
```

The branchable parent relation is especially relevant to temporary decision maps: a rejected
route remains inspectable without polluting the active path. Unmatched intent/action pairs
also inspire a useful invariant: a dispatch request without a durable resulting issue/PR or
hold is incomplete, just as agentflow already treats process exit without a stage outcome.

**Why it fits:** it supplies lineage across planning and execution while respecting ADR
0026's derived snapshot and GitHub-as-truth architecture.

**Sequence and weight:** after Proposal identity and event vocabulary; **medium–large**.
Start with an append-only JSONL or SQLite table written by one process. The dashboard reads
only the daemon's derived projection. Add branching only when Ask/map revisions need it.

**License:** MIT permits direct reuse with notice, but OpenHands' Pydantic/FileStore engine
is much broader than agentflow needs. Reimplement the small event contract; do not import
the SDK merely for storage.

## 5. Agent Orchestrator: facts first, display status derived

**Sources:** [canonical lifecycle source](https://github.com/AgentWrapper/agent-orchestrator/blob/main/packages/core/src/lifecycle-state.ts),
[provider/plugin interfaces](https://github.com/AgentWrapper/agent-orchestrator/blob/main/packages/core/src/types.ts),
[architecture notes](https://github.com/AgentWrapper/agent-orchestrator/blob/main/CLAUDE.md),
[license](https://github.com/AgentWrapper/agent-orchestrator/blob/main/LICENSE).

### Verified mechanism

The project formerly under ComposioHQ now redirects to `AgentWrapper/agent-orchestrator`.
Its versioned canonical lifecycle stores three related but separate dimensions:

- session state/reason (`working`, `idle`, `needs_input`, `stuck`, `done`, etc.);
- PR state/reason (`none`, `open`, `merged`; `ci_failing`, `review_pending`,
  `changes_requested`, `merge_ready`, etc.); and
- runtime state/reason (`unknown`, `alive`, `exited`, `missing`, `probe_failed`).

The source validates the payload with Zod and derives a legacy/display status on read;
the derived status is explicitly not persisted. Provider plugins expose observations such
as process liveness, activity, session information, restore command, and workspace hooks;
core lifecycle policy interprets them. The repository is Apache-2.0 licensed.

### What agentflow lacks

Agentflow already centralizes operational state in its coordinator and derives the dashboard
snapshot. However, the snapshot still infers user-facing stage from branch names, labels,
comments, and PR state in several places. As the seed adds Project/Proposal/map state, one
`status` field would become ambiguous: `awaiting decision`, `agent interrupted`, `PR waiting
review`, and `missing screenshot` describe different dimensions.

### Recommendation: **adopt the rule; adapt the schema**

Persist facts once and derive projections. Add orthogonal planning/evidence dimensions to
agentflow's own vocabulary rather than adopting Agent Orchestrator's states:

```text
proposal: staged | approved | published | discarded
publication: not_started | pending | unknown | verified
execution: queued | running | held | completed
delivery: no_pr | open | reviewed | merged | closed
evidence: unknown | incomplete | complete | invalid
```

Keep reasons separate from states and retain timestamps/source refs. The control-plane
snapshot should be a pure projection of GitHub facts, coordinator records, and the project
ledger—not another writable state machine. Provider adapters should continue extracting
facts; policy remains in agentflow core, matching ADR 0030.

**Why it fits:** this is a direct deepening of agentflow's durable coordinator and snapshot.
It prevents future project UI labels from becoming hidden policy inputs.

**Sequence and weight:** **small** if applied as a domain rule before project-state code;
**medium** if current snapshot fields are migrated. Do it before adding project workspace
screens so the UI does not hard-code an overloaded status.

**License:** Apache-2.0 permits reuse with attribution, license/NOTICE compliance, and
patent terms. The state concepts are simple enough to reimplement. Do not copy its large
TypeScript interfaces into the Python core, and do not create a generic provider plugin
system while Claude and Codex remain the only two real adapters.

## 6. Aider: ranked, token-budgeted repository maps

**Sources:** [`RepoMap` implementation](https://github.com/Aider-AI/aider/blob/main/aider/repomap.py),
[license](https://github.com/Aider-AI/aider/blob/main/LICENSE.txt).

### Verified mechanism

Aider's `RepoMap` extracts definition/reference tags with tree-sitter, falls back to
Pygments names where references are unavailable, and caches tags. It builds a directed
weighted graph from referring files to defining files. PageRank is personalized toward
files and identifiers mentioned in the conversation; reference frequency is damped and
mentions receive additional weight. Ranked definitions are rendered through syntax-tree
context, and a binary search selects the largest rendered map that fits the configured
token budget. Aider is Apache-2.0 licensed.

### What agentflow lacks

Intake and agents read repositories directly, but the future Ask surface needs a stable,
cheap way to answer exploratory questions and ground method routing before launching a
full deep session. A raw file tree or broad grep wastes context; a full external code graph
is powerful but conflicts with the open-source release's self-contained boundary if it is
mandatory.

### Recommendation: **adapt behind a narrow context-provider interface**

For Ask, produce a token-budgeted repository orientation map biased by the current question.
The output should be evidence, not project truth: file/symbol pointers with commit SHA and
refresh time. Begin with built-in text/symbol search and make ranked graph context optional;
adopt Aider's ranking only when measurements show the simple map misses relevant context.

Do not copy the whole `RepoMap`: it brings tree-sitter grammars, NetworkX, diskcache,
Pygments, and rendering machinery. Agentflow currently prefers stdlib tooling, and Ask must
degrade cleanly when no index exists. The deep interface should be closer to:

```python
context = provider.map(repo, query, token_budget)
```

with one implementation until a second real provider exists.

**Why it fits:** this directly serves Ask and method selection while leaving build/review
sessions free to explore the repository themselves.

**Sequence and weight:** after Ask has a concrete interaction contract; **medium** for a
simple map, **large** for Aider-equivalent ranking and language coverage. Measure retrieval
quality before taking the dependency cost.

**License:** Apache-2.0 permits code reuse with attribution/NOTICE and carries a patent
grant. Reimplementing the algorithm avoids importing Aider's dependency stack; copying
substantial code requires preserving Apache notices.

## 7. Shep: one feature workspace spanning spec, worktree, CI, and PR

**Sources:** [first-party product and data-shape documentation](https://shep.bot/),
[documentation index and architecture references](https://shep-ai.github.io/shep/),
[repository](https://github.com/shep-ai/shep).

### Verified mechanism

Shep is local-first and keeps state in SQLite under `~/.shep/`. A feature gets its own
branch, worktree, and agent session. In spec-driven mode, a `spec.yaml` contains a feature
name/number, one-line intent, requirements, tasks, and an approval gate. The same feature
workspace then follows implementation, commit/push, draft PR, CI watching and bounded
auto-fix, and optional merge. Worktrees are preserved until explicit cleanup. The project
and first-party site identify the code as MIT-licensed.

### What agentflow lacks

Agentflow has nearly all of this execution machinery, and its safety/review model is
stronger. What it lacks is the stable **workspace identity and presentation** across the
whole loop. Today a map issue, handoff issue, worktree, PR, review comment, CI run, and
screenshots are related by conventions but not presented as one Project/Proposal surface.

### Recommendation: **adapt only the workspace projection; reject the execution model**

Make the Project page and its active Conversation or Proposal the console's organizing
unit. That page can project:

```text
intent / current decisions / active method / Publication readiness
handoff issues / running sessions / PRs / reviews / CI / visual evidence
```

Every item remains a link to its authority: repo artifact, GitHub issue/PR/check, or local
coordinator record. Do not create a Shep-like shadow task runner or merge setting. Reuse
agentflow's autonomy profile, cross-tool review, continuation coordinator, and screenshot
gate.

**Why it fits:** Shep demonstrates the user value of grouping the loop, while agentflow's
seed extends that grouping upstream into Ask and decision maps. This is the control-panel
shape the seed describes.

**Sequence and weight:** last of the seven; **medium–large UI work**. It depends on Proposal
identity, factual projections, provenance links, and a locked `/ui-mockups` spec. Extend the
daemon-owned snapshot; never let the web server query GitHub directly.

**License:** MIT allows code reuse with notice, but there is little reason to copy UI or
orchestrator code. Borrow the workspace composition. Keep agentflow's visual identity and
domain language.

## Ranked incorporation shortlist

| Rank | Incorporation | Source pattern | Decision | Weight | Prerequisites |
|---:|---|---|---|---|---|
| 1 | Typed Proposal package with explicit approval/Publication | OpenSpec | Adapt now | Medium | Settled Proposal vocabulary and authority ADR |
| 2 | Orthogonal factual states with derived projections | Agent Orchestrator | Adopt rule now | Small–medium | Project-state vocabulary |
| 3 | Method router choosing the smallest adequate workflow | BMAD | Adapt | Medium | Proposal contract; 4 proven methods |
| 4 | Criterion IDs, Publication preflight, and evidence traceability | Spec Kit | Adapt | Small–medium | Proposal manifest; evidence refs |
| 5 | Append-only provenance linking decisions to landed evidence | OpenHands SDK | Adapt narrowly | Medium–large | Stable Project/Proposal/event IDs |
| 6 | Token-bounded, query-biased repo map for Ask | Aider | Adapt after Ask contract | Medium–large | Ask UX and retrieval benchmark |
| 7 | Project/Proposal workspace projection in the console | Shep | Adapt UI only | Medium–large | Items 1–5; locked UI mockup |

### Suggested sequence

1. Use the settled `Project`, `Conversation`, `Proposal`, `Publication`, `Build handoff`,
   and `Acceptance evidence` terms; record their authority and persistence boundary in an ADR.
2. Implement Proposal persistence and a read-only projection. Publication creates standalone
   GitHub issues and records their refs; existing intake owns everything downstream.
3. Split Proposal/Publication/execution/delivery/evidence facts and reasons before exposing them in UI.
4. Add four method definitions and deterministic completion/Publication checks.
5. Add stable acceptance-criterion IDs and attach PR/review/CI/screenshot evidence to them.
6. Add the provenance log only when multiple Proposal revisions or Ask branches need
   reconstruction.
7. Design the project workspace through `/ui-mockups`; add repo mapping when Ask itself is
   implemented and retrieval can be measured.

## Attractive ideas agentflow should explicitly avoid

### A universal spec ceremony

OpenSpec and Spec Kit are valuable because their artifacts are explicit, not because every
change needs all of them. Requiring proposal/spec/design/tasks for a one-line bug would
contradict decisive intake and BMAD's strongest lesson: route by uncertainty and scale.

### A second task tracker or writable control-plane database

Shep and other orchestrators use local SQLite as product truth. Agentflow should not mirror
GitHub issues, PRs, reviews, and CI into an independently writable task system. Keep GitHub
authoritative, the coordinator's SQLite private to continuation/admission, and the daemon
snapshot derived and disposable.

### Event-sourcing the execution engine

OpenHands' event log is useful for project provenance. Rebuilding the coordinator as a full
event-sourced system would add recovery and migration complexity without solving the seed.
Record Conversation/Proposal/Publication/evidence lineage; leave stage transitions in the existing
versioned `Record` store.

### A generic provider/plugin platform now

Agent Orchestrator's adapter surface is justified by many agents and runtimes. Agentflow has
two real providers with meaningful cross-tool semantics. Follow the Charter's rule: two
adapters establish the current seam; do not design a marketplace-grade interface until a
third provider exposes a genuinely different requirement.

### Importing Aider's repository-map dependency stack before a benchmark

The algorithm is good prior art; the dependencies are not automatically justified. Define
Ask's retrieval benchmark first. A simple built-in map may be sufficient, and external
indexers should remain optional for a self-contained release.

### BMAD personas and duplicate lifecycle language

Agentflow needs method contracts, not role-play agents, sprint vocabulary, or a second
implementation pipeline. Keep the existing ubiquitous language and use skills as methods.

### Taskmaster code or templates

[Taskmaster](https://github.com/eyaltoledano/claude-task-master) has an attractive durable
task/dependency model and a useful `next` operation, but its repository states that it is
MIT **with Commons Clause**, prohibiting hosted/competing offerings. That is source-available,
not a safe open-source code base for agentflow's intended official hosted option. Borrow only
the general DAG idea already represented by GitHub native dependencies and Wayfinder; do not
copy its code or templates.

## Bottom line

Agentflow should not become a bundle of other projects. Its current advantage is the
governed execution loop. The highest-leverage extension is to make the upstream and
cross-cutting state equally explicit:

> Ask explores against a bounded repository map; a method router creates the smallest
> adequate set of temporary artifacts; explicit approval and Publication turn a resolved Proposal
> into standalone GitHub work; the existing fleet executes it; and one provenance chain
> returns independently reviewed, criterion-linked evidence to the project workspace.

OpenSpec supplies the Publication grammar, BMAD the routing principle, Spec Kit the
traceability gates, OpenHands the provenance shape, Agent Orchestrator the state discipline,
Aider the context-ranking mechanism, and Shep the workspace composition. That combination
advances the original seed without weakening agentflow's existing GitHub-native core.
