# ADR 0033 — The daemon owns local Project workspace state; GitHub and git own published truth

- Status: Accepted
- Date: 2026-07-16

## Context

The Project lifecycle now distinguishes working history from published truth. A
Conversation retains one bounded exploration; it may stage immutable versions of one
Proposal for explicit approval. Publication creates or updates the durable artifact, and
artifact provenance links that effect back to its origin without making the origin required
context. These terms are settled in `CONTEXT.md`.

The current control plane has no durable representation for a Project, Conversation, or
Proposal. GitHub owns issues, pull requests, reviews, checks, comments, milestones, and
merges. Default-branch git history owns ADRs, glossary changes, visual specifications, and
other repository artifacts. The session coordinator has a private SQLite store for provider
attempts, permits, claims, and continuation recovery. The daemon derives a fleet snapshot
from those facts and bounded GitHub reads, then atomically publishes JSON for FastAPI to
serve.

[ADR 0026](0026-daemon-owned-snapshot.md) made this last boundary deliberately strict: the
web server never queries GitHub or produces the snapshot, so browser polling cannot consume
the fleet's API quota. [ADR 0023](0023-dashboard-replatform-control-plane.md) separately
authorized POST controls over existing verbs, but no control endpoint was implemented. A
Project workspace now needs both durable local working state and interactive commands while
preserving the quota, recovery, and authority boundaries.

The options and prior art are recorded in
[Project state and control-plane options](../research/project-state-control-plane-options.md)
and [Open-source prior art worth incorporating into agentflow](../research/open-source-prior-art-for-agentflow.md).

## Decision

### Published artifacts keep their existing authorities

GitHub is authoritative for every GitHub artifact and its lifecycle. Default-branch git
history at a commit SHA is authoritative for every checked-in repository artifact. The
Project workspace stores typed references, observed revisions, and provenance for those
objects; it never stores an independently writable issue, pull-request, check, merge, or
repository-file lifecycle.

Local state is authoritative only for facts the external systems do not own:

- stable Project identity, enrollment relationship, and archive history;
- immutable Conversation turns and their bounded outcome;
- immutable Proposal versions, attachments, exact approved hash, and lifecycle;
- idempotent commands and Publication intents, attempts, receipts, and unknown outcomes; and
- artifact-provenance edges connecting local origins to verified published effects.

A published artifact must stand alone. Losing local Conversation history may lose convenient
provenance and resume context, but can never make a GitHub issue or repository artifact
unusable or change its meaning.

### One daemon-side workspace module is the only logical writer

One deep workspace module inside the daemon boundary owns Project state. It accepts domain
commands and produces read models; callers do not receive table-level CRUD. It persists each
Project in a separate SQLite database under `AGENTFLOW_STATE`, with content-addressed local
blobs beside the database when attachments are too large for a row.

The workspace store remains separate from `coordinator/records.db`. Coordinator records are
private operational truth for admission and continuation; Conversation and Proposal history
has different identity, lifetime, failure, and retention rules. Neither store copies or
mutates the other's state. Each exposes only the factual projection the daemon needs.

There is one production persistence representation, so no public storage adapter is created.
The module hides schema and migrations until a second real representation establishes a
storage seam.

An unreadable workspace fails closed for workspace commands and Publication, but does not
stop already-published GitHub build issues from moving through the existing pipeline. The
daemon projects the workspace as unavailable and keeps the last verified read model rather
than guessing or rebuilding working history from external artifacts.

### Reads are projections; controls are commands

The daemon remains the only writer of fleet and Project read models. It combines workspace
facts, coordinator projections, bounded GitHub observations, and immutable repository
references, then atomically publishes bounded JSON projections. Large Conversation detail
may be split into paginated files, but a generation manifest becomes current only after the
whole generation is durable. Every projection carries its generation time, workspace
revision, and source-specific freshness. It is disposable and never a recovery source or
policy input.

FastAPI GET endpoints read only those daemon-published files. They never query GitHub, read
repositories, or open either SQLite store. With the daemon down they continue serving the
last honestly aged projection.

FastAPI POST endpoints are a separate authenticated transport to a local daemon command
channel. A command contains a stable idempotency key, expected aggregate revision, and the
operator action. The web server validates transport concerns but does not apply domain
transitions, call GitHub or git, or mutate a projection. If the daemon is unavailable, the
command fails unavailable; there is no direct-write fallback.

