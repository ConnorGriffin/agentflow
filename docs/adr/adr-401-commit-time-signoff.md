# ADR 401 — A session's checkout signs its own commits as it makes them

**Status:** accepted
**Originating issue:** #401

## Context

Every pull request this pipeline opens is checked for a developer certificate of origin: each
non-merge commit on the branch must carry a sign-off whose email matches that commit's own author.
A commit that arrives without one cannot be fixed forward — the only cure is rewriting it, and
every stage that could notice the problem (review, revise, respond) is forbidden from rewriting
pushed history. So one missing line permanently strands a pull request that passes every other
check, and only a human amending by hand ends it. It happened on two pull requests.

Issue #357 answered this by *asking*: the same sign-off paragraph now appears in all five
commit-capable session prompts, and a test pins all five render paths. That issue deliberately
excluded hooks and repository configuration from its scope. A build session ran after it shipped
and produced an unsigned commit anyway, so instruction alone has now been observed to not hold.

## Decision

**Preparing a session checkout installs the sign-off, so a commit carries it the moment it is
created** — inside the session that authored it, while the branch is still that session's to
rewrite. This supersedes #357's "no hooks, no repository-wide Git configuration" scope line; the
five prompts keep their paragraph as the belt to this new braces.

Four boundaries are part of the decision, not omissions from it:

- **Commit time, not push time.** A round-1 draft also refused *pushes* carrying an unsigned
  commit. Dropped: its only observable effect in this engine would have been to police the
  daemon's own rebase force-push, and it earns nothing the commit-time signer does not.
- **Only for the commit's own author.** A sign-off is a personal certification. When the identity
  on the commit differs from the identity the checkout is configured to commit as — which is what
  happens when a session amends somebody else's commit — the signer adds nothing and the message
  stays byte-identical. That case stays red and stays a human's call.
- **The maintainer's shared checkout is untouched** but for one named key. Enforcement is confined
  to the session's own checkout: a hooks directory under that checkout's private git data,
  selected by a *per-worktree* configuration value. Enabling per-worktree configuration costs one
  line in the shared repository config, which is stated plainly rather than claimed away, and is
  the only thing written there. Where the shared config holds settings Git's own guidance says
  must be relocated first, preparation **refuses** and says so instead of reconfiguring the
  repository.
- **The pull-request check is unchanged and remains the backstop.** What counts as a valid
  sign-off did not move; a session that deliberately skips verification is still caught there, and
  unsigned history already on a remote still needs a human.

**Preparation fails open.** The two callers that prepare a checkout read a raised failure as "do
not admit this stage", and one of them logs nothing at all on that path — so a signer that raised
would silently stop a repository producing pull requests. Installation therefore never raises: a
checkout that cannot be enforced runs unenforced, with one line per repository naming the
repository and the remedy, so the quiet long-lived state is not possible.

## Consequences

- A pull request that reaches review is already signed, and the hand-amendment loop is gone for
  commits this engine creates.
- Nine real repositories gain one configuration line and a per-checkout hooks directory. Because
  the hooks selection replaces a repository's hooks directory wholesale rather than layering on
  it, every hook a repository already has is forwarded from the installed directory — all nine
  enrolled repositories carry a live post-commit hook, and a silent fleet-wide loss of it would be
  a worse bug than the one being fixed.
- Preparation re-runs before every attempt, so the install is regenerated cleanly each time and
  the hand-off target is read from the repository's own scope, never from the effective value a
  previous preparation already changed. Reading the effective value would chain the signer to
  itself and make every commit in a continuation recurse.
- Research and conversation checkouts are deliberately left out: they own no branch and push
  nothing, so no commit of theirs can enter a pull request's range.
- The unenforced-repository line is written to the daemon's stream directly rather than through
  the daemon's own logger, because the daemon reaches this code and importing it back is an import
  ring the architecture tests refuse. The line's shape is pinned by a test against the daemon's
  real formatter rather than by convention.
