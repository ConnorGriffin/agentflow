# ADR 649 — Disposable artifacts require durable ownership proof

Status: Accepted
Date: 2026-08-16

## Context

Provider-discovery evidence from ADR 582 was created inside enrolled repositories. Its unique
skill was consequently committed and inherited by later worktrees. Git registrations and
Codebase Memory projects also outlived their directories, while path shape alone could not
distinguish disposable AgentFlow work from operator-retained work.

## Decision

Provider discovery creates its probe under a unique OS temporary root and passes that root to the
provider. The complete root is removed by context cleanup on every exit, including interruption;
the durable receipt remains bound to the enrolled repository. Historical probe names and bytes
are an explicit inventory, never a prefix match.

Each new AgentFlow worktree records a schema-1 `agentflow-owned.json` marker in its per-worktree
Git directory. The marker binds `owner: agentflow`, the exact real worktree path, and whether the
checkout is disposable. It lives beside Git's existing activity marker and does not dirty the
checkout. An absent, malformed, symlinked, or path-mismatched marker proves nothing.

`agentflow maintenance` inventories before mutation and emits one JSON object per line. Dry run is
the default; `--apply` revalidates every eligible entry immediately before acting. A worktree is
removed only when the marker proves it disposable, coordinator state proves it is neither live
nor held, Git's activity marker is inactive, it is unlocked, and a non-writing status is clean.
Dirty, live, held, retained, unknown-owned, and unreachable entries are refusals.

Missing Git registrations may be pruned without directory ownership because their path no longer
exists. A Codebase Memory project may be deleted only when its authoritative recorded root is
missing, or when that exact root was successfully removed in the same run. The supported
single-tool CLI performs deletion; immutable read-only access to its project database supplies an
authoritative root when the one-shot list response omits it.

The public skills repository authors skill releases, `capabilities.toml` pins them, and enrollment
materializes both provider destinations as digest-verifiable directories. This decision does not
introduce provider symlinks or change `skill_destination_status`.

## Alternatives

- Treat paths under `.agentflow/worktrees` or `~/worktrees` as owned. Rejected: both contain live,
  retained, and human-created work.
- Infer Codebase Memory roots by reversing project names. Rejected: the transform is not
  reversible and would turn ambiguity into deletion authority.
- Keep probes in the enrolled checkout and delete them afterward. Rejected: interruption and
  commits can make the probe inheritable before cleanup.

## Consequences

Old unmarked worktrees are reported but never deleted; an operator must establish ownership by a
separate reviewed action. Research and conversation worktrees are marked owned but non-disposable.
Maintenance remains operator-invoked, replay-safe, and isolated from dispatch.
