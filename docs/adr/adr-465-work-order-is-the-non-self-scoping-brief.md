# ADR 465 — A work order is the brief for a builder that will not self-scope

- Status: Accepted
- Date: 2026-08-02
- Ticket: [#465](https://github.com/ConnorGriffin/agentflow/issues/465)
  (wayfinder map [#463](https://github.com/ConnorGriffin/agentflow/issues/463))
- Constrains: [ADR 0005](0005-spec-rigor-rides-the-dial.md) (work order),
  [ADR 0022](0022-one-build-input-and-the-build-verb.md) (one build input),
  [ADR 0043](0043-recovery-state-before-replay.md) (continuation),
  [ADR 464](adr-464-slice-runs-in-session.md) (slices run in session)

## Context

ADR 464 settled that a coordinated build's slices run as in-session subagents of the
coordinator. It left open what a slice actually *is* — the contract a worker is handed,
who authors it, and how sealed it must be.

The obvious candidate already exists in the glossary. A **work order** is defined as a
frozen hermetic spec used only at `guarded`: grounding pre-done as literals and fixtures,
a file allow-list, and named invariant tests. That is close to what a coordinator would
hand a worker, and map #463 warned against taking it for the wrong reason — at `guarded` it
is a grounding mechanism for high domain risk, not a cost mechanism.

Two facts narrow the choice. First, the work order is not a live artifact: ADR 0022 retired
its *format* — the separate frozen comment, the seeding skill, and the second build-input
path — while explicitly keeping its *substance*, which now rides in the brief. ADR 0017 then
moved the only `guarded` repo off frozen work orders. So nothing is being displaced; the
term names a content standard with no current user. Second, a worker's economics are the
mirror image of a guarded builder's. A guarded builder is forbidden to guess a domain fact
because a plausible-wrong merge is expensive. A coordinated worker is a fresh, narrow
context on a cheap tier, and every turn it spends re-reading the repository is the
re-grounding inflation that map #463 says swings the whole saving to zero at 1.6×. One
must not look; the other cannot afford to. Both need grounding pre-done by someone else.

Timing was the live question, because grounding and slicing rot at different rates. Domain
facts, acceptance literals, and invariant tests survive whatever lands on `main`; a list of
files does not. Measured over the 37 most recent issues that reached a pull request, the
median gap from `ready-for-agent` to the pull request opening is **0.9h**, with a long tail
(p75 7.6h, p90 21h, max 60h). In that gap `main` moved not at all in 18 of 37 windows — but
in 13 of 37 it took three or more commits, and the two worst took 41 and 44 commits touching
108 and 116 files. A file-level plan frozen at triage is therefore stale in roughly a third
of builds and badly wrong in about one in ten.

Continuous integration does not constrain the shape: the gate runs on pull requests and on
pushes to `main`, and a build opens its pull request once at the end, so slice commits held
in the worktree cost nothing and a coordinated build burns exactly one run.

## Decision

**A work order is the form a brief takes when the builder that writes the code will not
self-scope.** One term, one mechanism, two situations that need it: `guarded`, where a
builder *must not* guess a domain fact, and a coordinated build, where workers *cannot
afford to look*. It is never a second build input — it rides in the brief (ADR 0022).

- **Authorship splits by what rots.** Intake writes the durable half at scope time — the
  domain facts as literals, the fixtures, the named invariant tests, and the judgment that
  the work is separable at all. The **slicer** cuts the file-level slice list at pickup,
  against the repository as it actually stands, as the coordinator's first in-session
  subagent. Same duty, later moment: no new stage, no new session, no admission change.
- **Intake writes a slice-bearing work order only for deep *and* separable work.** It may
  decline to slice a deep issue it judges indivisible. The instruction that asks for that
  judgment carries worked examples of sliceable and non-sliceable work drawn from real past
  issues — a hand-curated list, refreshed at the monthly recalibration pass, because an
  instruction without examples decides at random.
- **A slice is sealed for deciding, open for reading.** A worker takes no domain fact and
  no scope choice from anything the work order did not name; it may still read the
  repository freely to write code that matches the house style. An allow-list is a floor
  under its grounding, not a fence around its eyes.
- **A worker is told its own slice, the shared grounding, and one line per finished
  predecessor** saying what that slice produced. Not the whole work order — that re-imports
  the context slicing exists to shrink — and not silence, which would make it re-derive what
  the coordinator already knows.
- **Every finished slice leaves the repository green.** Slices commit locally as ADR 464
  requires; nothing is pushed until the build opens its single pull request.
- **A gap stops the worker, not the build.** A worker that hits something its slice does not
  cover stops and asks the coordinator, and **its session is resumed with the answer, never
  killed and re-dispatched** — re-dispatch would pay for its context twice. The coordinator
  answers when the answer is a repository fact it can verify and parks when the gap is a
  domain or intent fact, and either way the answer is written where a rerun of that issue
  will find it, so the same question is never bought twice. The slicer follows the same rule
  at pickup: it amends a moved file or a rename and records the amendment; it parks the
  issue when the work order's grounding is wrong.
- **The coordinator orchestrates, it does not re-author.** It may merge, split, or reorder
  the slices it was given, but never invent work the order did not name; anything beyond
  that stops the build.
- **A failed slice fails the build.** No pull request opens with a known-unfinished diff.
  The committed slices stay in the worktree and ADR 0043's continuation resumes from them,
  which is what ADR 464 gave as the reason for committing per slice.
- **Self-scope is a property of the session, not of every actor inside it.** A coordinated
  build self-scopes at its slicer — which reads the repository fresh at pickup — and forbids
  its workers to. Coordinated build is therefore available at `reviewed` and `autonomous`
  cells, not only where self-scope is already disallowed.

## Alternatives considered

- **Freeze the slice list at triage with the rest of the work order.** Rejected on the
  measurement above: in 13 of 37 recent windows `main` moved three or more commits before
  the build started, and the worst pair moved 41 and 44 commits across 108 and 116 files. A
  plan that names files must be cut against the tree it will run on.
- **A separate slicing stage between pickup and build.** Rejected. It buys the same fresh
  grounding at the price of another session, another cell, and another admission demand, and
  it breaks ADR 464's finding that coordinated build is a Build adapter change. The
  coordinator's own session can host the duty.
- **Mint a second term for the coordinated spec.** Rejected: two names for one mechanism.
  The difference between `guarded` and coordinated build is *why* the builder does not
  self-scope, not what it is handed.
- **Retire "work order" in favour of a hardened brief.** Rejected: **hardened brief** is
  already taken — it names the draft that ran out of attacker objections (ADR 380) — and
  reusing it would collide two unrelated ideas at the exact point where both matter.
- **Let workers re-ground themselves from a pointer.** Rejected. Re-grounding turns are the
  inflation parameter map #463 identified as the one that can zero the saving, and a fresh
  narrow context is the most expensive place in the system to re-read a repository.
- **Fence the worker's reading as well as its deciding.** Rejected: reading neighbouring code
  to match the house style is not grounding, and forbidding it buys nothing while producing
  code that reads as foreign.
- **Open the pull request with whatever slices landed when one fails.** Rejected: it spends a
  deep cross-tool review — the most expensive stage after build — on a diff already known to
  be unfinished.

## Consequences

- **Intake's output grows for deep separable work,** and intake is all-deep (ADR 0045) at
  13.3% of audited spend. The authoring cost is real and moves spend earlier in the pipeline.
  The dated re-review ([#469](https://github.com/ConnorGriffin/agentflow/issues/469)) must
  read intake spend beside build spend, or a build-stage saving could be an intake-stage cost
  wearing a disguise.
- **Coordinated build gains a candidate pre-dispatch signal** — intake's separability
  judgment is a fact that exists before dispatch. Whether the gate uses it, and which cells
  switch on, remains [#466](https://github.com/ConnorGriffin/agentflow/issues/466)'s decision.
- **A slice's bounded return must carry its one-line summary** for the next slice's context.
  What else it carries is [#468](https://github.com/ConnorGriffin/agentflow/issues/468)'s
  question; that it carries this is settled here.
- **Coordinated build requires resumable workers.** A tool that cannot hand an answer back
  into a running subagent's context cannot honour the gap rule, and its cells cannot switch
  the route on.
- **The gap protocol now has an inner form.** The operator-facing marker is unchanged and
  still the answer for a domain or intent gap; a worker-to-coordinator gap is settled inside
  the session and never reaches the needs-you inbox.
- **`self-scope` is sharpened in `CONTEXT.md`** to mean the session, which is what makes the
  route legal at `reviewed`. A future decision that re-reads it as per-actor would strand
  coordinated build on repos that no longer exist.
