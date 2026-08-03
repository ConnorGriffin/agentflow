# agentflow — glossary

The ubiquitous language for the autonomous issue → PR → review pipeline. Glossary
only — no implementation details, no decisions (those are in `docs/adr/`).

## Terms

- **Agentflow** — the headless workflow engine that moves approved GitHub build
  issues through intake, dispatch, build, review, and merge. It does not own
  planning conversations, issue tracking, or repository decisions.

- **Wayfinder** — the chat-invoked planning capability that explores uncertainty
  before agentflow intake. It records multi-session efforts as GitHub decision maps
  and hands cleared work off as ordinary build issues; it is not part of the
  agentflow runtime or operator console.

- **Decision map** — the durable record of a bounded, temporary deciding effort
  around one desired outcome. It contains decision tickets and their dependencies,
  and closes once no unresolved decision blocks downstream work; an independently
  cleared branch may produce build issues before the whole map closes. A map may
  produce standalone build issues or create or reshape one or more milestones. Its
  states are `active`, `resolved`, and `abandoned`; a resolved map reopens only when
  the same outcome encounters new blocking uncertainty before its standalone work
  lands or its affected milestones are achieved.
  *Avoid:* trail, roadmap, plan.

- **Decision ticket** — one bounded question or prerequisite inside a decision map.
  It is resolved in its own agent session — operator-driven for judgment tickets,
  or daemon-dispatched and unattended for AFK-able research — with the whole map
  loaded as context. Its states are `open`, `resolved`, and `discarded`; `blocked`
  is derived from open prerequisite tickets rather than stored as a state.
  Resolution requires a durable answer and required outputs, but the ticket itself
  never enters the build pipeline.

- **AFK-able ticket** — a decision ticket an unattended agent session can finish
  alone (`wayfinder:research`), whether it reads the repository or external
  sources. The only ticket type the daemon may dispatch; every other type needs
  the operator in the loop. The label is a promise of AFK-ability, applied
  honestly.
  *Avoid:* inward/outward research (source location is not the axis).

