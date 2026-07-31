# ADR 0051 — Merged is not running: the runtime checkout is the unit of deployment

- Status: Accepted
- Date: 2026-07-30
- Amends: [0050](0050-bounded-worktree-retention.md) (the bound it describes only
  protects a repository once the daemon is actually running it)

## Context

The daemon is a long-lived process. It loads engine code once, at launch, from a
checkout that is not the development checkout: `worktrees/agentflow/daemon`, held
deliberately detached. Merging to `main` therefore changes nothing by itself. A merge
takes effect only after that checkout advances *and* the resident process is relaunched.

Nothing in the pipeline reports that gap. Issues close, PRs merge, the ADR lands, and
the fleet keeps running the engine it booted with. Every signal a maintainer normally
reads says "shipped".

The forcing event was the 2026-07-30 outage. [ADR 0050](0050-bounded-worktree-retention.md)
bounds registered checkouts precisely so sessions stop losing their shell. It merged
before the outage ran its course — and never executed. The daemon had been hotfixed in
place during an earlier incident, which is the fastest way to stop a live failure and
left the runtime checkout on a commit that was not an ancestor of `main`. The sync only
ever moved that checkout by `git merge --ff-only`. Fast-forward was impossible, so it
refused — every ten minutes, indefinitely, logging `skip-noff`, which reads like a safe
skip rather than a daemon frozen seven commits back.

So the repository sat through the exact outage its merged fix prevents, with the fix on
`main` looking deployed. The failure was silent in both directions: the sync reported a
skip, and the daemon reported nothing at all, because a daemon running old code behaves
perfectly — as old code.

Two properties made this durable rather than transient. The refusal had no escalation
path: nothing retried differently, aged out, or complained. And the hotfix that caused
it was the correct emergency response, so the trap is baited by good behavior.

## Decision

**The runtime checkout is set to `origin/main`, not fast-forwarded onto it.** It is
disposable by construction: no branch, nothing staged, unstaged, or untracked, and
outside any rebase or merge — all four established by inspection before it is touched.
Nothing there can be lost by a non-linear move, so ancestry is not a safety property
worth enforcing. Refusing to move it protects nothing and freezes the engine.

**A non-linear move is named, not hidden.** Divergence logs `diverged: … is not on main
— resetting onto origin/main`. Skipping and resetting must not read alike; the 2026-07-30
failure was legible in the log the whole time and still went unread for hours.

**The development checkouts keep the opposite rule.** They hold real local work and stay
fast-forward-only, skipped on any dirty, branched, or mid-operation state. The asymmetry
is the point: one checkout holds work, the other holds a process.

**Deploying includes the restart, and the restart yields to live work.** Dispatch is
stopped first, then live sessions are checked; if one is still running the old daemon is
restored and the update waits for the next interval. A restart interrupts running
provider sessions and each interruption burns one attempt of that stage's budget, so a
deploy that raced a review would spend the review's attempts to ship itself. A failed
start leaves a durable retry marker rather than a stopped fleet.

Implementation lives in `dotfiles/scripts/fleet-sync` (`sync_daemon_checkout`), fired
every ten minutes by launchd, with the guard exercised through its real interface in
`dotfiles/tests/fleet-sync.test.sh`.

## Alternatives

**Keep fast-forward-only everywhere.** Uniform and easy to reason about, and it is what
we had. It treats an unreachable state as a safe one: the refusal is permanent, and the
cost of being wrong is unbounded — the engine never updates again. A guard whose failure
mode is "never recovers" is not conservative.

**Never hotfix in place; always ship through `main`.** This removes the cause but not the
class, and it is the wrong trade during an outage, where the fastest correct action
should not be the one that quietly disables future deploys. Any state the runtime lands
in must be recoverable automatically.

**Alert on drift instead of correcting it.** Another signal to miss, in a fleet whose
premise is that unattended work does not wait on a human to notice.

**Run the daemon from the development checkout.** Deletes the second checkout, and pins
the resident process to whatever local state that checkout is in — mid-rebase, on a
branch, dirty. That is the failure this separation exists to prevent.

## Consequences

- Merged and running are separate facts. When an engine fix appears not to have changed
  behavior, compare the runtime checkout's `HEAD` against `origin/main` **before**
  suspecting the fix. That check would have cut hours off 2026-07-30.
- Hotfixing the live daemon is safe again: the next sync moves it back onto `main` and
  says so. The hotfix is still lost if it was never merged — the runtime checkout is not
  a place to keep work.
- Deploys are deferred while any fleet session is live, so an engine fix can lag a merge
  by longer than one interval on a busy fleet. Accepted: attempts are scarcer than time.
- ADR 0050's bound protects a repository only from the moment the daemon runs it. The
  same is true of every engine change; this ADR is what makes that reliable.
