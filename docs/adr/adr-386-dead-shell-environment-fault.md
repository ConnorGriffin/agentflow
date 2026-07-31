# ADR 386 — A dead shell is an environment fault, not a spent budget

- Status: Accepted
- Date: 2026-07-30
- Follows: [0050](0050-bounded-worktree-retention.md), whose final consequence names this as
  deferred work: bounding retention removed the precondition but left the misclassification.
- Amends: [0028](0028-stage-scoped-continuations.md) (the attempt budget gains another refunding
  ending, and the first one that refunds *and* holds); extends
  [0030](0030-session-coordinator-seam.md)'s provider seam with a fact that is about the machine
  rather than the provider

## Context

A session whose shell cannot be started never reaches the work. It cannot run a test, reach
`git` or `gh`, or post a comment explaining itself. The pipeline drew only one distinction for
a session that ended without producing its outcome — the agent finished without delivering —
so all four sessions on the outage that produced ADR 0050 were recorded as *continuation budget
exhausted*. That is true and actively misleading: it reads as "tried three times and could not
do the work", so the maintainer who opens the issue starts by questioning the scope. Three of
that issue's three attempts were spent on a condition no attempt could have affected, and
diagnosing it took a human session.

Two things produced the misclassification. The coordinator stamps the literal reason
`continuation budget exhausted` when a stage runs out of continuations. And the comment a
maintainer actually reads branched on exactly two cases — an integration collision, or
everything else — so a build held for a permanent provider condition, or held because a replay
would have been identical, *already* claimed it had run out of tries. Intake had grown a
per-condition diagnosis for its own holds (issue #342's four fixed bodies); build never did.

Separately, we teach every spawned session that a rejected command should be adjusted rather
than repeated. Against an adjustable rejection that is right. Against a shell that cannot start
it is exactly the instruction that produced 3, 25, 11 and 11 wasted retries.

## Decision

**A shell that never started is its own ending.** It is recognized from facts already persisted
in the Claude session stream: each shell tool result is correlated back to its own tool-use
block, and the ending is claimed only when *every* shell result is the harness's exec-level
start-failure line — that is, no command in the session ever ran. Two independent anchors, so
nothing is diagnosed from loose keyword matching, which
`docs/research/provider-interruption-signals.md` forbids.

**It is carried as a permanent *reason*, not a new cause.** The observation's category is read
from `cause` alone by contract, so a new cause would move every branch that reads the
classification table. A dead shell is therefore a permanent condition — a human has to act,
nothing lifts on its own — whose reason says the environment, not the provider, was what
failed. A provider that reported a typed condition of its own keeps it; the environment fault
only claims endings that would otherwise read as an ordinary incomplete, timed-out, or unknown
session.

**The attempt is refunded and the stage holds immediately.** Nothing about the work was
attempted, so the attempt is given back through the same committed-flag discipline the capacity
pause already uses — a hold re-observed after a crash cannot refund twice. Unlike a capacity
pause it does *not* requeue: a quota window rolls over on its own, and an unusable machine does
not. A stage held on its first attempt is held with none of its three consumed, so a maintainer
resume starts from a full budget.

**The maintainer-facing comment names the fault and its remedy.** Intake gains a fifth fixed
body: the machine could not give the agent a working command line, nothing is waiting on a
decision, and the remedy is to reclaim the repository's leftover session checkouts. Build's
hold comment gains the branches it never had — an environment fault and each kind of permanent
provider condition now read as themselves, and a genuinely spent budget is unchanged.

**What the comment says is decoupled from what its post-once marker keys on.** The marker is a
hash of the record identity plus a status string, and a held record recomposes it every cycle,
so improving the wording would have made every already-held issue look unheld and post a second
comment on deploy. The marker keys on a frozen status — exactly the two strings it has always
used — while the displayed status is chosen separately.

**The session is told to stop.** The shell crib now opens with the one exception to "adjust the
command": a shell that cannot start at all is not adjustable, no other command shape will pass,
and the session should report it and stop rather than trying variants.

## Alternatives

**Wait for the reclamation sweep to cure it instead of holding.** Rejected. A capacity pause
waits because the provider itself declared a reset; here nothing has promised to fix anything.
Reclamation only runs while dispatch is enabled and can only reach agentflow's own checkouts —
on the machine that forced this, a large share of registrations are foreign and unreachable. A
free wait would also be a free retry loop, which is the failure this replaces.

**A new provider cause rather than a permanent reason.** Rejected: the classification table is
the contract every branch reads, and this ending wants precisely the existing permanent
behavior (hold, no continuation) with different copy.

**Recognize a Codex dead shell too.** Not done. The Codex exec JSON surface carries no typed
tool-result fact to correlate a refusal back to a shell call, and its prose never diagnoses
(ADR 0030). Inventing a prose matcher there would be exactly the thing the interruption-signals
research rules out. Codex sessions keep today's classification; when the adapter moves to the
app-server surface, this is one of the facts to revisit.

## Consequences

- A repository whose environment cannot carry a session now stops spending that issue's
  attempts on it. The issue still holds for a human — this makes the hold honest, it does not
  make the work happen.
- The durable hold reason keeps the `permanent provider condition` prefix with an `environment`
  suffix, because that prefix is the predicate both stage handoffs use to tell an
  infrastructure failure from a hold the model reasoned its way into. The prefix is now
  slightly wider than its name: not every condition behind it is the provider's.
- Detection depends on the harness's start-failure line keeping a recognizable shape. If it
  changes, the ending degrades to today's behavior — an ordinary incomplete session — rather
  than misfiring.
- A build already held under the old wording keeps its original comment. The marker still
  matches, so it is not re-commented; only new holds read as themselves.