- **Ticket claim** — the `wayfinder:resolving` label marking a decision ticket in
  progress by any session, human or daemon; one signal for both, released on
  completion or by daemon crash recovery.
  *Avoid:* assignment-as-claim (the daemon shares the operator's GitHub identity).

- **Parked research ticket** — an AFK-able ticket an unattended session ended without
  producing a ruling the daemon may record, either by spending its whole budget on the
  question or by being stopped before it read the question at all. It is handed back in
  the open — commented with what stopped the run and labelled so no later unattended run
  picks it up — and the operator either rewrites the question, repairs what stopped the
  session, or answers it in a session. Distinct from a ticket awaiting disposition, where
  the research succeeded and left candidates to choose among.
  *Avoid:* failed research (the question is not judged, only handed back).

- **Build issue** — one operator-approved, independently buildable GitHub issue that
  enters intake. It may be filed directly from chat or handed off from a cleared
  decision map, and belongs to at most one milestone.

- **Artifact provenance** — links from a durable artifact to any related decision
  ticket, decision map, milestone, or visual specification. Provenance provides
  traceability; the artifact must still stand alone without following those links.

- **Build handoff** — filing an operator-approved build issue in a form eligible for
  agentflow intake. There is no second approval inside agentflow.

- **Dispatch** — the scheduler launching an eligible pipeline stage when capacity
  permits. Dispatch is not a second operator approval after build handoff.

- **Landed change** — a build issue whose pull request has merged into the default
  branch. Landing does not imply that the change was released, deployed, or shipped
  to users.
  *Avoid:* shipped change, released change.

- **Milestone** — a durable, observable product outcome for one repository, delivered
  through one or more build issues. It is achieved only when acceptance evidence
  demonstrates the outcome, not merely when its issues close. Its states are `planned`,
  `active`, `achieved`, and `abandoned`; it becomes active when its first build issue
  enters the pipeline. Achievement requires operator acceptance of the evidence.
  *Avoid:* sprint, deadline, release bucket, phase.

- **Mockup** — one disposable interactive candidate used to explore a user-interface
  direction during chat planning, optionally belonging to a decision ticket. Selecting a
  mockup promotes it into a visual specification; unselected variants remain
  exploration history.

- **Visual specification** — an approved mockup together with its required behavior
  and acceptance notes for one product outcome. One or more implementing build issues
  reference the same specification and accumulate acceptance evidence against it.

- **Acceptance evidence** — durable, inspectable proof that a build issue or milestone
  meets its stated acceptance criteria. It may include tests, CI results, screenshots
  against a visual specification, or captured behavior; an agent's assertion is not
  evidence.

- **Pipeline** — the end-to-end path a unit of work travels: build issue → triage/scope
  → build → PR → review → merge. One pipeline serves every repo; behavior varies
  only by the repo's autonomy profile.

- **Autonomy profile** — a per-repo dial governing how much an unwatched agent is
  trusted. It sets grounding rigor, review mode, and merge policy together. One of
  three levels:
  - **`autonomous`** — agent self-scopes, builds, and enters cross-tool review until
    the other tool makes no changes; it auto-merges only on green CI, a clean exact-head
    review, and no same-tool taint. (Vibe-code / low domain risk.)
  - **`reviewed`** — agent builds, gets cross-tool review when available or an explicit same-tool
    fallback summary, then a human glances and merges. The default. (Most repos.)
  - **`guarded`** — mandatory real-data grounding, Full dual/human review, human merges.
    (Safety-critical projects.)

- **Domain risk** — the cost of a *plausible-but-wrong merge* in a given repo. The
  durable constraint that a smarter model does not erase; it, not tool identity,
  sets a repo's position on the autonomy dial. High in safety-critical projects,
  low in a vibe-code project.

- **Complexity** — the per-issue builder model size intake stamps as a hard gate:
  `standard` (sonnet/Terra) or `deep` (correctness-sensitive — opus/Sol).
  Tool-agnostic; each runner resolves it to its tool's model. Orthogonal to the pool:
  pool = *which plan* (by headroom), complexity = *how big a builder model within
  it*. Reviewers do not inherit this dial; every review uses the deep tier.
  (Supersedes the earlier single `tier` dial; `light`/haiku dropped — ADR 0018.)

- **Effort** — the second dial intake stamps alongside complexity: `low | medium |
  high | extra` — how much work the issue warrants, independent of model size. It
  also configures the **builder's** provider reasoning effort (`extra` maps to the
  ladder's Extra High and clamps there; Max/Ultracode stay manual-only — ADR 0046).
  Revise inherits the original builder's effort. Non-build stages take
  provider-default reasoning (tunable cells, ADR 0044/0046).

- **Runner** — the interchangeable executor that performs a pipeline stage:
  Claude (Opus) or Codex (GPT-5.6 Sol). Chosen per stage by cost / availability /
  preference, **not** by a capability ceiling — both are full-loop capable.

- **Builder** — the runner that implements an issue and opens the PR. Self-reviews
  and flags uncertainties, but its own sign-off never gates a merge.

- **Reviewer** — the runner that verifies a PR and may ship clear grounded fixes on its
  branch. A reviewer never approves its own changed head; another tool must inspect that
  exact pushed state before completion.

- **Review depth** — the verification scope assigned from a change's complexity and stakes,
  proposed by its author and only escalated later: **Focused** for exact housekeeping and
  evidence changes, **Targeted** for one contained behavior or journey, and **Full** for
  connected behavior, sensitive information, permissions, safety, or competing product
  decisions. Small is not necessarily low-stakes. Full starts with separate product-outcome
  and project-standards passes.

- **Review action** — the disposition of a grounded review observation: **fix before
  completion**, **file a necessary follow-up**, **ask the maintainer**, or **discard as
  unsupported reviewer preference**. A necessary follow-up is outside the PR's purpose and
  carries evidence, a desired outcome, and verified duplicate search. “Nit” is retired.

- **Cross-tool review** — a review performed by a different tool than the author of the
  current change set (Codex→Claude or Claude→Codex). Independence follows every pushed
  change, not only the original builder: review/fix passes ping-pong until the other tool
  makes no change. Three consecutive change-making passes park as drift/disagreement.
  Autonomous work waits without consuming capacity when the other tool is unavailable.

- **Same-tool taint** — the human-merge-only state created when a maintainer confirms a
  forced same-tool review. It prevents auto-merge until the other tool returns and cleanly
  reviews the open PR. Reviewed repositories may use same-tool immediately during an outage,
  but their final summary says “same-tool review; maintainer merge required.”

- **Review handoff** — private durable state passed to the next reviewer: the current PR,
  exact changes since the previous review, what changed and why, assigned depth, completed
  proof/checks, and unresolved concerns. Intermediate agents do not comment on GitHub.

- **Conflict revise** — bounded work that reconciles a finished PR with newly moved `main`.
  Preserve both intended outcomes when compatible; when they compete, neither side wins because it
  is newer and the choice enters the private second-opinion path. Every genuinely new conflict gets
  its own stage budget—there is no PR-lifetime count—and the resulting choice receives Focused,
  Targeted, or Full review.

- **Conflict decision handoff** — the one private, narrow handoff to the other tool after a
  resolver records two genuinely ambiguous product options, exact missing guidance, and a
  recommendation. If the second tool is also unsure, agentflow parks once with the precise
  maintainer decision needed; there is no intermediate PR comment.

- **PR-bound stage** — a stage whose subject is an open PR (review, revise,
  respond). Admission drains PR-bound work before issue-bound work (build,
  mockup, intake): an open PR is the first thing to get over the finish line
  (ADR 0039). Interactive turns still outrank everything (ADR 0034).

- **Review park** — the single public handoff when the chain cannot finish safely. It has a
  **Maintainer decision needed** section in application behavior and an **Agent handoff**
  section with code locations, conflicting changes, checks, retained work, and the exact
  next action. Intermediate review agents remain silent.

- **Head check gate** — the third unwaivable mechanical gate, alongside cross-tool review
  and screenshot evidence: before a review may finish clean, the checks reported on the
  *exact reviewed commit* are read from GitHub, never from the branch tip and never from
  the verdict. A red check opens a revise round rather than clearing; pending, absent,
  skipped, and cancelled checks change nothing; an unreadable answer defers only the clean
  settlement. A reviewer cannot clear it by not looking.
  *Avoid:* CI gate (the merge-time CI wait is a different, older thing).

- **Brief** — at `autonomous`/`reviewed`, the spec a builder starts from: the issue
  itself (acceptance criteria + file pointers). The builder self-scopes from it.

- **Self-scope** — a *session* reading the repo and grounding against real data to
  decide its own touch-set and approach, instead of being handed a frozen spec.
  Trusted at `autonomous`/`reviewed`; disallowed for *domain facts* at `guarded`.
  A property of the session, not of every actor inside it: a coordinated build
  self-scopes at its slicer and forbids its workers to (ADR 465).

- **Work order** — the form a brief takes when the builder that writes the code will
  *not* self-scope: grounding pre-done as literals and fixtures, named invariant tests,
  and the files the work is expected to touch. Two situations need one — `guarded`,
  where a builder *must not* guess a domain fact, and a coordinated build, where workers
  *cannot afford to look*. Not a per-tool cage, and never a second build input: it rides
  in the brief (ADR 0022, ADR 465).

- **Gap protocol** — a builder that hits an unstated domain fact, threshold, or fixture
  stops rather than guessing (a plausible-wrong guess is the expensive failure). At
  `guarded` it posts a marker for the operator. Inside a coordinated build it has an
  inner form: the worker stops and asks its coordinator, which answers a repo fact it can
  verify and parks a domain or intent fact — the operator only ever sees the second kind.

- **Coordinated build** — a build whose deep **coordinator** delegates the issue's slices
  to cheaper in-session workers and lands them on one pull request, which gets today's
  unchanged single review. A route a build may take, off by default and switched per cell
  (ADR 464).

- **Slice** — one worker's portion of a coordinated build: a stated outcome, the files it
  is expected to touch, the grounding it needs, and the test that says it is done. Sealed
  for *deciding* (no domain fact or scope choice comes from outside it) and open for
  *reading* (it may read around to match the house style). Committed green before the next
  slice starts.

- **Slicer** — the duty that cuts a work order into slices at pickup, against the repo as
  it actually stands, as the coordinator's first in-session worker. Intake decides whether
  work is separable at all and grounds it; the slicer decides where the cuts fall, because
  a list of files goes stale between scope time and build time and grounding does not.

- **Pool / headroom** — each prepaid plan (Claude, Codex) is a *pool* of rate-limit
  capacity. A pool can report multiple windows, such as a 300-minute window and a
  weekly window; reported windows may appear or disappear as plan limits change.
  *Headroom* is the unspent remainder. The scarce resource the scheduler optimizes
  — cost is not, since both plans are flat-rate. Idle headroom while work is queued
  is wasted sunk cost.

- **Capacity permit / permit budget / admission demand** — a capacity permit is one unit
  of concurrent demand within a pool; the pool's *permit budget* is the fixed number it can
  lend at once, and a session's *admission demand* is the permits it reserves until it ends.
  Permits prevent simultaneous sessions from racing on the same headroom fact; they are not
  a measure of spent headroom.
  *Avoid:* points (which does not name what is being bounded).

- **Admission matrix** — the reviewed mapping from a session's stage, model, complexity,
  and effort to its admission demand. It is calibrated from completed session history and
  remains static while the fleet runs.
  *Avoid:* adaptive scheduler, learned weights.

- **Two-pool load balancer** — the scheduler that assigns builds to keep both pools
  maximally utilized in parallel: builder → the pool with more headroom, reviewer →
  the other tool/pool. Never leaves a prepaid plan idle while work is queued.

- **Floodgates** — an operator emergency override (ADR 0025 amendment) that lifts the
  paced weekly allowance and raises the spend ceiling to 100 for a pool, fleet-wide (env
  `AGENTFLOW_FLOODGATES` or the `agentflow floodgates open`/`close` flag file) or scoped to
  one dispatch/record. Never touches the hard permit ledger — it widens *how much of the
  window* may be spent, not how many sessions may run at once.

- **Machine ceiling / per-stage caps** — the fleet runs many sessions at once, but bounded:
  the *machine ceiling* is the most agent sessions that may run concurrently (of any kind),
  and each kind has its own *cap* — triage is allowed more parallelism than builds, since
  grounding sessions are short and cheap and a deep intake queue should drain fast. Merges
  are the exception: they stay serialized so two never land at the same instant (ADR 0009).

- **Activity-adaptive ceiling** — the daemon yields to a live operator instead of stopping
  (ADR 0025). When the operator is working interactively on a pool, the daemon's spend
  ceiling for that pool drops (≈50% instead of ≈85%) and new sessions on it are *paced* to
  one per cycle; the other pool keeps running full. Already-running sessions finish; nothing
  is killed. The ceiling ramps back up on its own as the operator goes idle.

- **Intake** — the autonomous stage every new build issue passes through (fires on any open
  issue with **no state label**, except upstream `wayfinder:*` planning artifacts): it
  grounds the request (reads code + a read-only data
  pull if the repo declares one), rewrites the title/description, stamps the dials, and
  routes to one outcome — `ready-for-agent`, `needs-mockup`, or `needs-grilling`. Not a
  tollbooth: it scopes anything it can pin down confidently and holds only an
  *outcome-changing* fork it can't settle from code/data (ADR 0016). A `ready` decision is
  a *draft*, not a publication: it must survive its attack rounds before the issue ever
  changes (ADR 380).

- **Draft** — the brief a triage round hands back instead of publishing (ADR 380). Nothing
  on GitHub changes while a draft exists: the maintainer never reads one, and a draft that
  never survives its attackers was never anything anyone had to un-read.
  *Avoid:* brief (reserved for the published artifact).

- **Attack** — one cold session asked to break a draft before it is published: it carries
  nothing from the session that wrote it, reads only the *newest* draft against the actual
  repository, and answers with numbered objections — each with its evidence, why it breaks
  the build if unfixed, and the cheapest fix — or with none, which is a draft surviving,
  not an attacker slacking. Runs at the draft's own complexity dial: a standard brief gets
  one round on the standard tier, a deep one up to three on the deep tier. Taste is not an
  objection. *Avoid:* plan audit, pre-build review (both suggest judging a published plan).

- **Redraft** — the fresh triage round that answers an attacker: it re-grounds from the
  same issue snapshot, fixes what landed, and defends what didn't under an
  `## Answered objections` heading *inside* the brief — the only place a settlement can
  live, since the next attacker reads the draft and nothing else. A redraft may still
  route to grilling when the objections expose a fork only the maintainer can settle.

- **Hardened brief** — the draft that ran out of objections, published as the ready brief
  through the ordinary intake projection, with one line saying what the argument cost. This
  is the only way a brief reaches `ready-for-agent` from the daemon's triage. A draft that
  runs out of *rounds* still contested is never published — it becomes a held issue whose
  question is the surviving objections. *Avoid:* countersigned (there is no marker; the
  publication itself is the proof).

- **Held issue** — an issue parked at `needs-grilling` or `needs-mockup`: inert to
  agents until its missing input is durably supplied. It remains a build issue;
  the operator resolves the hold in chat and updates the issue, or uses a decision
  map when several dependent decisions are required. No builder touches a held issue.

- **Recoverable interruption** — a pipeline session ending because of a temporary
  capacity or execution condition, rather than because the work itself cannot proceed.
  Rate limits, session timeouts, CLI crashes, and transient provider/network failures are
  recoverable interruptions; a fresh runner may continue the same owned work.
  *Avoid:* build failure, error (both blur temporary interruption with a real hold).

- **Continuation** — a fresh runner attempt that carries a pipeline stage forward from
  its durable state after a recoverable interruption. Worktree-owning stages continue
  their existing changes; read-only stages restart from their durable source state.
  *Avoid:* retry (which suggests discarding the earlier attempt and starting over).

- **Recovery envelope** — the bounded durable facts a continuation hands its fresh
  session so it resumes rather than replays: the attempt number, the missing outcome, and
  the retained worktree path. Never the prior session's event stream. A continuation with
  no envelope would just re-run the identical prompt (ADR 0043).

- **Targeted repair** — the single continuation a read-only stage (such as intake) earns
  after a clean exit that produced no outcome. Its envelope names the exact missing proof.
  A read-only stage owns no partial work, so beyond one repair a fresh session would replay
  identically — so it parks for a human instead (ADR 0043).
  *Avoid:* retry, replay (both imply re-running the same empty attempt).

- **Stage outcome** — the durable fact a pipeline stage must produce before the pipeline
  can advance, such as an intake route, opened PR, or review verdict. A clean process exit
  without that fact is incomplete, not success; a human hold is a separate terminal handoff.

- **Continuation budget** — the bounded allowance of fresh runner attempts available to
  one logical stage after its initial attempt. A later stage receives its own budget; an
  exhausted budget turns the current stage into a human hold.

- **Tool lineage** — the runner identity retained across every code-writing attempt on
  one change. A continuation stays in its original Claude or Codex lineage so the other
  tool remains independent for cross-tool review. After a reviewer pushes, a later stage
  starts from the PR's current pushed state; only the same interrupted task may retain local work.
  *Avoid:* current runner, last runner (both lose the change's authorship history).

- **Bail** — a deliberate runner stop because continuing would require guessing missing
  intent, expanding scope, or crossing an integration collision. A bail needs a human
  decision and is not a recoverable interruption.

- **Grounding fetch** — a per-repo, **read-only** pull of real data intake runs to check
  facts before scoping (ciq: `ciq-pull-db` → `ciq.readonly.db`). Declared once in the
  repo's config; run on-demand, skipped for issues already crisp (ADR 0016).

- **Decide-then-review** — the pipeline's default posture: a stage makes its best
  decision and *stages it under a review gate* instead of asking the human up front.
  Emits an answer, not a question. Only undecidable *intent*-gaps punt to grilling.

- **Trust ratchet (graduated autonomy)** — a repo starts conservative (gates on,
  decisions reviewed) and is loosened toward autonomy as its staged decisions are
  consistently confirmed without correction. Earned, deliberate, per-repo,
  reversible. The autonomy profile is the *current* setting; the ratchet moves it.

- **Operator console** — agentflow's read-only console for the fleet, sitting *over* GitHub
  (the source of truth), not replacing it. Reads GitHub + scheduler state; shows
  fleet overview, two-pool headroom, the needs-you inbox, a recently-merged audit
  feed, ratchet state, and read-only decision maps derived from GitHub's native
  child and dependency relationships. Actions deep-link to their authoritative
  GitHub, chat, or CLI surface; the console performs no mutations.

- **Repository view** — the operator console's derived view of one enrolled
  repository: its decision maps and current frontier, build issues moving through
  the pipeline, blockers, landed evidence, and contextual ADR links. It excludes
  the repository's general backlog and stores no repository or planning state of
  its own.

- **Snapshot** — the one fleet-wide view the operator console shows: dispatch state, pool
  headroom, running sessions, and every repo's queue/in-flight/parked/merged state.
  Produced only by the daemon under a bounded GitHub API budget; the console serves
  the latest projection and shows its age honestly — it never asks GitHub itself
  (ADR 0026). With the daemon down you see the last snapshot, aged, not an error.

- **Needs-you inbox** — the operator's action list: `guarded` merges awaiting, review parks,
  and intent-gap grillings. The same set ntfy pings.

- **Charter** — the canonical engineering-standards file (`standards/CHARTER.md`)
  every app in the flow must meet: deep-module architecture, UI→`/ui-mockups`,
  test-through-the-interface, maintainability. It applies machine-wide and is
  enforced at cross-review through grounded fix-before-completion or maintainer-decision actions.

- **Hazard** — an *environmental* obstacle to autonomous work: PHI/real data,
  live credentials, a demo that needs a running app. Historically fenced work off
  to a specific tool; now treated as agent-handleable and captured in per-repo
  config, not in routing.
