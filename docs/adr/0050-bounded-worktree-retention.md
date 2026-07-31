# ADR 0050 — Retention is bounded: recoverable by ref, not present on disk

- Status: Accepted
- Date: 2026-07-30
- Amends: [0028](0028-stage-scoped-continuations.md) (code-writing worktrees remain
  exactly as they are), [0043](0043-recovery-state-before-replay.md) (the retained-worktree
  envelope contract)

## Context

Every session that held, was abandoned, exhausted its continuation budget, or died
before opening a PR left a permanently registered git worktree. Reclamation only ever
removed sessions git could already prove were durable — pushed, clean, reachable from
`origin` — so any uncommitted work pinned its registration forever. Retention was
monotonic and had no age bound and no ceiling.

Held records are the dominant term. A hold is terminal but is never retired, so its
source stayed "coordinator-owned" and therefore protected for the life of the store. On
the live fleet, 63 of 74 protected sources belonged to held records.

The forcing event was a repository-wide outage on 2026-07-30. The provider CLI builds its
sandbox profile with one filesystem deny path per registered worktree. At roughly 246
registrations that single argument crossed the OS exec-argument limit (~1.6 MB), and
every agent session in the repository — intake and build alike — lost its shell on its
first command. It could not run tests, could not reach `git` or `gh`, and could not post
a comment explaining itself. Four consecutive sessions on one issue died that way; the
recorded outcome was "continuation budget exhausted", which was true and actively
misleading. Each attempt left one more registration behind.

The daemon never sees this failure: its own git calls are unsandboxed. It was feeding
agents into an environment it had itself made unusable.

## Decision

**Safety means the work is recoverable, not that the directory still exists.** A
stranded session's full state — committed, staged, unstaged, and untracked — is
snapshotted into a commit anchored under `refs/agentflow/stranded/<name>/<sha12>`, and
only then is its checkout reclaimed. The snapshot is git plumbing parented on the
checkout's own HEAD: no branch moves, no PR changes, nothing is force-committed. The tree
is built in a scratch index so a failure partway through leaves the worktree exactly as
it was found. If any step fails, the worktree stays.

**Retention is bounded by idle age, then by count.** A stranded session is eligible for
archiving only after 24 hours untouched, and then only the oldest beyond a per-repository
cap of 12 are archived, at most 20 per sweep. The idle floor is the entire protection for
a checkout with no session marker — a `/agentflow pickup` session or a hand-cut worktree
— because no local clock moves for an editor writing files without running git.

**Held sources are no longer protected from reclamation.** A maintainer resume rebuilds
the checkout from the branch regardless, and the archive preserves the uncommitted state.
Two further classes join the same path: a session whose completion cannot be confirmed
(previously retained forever on the "unknown" reading), and a session that is complete but
whose removal fails the durability check — routine, since one untracked file or a
squash-merged branch whose commits `origin` has pruned is enough to fail it.

**Reclamation runs on a cadence inside the dispatch pass**, hourly per repository, ahead
of admission and under the same single-flight guard — never as a parallel thread, because
it reads the set of owned sources and then spends minutes confirming completion, and a
stale reading could archive a live session's working directory. A paused daemon does not
reclaim.

**The daemon refuses to dispatch into a repository past a registration ceiling** (175
registrations), checked count-only with no GitHub call and no mutation, and fails open if
git cannot be read. The refusal names how many registrations are agentflow's own versus
foreign, because reclamation can only reach the former.

Research and conversation checkouts are excluded from reclamation entirely: neither has a
completion rule, a conversation's checkout is its only durable output and is reused across
turns while each turn's record retires, and both populations are small and human-driven.

## Alternatives

**Keep the "directory survives" promise and cap something else.** There is nothing else
to cap: the registration count *is* what the sandbox profile scales with, and every
uncommitted session pins one indefinitely.

**Age out worktrees by deleting them.** Rejected — it destroys work, which is the property
ADR 0028 was protecting. Archiving keeps the promise's substance while dropping its form.

**Fix the sandbox profile so it does not enumerate worktrees.** Correct, and the fix that
would make this failure impossible rather than merely bounded — but it is upstream in the
provider CLI, not ours. These bounds hold regardless.

**Sweep only when over the ceiling.** Rejected: a cliff-triggered sweep makes the ceiling
the steady-state bound rather than the floor-plus-cap, which is the number that has to be
small.

## Consequences

- An archived session is recoverable by a human, not auto-continued. A re-dispatched build
  re-prepares from the branch or `origin` and starts fresh; the uncommitted delta is on the
  stranded ref. That is the deliberate trade.
- ADR 0028's "human re-entry adopts the retained code-writing worktree" and "missing local
  state holds again rather than silently starting over" no longer hold for a session idle
  past the floor. Re-entry starts from the branch tip with the ref named for rescue.
- Hold comments that direct a maintainer to a retained worktree now also carry the
  `git for-each-ref refs/agentflow/stranded/<name>/` incantation, since that path may be
  gone by the time it is read.
- The steady-state bound is foreign registrations + live-state protected sources + the cap,
  enforced hourly *while dispatch is enabled*. A long-dormant daemon still opens
  continuations and Ask turns and never reclaims, so it converges back on the old behavior
  until re-enabled.
- The ceiling is a count proxy for a byte limit. Path lengths vary by machine and
  repository, so the number does not port.
- The gate stops new cold submissions only. Records submitted in earlier passes still admit
  and launch, and converse turns on the drain arm are not covered — a bounded residual that
  the sweep is concurrently curing.
- Sessions that lose their shell are still misclassified as budget exhaustion. Naming that
  as a distinct environment fault is separate work; this decision removes its precondition.
