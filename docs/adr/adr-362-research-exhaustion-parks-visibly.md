# ADR 362 — Research exhaustion parks the ticket visibly instead of releasing it in silence

- Status: Accepted
- Date: 2026-07-30
- Amends: [ADR 0037](0037-daemon-dispatch-of-afk-research.md)

## Context

[ADR 0037](0037-daemon-dispatch-of-afk-research.md) said a research run whose findings
carry no usable disposition stays "within the research stage's recovery budget," and left
the end of that budget as a claim release: drop `wayfinder:resolving` and the ticket is
"simply eligible again next cycle." Two things turned out to be wrong about that.

**It says nothing to anyone.** Since the disposition contract began failing closed on any
drift, rejection is *total and repeatable* — the same artifact fails the same check every
time. A ticket whose question an unattended session cannot answer in the required shape
therefore burns a whole run and leaves the ticket looking untouched: open, still typed
`wayfinder:research`, no comment, no label, nothing in the map. The maintainer has no way
to tell "not tried yet" from "tried and could not rule."

**And the ticket is never researched again anyway.** The promise of re-eligibility was
never kept. A research run's identity is stable across cycles, and an exhausted run leaves
a terminal held record under that identity, so the next cycle's submission resolves to the
same finished record and creates nothing to run. Research dispatch was the one claiming
path that never inspected the record it had just submitted — and it stamped the claim
*before* submitting — so every cycle re-claimed a ticket for a session that would not
start, logged a submission that had not happened, and had that claim stripped an hour
later by orphan reconciliation and restamped on the next pass. The steady state was an
hourly flap around a ticket nobody would ever look at.

## Decision

**Exhaustion is the research stage's own operator-facing handoff.** When an unattended run
spends its whole recovery budget without recording a ruling the contract accepts, the
daemon writes one durable park:

- **One comment** on the ticket, carrying the research disclaimer and its own dedup marker,
  that says the unattended run could not produce a usable ruling, **names the specific check
  that refused it** — no `## Disposition` section, more than one, a section that was not a
  single fenced JSON object, unexpected keys for the claimed kind, a summary that was too
  short or too vague, a trigger or verification that named nothing observable — or that
  nothing was recorded at all, and reproduces whatever findings text the run did write so
  the evidence is not lost.
- **One park label**, `wayfinder:parked`, that takes the ticket out of unattended selection.
  It is deliberately distinct from `wayfinder:awaiting-disposition`, which means the
  opposite: research succeeded and produced candidates for the operator to choose among.
- **The shared claim released**, the ticket left open, and the run's isolated worktree
  removed — nothing will resume it.

The comment states a machine limit, never a verdict on the question, and the daemon still
files, closes, judges, and re-words nothing (ADR 0037). It says plainly that unattended
research will not retry the ticket: the maintainer either rewrites the question so a bounded
session can answer it, or answers it in a human wayfinder session.

**The park withholds its proof** until comment, label, and released claim can all be re-read
as durable, exactly as durable resolution does, so an interrupted park replays rather than
being recorded as done. Running it twice produces one comment and one label.
Its bookkeeping follows [ADR 0042's convergent write-placement rule](0042-durable-handoff-envelope.md#consequences).

**The comment's story comes from the run's durable hold reason.** Exhaustion is the ordinary
way a research run ends up here, but it is not the only one: a permanent provider condition —
a refused sign-in, a request the provider rejected, a configured spend ceiling — stops the
session before it reads the question at all, and lands in the same park. That park says so and
names the remediation, rather than telling the maintainer the machine spent a budget failing to
answer and asking them to rewrite a question no session ever saw. This is the same rule Intake's
hold already follows (issues #328 and #342): only the words differ, while the label, the released
claim, and the re-proof are identical, because the ticket is equally terminal either way.

**The rejection reason is produced by the parser itself**, at the check that fails, so the
sentence the operator reads and the rule the daemon enforces cannot drift apart. The
disposition contract is unchanged by a single character: the same artifacts that parsed
before parse now, and the same ones that failed still fail.

**Research dispatch inspects the record before claiming.** It submits, checks whether the
submission actually produced runnable work, and claims only then — withdrawing the
submission if the claim cannot be established. A terminal record is reported as parked and
passed over. This is the same guard Build and Intake already carry.

**One run is the bound.** The three attempts inside a run are the whole unattended budget for
a research ticket. There is no per-ticket re-dispatch counter, no resume identity, and no
"rewrite the question and it re-runs" flow. After a park, a human owns the ticket.

## Alternatives considered

- **Keep the silent release and add a per-ticket attempt counter.** Rejected: it spends more
  headroom on a rejection that is already known to be total and repeatable, and still tells
  the maintainer nothing until the counter runs out.
- **Reuse `wayfinder:awaiting-disposition` for the park.** Rejected: it asserts the opposite
  outcome. Collapsing "research produced candidates for you to choose among" and "research
  produced nothing usable" into one label makes the map's pending queue lie.
- **Close the parked ticket.** Rejected: closing is a ruling, and the daemon has no ruling.
  ADR 0037's boundary holds — the question is handed back, not settled.
- **Loosen the contract so more artifacts parse.** Rejected outright: fail-closed validation
  is what keeps unattended sessions out of the decision map. The fix is visibility, not a
  lower bar.
- **Let the park re-word the ticket's question.** Rejected: that is planning judgment, which
  belongs to a human wayfinder session.

## Consequences

- A research ticket the machine cannot answer becomes visible on GitHub with the reason
  attached, instead of cycling invisibly.
- The hourly claim/reclaim flap ends, and the daemon's log stops reporting sessions that
  never started.
- ADR 0037's "simply eligible again next cycle" is superseded for the exhaustion path only;
  its dispatch boundary, disposition contract, and handoff-required behavior are untouched.
- Unattended research now has a terminal state that is neither a decision nor a retry, so a
  maintainer sweeping `wayfinder:parked` sees exactly the questions the fleet handed back.
- An exhausted run's worktree is removed rather than retained; the resume it was preserved
  for no longer exists.