This narrows [ADR 0026](0026-daemon-owned-snapshot.md)'s “pure file reader” wording: web
**reads** remain file-only and browser count still cannot multiply external reads; web
**writes** may submit authenticated commands, but only the daemon interprets or applies
them. It realizes [ADR 0023](0023-dashboard-replatform-control-plane.md)'s thin-control
intent without putting pipeline or workspace policy in FastAPI.

### Proposal approval and Publication are versioned and reconciled

Proposal versions are immutable. Editing creates a new staged version. Approval binds the
exact content and attachment hashes of one version; it never means “whatever the latest
draft contains.” A failed or unknown Publication leaves that approved version retryable and
does not silently create another Proposal.

Local state and an external GitHub or git effect cannot share one transaction. Publication
therefore follows a reconciled protocol:

1. transactionally record the approved Proposal version, deterministic Publication key,
   target, expected source revision, and pending intent before the external call;
2. include that key in a non-semantic provenance marker or deterministic target identity;
3. perform the external operation, then read the authoritative target back;
4. verify identity, content/hash, and preconditions before recording the receipt and marking
   the Proposal version `published`; and
5. after a crash or timeout, search and verify by the same key before any repeat.

A proven absence may retry. A proven effect completes the existing receipt. An ambiguous
outcome fails closed and remains approved/unknown rather than blindly duplicating an issue,
comment, or repository artifact. Repeated commands with the same idempotency key return the
same terminal receipt; stale expected revisions are rejected.

Artifact provenance is a typed immutable relation, not copied artifact state. Internal
references use stable Project, Conversation, Proposal, and Proposal-version IDs. GitHub
references include canonical repository plus stable issue, pull-request, or node identity.
Repository references include repository, commit SHA, path, and blob hash. Published
artifacts carry a backlink or marker where practical but remain self-contained.

### Method execution remains a later decision

This ADR settles retention, authority, command, projection, and Publication boundaries. It
does not choose how Ask or Chart invokes skills, resumes provider context, selects a method,
or returns a Proposal payload. [The methodology-session decision](https://github.com/ConnorGriffin/agentflow/issues/127)
may assume stable Conversation IDs, immutable turns, optimistic revisions, immutable
Proposal versions, and daemon-owned commits of accepted session results. Skills and agents
never write workspace state, GitHub, coordinator records, projections, or default-branch
repository truth directly. They may produce staged attachments in isolated working state;
the daemon-side orchestration layer adopts accepted outputs into a Proposal version and
Publication remains separately approved.

## Alternatives considered

- **Store every Conversation and Proposal in GitHub or the repository.** Rejected: it turns
  exploration into durable project truth, makes resume depend on network state, and erases
  the explicit approval boundary.
- **Use a long-lived working branch as the workspace database.** Rejected: it introduces
  merge conflicts, cleanup and accidental-publication rules, and cannot make external
  Publication idempotent.
- **Let FastAPI own or share the workspace database and perform effects.** Rejected: each
  server becomes a policy writer, multiple servers can race, and the adapter bypasses the
  daemon's ownership and recovery boundaries.
- **Persist the workspace as JSON or JSONL.** Credible for a throwaway prototype, but
  rejected as the durable boundary: approval hashes, aggregate revisions, unique command
  keys, Publication receipts, and provenance would recreate transactions by hand.
- **Add Project tables to the coordinator database.** Rejected: it couples long-lived human
  working history to fail-closed provider admission and violates the coordinator store's
  intentionally private seam.
- **Create a second always-on workspace service.** Rejected for the first product slice:
  the solo local daemon already owns reconciliation and projection publication. A second
  brain adds deployment and failure coordination before a real need exists.
- **Let stale reads fall back to live GitHub queries.** Rejected by ADR 0026's original
  failure: observability must not starve the pipeline it observes.

## Consequences

- Agentflow gains local durable working memory without becoming a second task tracker.
- The daemon gains one workspace module, a command receiver, and Project projection work;
  FastAPI remains a thin static/read/command adapter.
- The Project workspace can resume while GitHub is unavailable, but Publication and any
  view that claims fresh external state cannot proceed without authoritative verification.
- Project removal archives local history rather than deleting it; retention, export,
  backup, and multi-machine replication remain later policy decisions.
- Snapshot freshness remains bounded independently of browser count. Detail projections
  must stay bounded so Conversation history does not turn the fleet snapshot into a database
  dump.
- Existing intake, build, review, merge, and coordinator behavior is unchanged after a
  build handoff publishes its standalone GitHub issue.
- Implementation may prototype the command handler and projections in process, but the real
  UI still goes through `/ui-mockups`, and no build issue is ready until the methodology and
  workspace-prototype decisions clear.
