# ADR 0041 — One value type owns the worktree-path layout

- Status: Accepted
- Date: 2026-07-19

## Context

Each pipeline session's checkout lives at a path that encodes facts:
`{workdir}/.agentflow/worktrees/{lane}/{name}`, where `lane` is `{tool}`, `{tool}-review`,
or `{tool}-intake`, and `name` is `issue-{n}-{slug}`, `mockup-{n}-{slug}`, or
`pr-{pr}-{slug}`. The branch is `agentflow/{lane}/{name}`. That single convention is the
smuggled form of a `(workdir, tool, kind, issue|pr, slug)` tuple.

It is decoded by ad-hoc `str.split("/.agentflow/worktrees/", 1)` plus regex in roughly a
dozen places — `_build_source_parts`, `_source_facts`, `_review_source_facts`,
`_revise_builder_source`, `_park_pr_number`, four verbatim `Path(source).name.split(f"pr-{pr}-")`
slug re-extractions in `coordinated_build.py`, `tracer.py`'s branch derivation, and the
intake/research/converse variants — and it is encoded by f-strings in `loop.py`'s
`_builder_worktree`, `reviewer.py`'s `review_worktree`, and the mockup submission. Parse and
construct are the same rule written in two directions, in different files, so they can
silently drift: a path built one way could fail to parse the other. Each decode site also
validates slightly differently (only `_source_facts` cross-checks the record's lineage
against its pool; only the review sites require a `-review` suffix), so the layout's shape
rule has no single owner.

## Decision

### One value type owns the layout, both directions

Introduce a `WorktreeRef` value type that is the single owner of the
`.agentflow/worktrees/...` layout for **every** session kind — build, revise, review,
mockup, intake, research, and converse. It both takes an existing path apart and builds new
worktree paths and branch names:

- `parse(source) -> WorktreeRef | None` decomposes a path into typed components (workdir,
  lane, tool, kind, issue-or-PR number, slug).
- Named constructors (`for_build`, `for_review`, `for_mockup`, `for_intake`, …) build a ref
  from those components.
- `.path` and `.branch` render the ref back to the on-disk path and the `agentflow/...`
  branch name.

Because one type defines the layout, parse and construct cannot drift; a round-trip property
test (`parse(ref.path) == ref`) pins the whole convention in one place. The f-string
construction sites and the regex decode sites both collapse to this type.

### Layout shape versus record agreement are different jobs

`parse` owns only the *shape* rule: a string is either a well-formed worktree path or it is
not. Malformed or unrecognized input returns `None` — the same fail-closed convention every
current site already uses and [ADR 0040](0040-github-access-module.md) applies to reads.
Checks that compare a parsed path against a *record's* expectations (does this path's lane
match the record's pool; does this review path belong to this record's builder lineage) stay
with the callers that hold the record. The type does not take a record; mixing record
agreement into layout parsing is exactly the divergence that produced today's per-site
validation drift.

### The keystone is purely additive

The first change adds `WorktreeRef` and its tests and migrates zero callers, so it cannot
alter pipeline behavior. The dozen decode sites and the construction f-strings migrate to it
later, in behavior-preserving batches, serialized on any shared file (`coordinated_build.py`)
against the other candidates' migrations to avoid conflict-revise churn
([ADR 0038](0038-conflict-resolution-as-revise.md)).

## Alternatives considered

- **Parse only, leave construction as f-strings.** Rejected: the layout rule keeps two
  owners, so parse and format can drift — a bug that only surfaces when a real path built one
  way won't parse the other. Owning both directions is the locality win.
- **Model only the build/review/mockup shapes the busiest file uses.** Rejected: intake,
  research, and converse would keep private parses, so the layout still has 2–3 owners — the
  problem this deletes.
- **Fold record-agreement checks into the type.** Rejected: those checks compare path facts
  to record facts the path does not carry; putting them in the type reintroduces per-caller
  variation and couples a pure value type to record shape.
- **Raise on malformed input.** Rejected: every current site fails closed by returning
  `None`; raising would change behavior at the migration sites and break the additive promise.

## Consequences

- The worktree layout has one owner; a naming change or a new session kind is one edit plus a
  round-trip test, not a hunt across a dozen regexes and f-strings.
- The review→build sibling derivation (`_revise_builder_source`) becomes a constructor call
  from the parsed review ref plus the record's builder lineage — no second durable field.
- Additive keystone; independent of the `github` module, so it can be built in parallel with
  it. Its migrations serialize with the others on shared files.
