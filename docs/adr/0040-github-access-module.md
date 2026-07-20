# ADR 0040 — One typed, fail-closed module owns GitHub access

- Status: Accepted
- Date: 2026-07-19

## Context

Every pipeline stage talks to GitHub the same way: build a `gh` argument vector, run it
through `runner._run`, check the return code, parse `stdout` as JSON, and fail closed on
anything unexpected. That four-step shape is hand-rolled at roughly ninety call sites
across `loop.py`, `coordinated_build.py`, `gate.py`, `intake.py`, `coordinated_research.py`,
`dashboard_data.py`, and others, with about sixty-five separate `json.loads(...stdout)`
parses. GitHub's wire field names (`labels[].name`, `headRefOid`, `comments[].createdAt`,
`state`) leak through every one of those modules.

The only shared primitive is `_run`, a bare `subprocess.run` wrapper. There is no interface
between a stage and GitHub — so a change to the fail-closed convention, or to how a label
read is proven, has to be edited in dozens of places, and tests can only stand in for
GitHub by pattern-matching `gh` argument vectors against a fake `_run` (36 such patches in
`test_loop.py` alone; argv-matching fakes in every tracer test). `runner.py` already holds
three typed, fail-closed readers (`_pr_info`, `_issue_state`, `_pr_state_for_branch`) — the
seed of the missing module, but private and used only there.

This repo decides merges. An unreadable GitHub that is silently treated as "empty" could let
a stage act on a fact it never confirmed. The scattered, untyped access is therefore both a
locality problem and a correctness hazard.

## Decision

### The seam sits at one GitHub-access module

Introduce one deep `github` module through which **all** GitHub access flows. It exposes
typed, single-fact methods for the common operations — reads (`issue_labels`, `issue_body`,
`issue_state`, `pr_state`, `pr_comments`, the discovery `list`/`search` collections),
mutations (`add_label`, `remove_label`, `edit_title`, `edit_body`, `comment`, `close`,
`pr_ready`), and label creation — plus one explicitly-marked `api()` escape hatch that
carries the three exotic calls (a GraphQL comment-edit, a REST blockers read, and the auth
token) through the same return-code/JSON fail-closed handling. Nothing outside this module
ever shells out to `gh`. `runner._run` remains, but its GitHub role retires as callers
migrate; its git plumbing stays.

The module is a set of module-level functions taking `repo` as a parameter (matching how
`--repo` is threaded today), not an injected object. Callers keep calling free functions;
tests stand in for GitHub by stubbing the typed helpers directly (`github.issue_labels`
returns a set) rather than matching argument vectors. Purest dependency injection — handing
every caller a `GitHub` object — was rejected: it would thread an object through ~90 call
sites and could no longer land as a purely additive keystone.

### Reads are single-fact and fail closed on `None`

Each read owns exactly one fact's argument vector, parse, and fail-closed rule, and returns
a typed value or `None`. `None` means only that the read *failed* — `gh` errored, timed out,
or returned unparseable output. A real subject with no labels returns an empty set, not
`None`; a real empty comment thread returns an empty list. Callers fail closed on `None` and
act on the empty value. This one rule holds for every read, so "couldn't reach GitHub" can
never be confused with "the answer is empty." Reads do not batch unrelated fields: the warm
claim-verify paths that need only labels never drag whole comment threads. The few cold
sites that today fetch two fields in one call (`labels,url`, `state,comments`) make two calls
or share one small combined helper — the hot path is the discovery collection reads, which
stay one call.

Collections and comments return typed rows (`IssueRow(number, title, body, labels)`,
`Comment(body, created_at)`), so GitHub's field names stop leaking past this module — the
whole point of the seam.

### Mutations report the command's result, not durable proof

`add_label`, `comment`, and the other mutations return whether the `gh` command succeeded.
They do **not** re-read to prove the label or comment actually landed. Proof — read back,
confirm, then notify exactly once — is the job of the separate durable-handoff work
(candidate 3 of the same review), which layers on top of this module. Making mutations
prove-on-write here would silently change the many fire-and-forget call sites and duplicate
the handoff recipe, so the seam stays thin.

### The keystone is purely additive

The first change adds the `github` module and its tests and migrates **zero** callers, so it
cannot alter pipeline behavior. The three seed readers in `runner.py` are left in place and
briefly duplicated rather than moved, keeping the keystone's "can't break the pipeline"
promise absolute; they retire when `runner.py` is migrated. Call sites then migrate in small,
behavior-preserving batches — one module at a time, each keeping the suite green, each
swapping that module's tests from argv-fakes to stubbed helpers — starting with the smallest
(`gate.py`) and ending with the two largest (`loop.py`, `coordinated_build.py`). The old
GitHub-through-`_run` path is retired once no caller remains.

## Alternatives considered

- **Cover only the common cases, leave the exotic three on raw `_run`.** Rejected: GitHub
  knowledge would still leak in a few spots and the retire-the-old-path step could never
  complete. One `api()` escape hatch keeps 100% of access behind the seam.
- **Prove-on-write mutations returning durable proof.** Rejected: it changes fire-and-forget
  callers' behavior and duplicates the durable-handoff recipe that is deliberately a
  separate module.
- **One fat `issue(n)` view carrying every field.** Rejected: the warm claim-verify paths
  read only labels, and a fat view would pull whole comment threads on exactly those paths.
- **Inject a `GitHub` object into every caller.** Rejected: not additive — it threads an
  object through ~90 sites — and the repo already tests leaf GitHub access by patching module
  functions.
- **Return plain dicts / return empty on read failure.** Both rejected: dicts keep wire
  shapes leaking; empty-on-failure erases the fail-closed distinction a merge-deciding repo
  depends on.

## Consequences

- GitHub's wire format has one owner; a fail-closed rule or `gh` behavior change is one edit.
- Tests stand in for GitHub by stating facts (`issue 5 has labels {ready}`) instead of
  matching argument vectors, moving `loop.py` and the tracer tests toward the
  through-the-interface style `test_coordinator.py` already uses.
- The module is the natural place to meter the ADR 0026 bounded GitHub API budget.
- Durable-handoff proof (candidate 3) and the per-lane claim primitive (candidate 4) build on
  this module; the `coordinated_build.py` regroup follows once access, worktree identity, and
  handoff are extracted.
- Migration issues that rewrite the same file (`coordinated_build.py`, `loop.py`) are
  serialized, not built in parallel, to avoid conflict-revise churn (ADR 0038).
