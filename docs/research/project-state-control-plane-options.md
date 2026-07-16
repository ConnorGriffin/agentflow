# Project state and control-plane options

_Research date: 2026-07-16. Scope: issue [#126](https://github.com/ConnorGriffin/agentflow/issues/126). This is a boundary recommendation, not an implementation design._

## Answer

**Recommendation (inference):** retain Conversation history, immutable Proposal versions,
approval/publication attempts, and provenance in a per-Project local SQLite workspace store.
Keep that store separate from the coordinator database. A daemon-side workspace module is
its only logical writer. Browser controls cross an authenticated command channel to that
writer; they never write the store, a projection, GitHub, or a repository directly. The
daemon remains the only projection writer and materializes bounded JSON read models for the
web server. GitHub remains authoritative for issues, pull requests, reviews, checks, labels,
comments, milestones, and merges; the repository's default-branch history remains
authoritative for repository artifacts.

This preserves the distinction already fixed in the glossary: a Conversation is resumable
working history, a Proposal is an explicitly approved atomic change, a Publication is the
verified durable effect, and provenance links the effect back without making the local
workspace a competing source of project truth ([`CONTEXT.md`](../../CONTEXT.md#L8-L60)).

## Verified current boundary

The following are source facts, not recommendations.

| State now | Current owner/writer | Authority and recovery role |
|---|---|---|
| Issues, labels, comments, pull requests, checks, and merges | GitHub, through pipeline `gh` operations | Authoritative delivery/task state. The console was explicitly designed to sit over GitHub rather than replace it ([ADR 0010](../adr/0010-operator-dashboard.md#L20-L52)); the snapshot builder reads these objects and does not mutate them ([`dashboard_data.py`](../../agentflow/dashboard_data.py#L47-L56), [`dashboard_data.py`](../../agentflow/dashboard_data.py#L74-L195)). |
| Code, ADRs, glossary, visual specifications, and other checked-in artifacts | Git history and the repository's normal writers | Durable repository truth. The Charter requires domain language from `CONTEXT.md` and load-bearing decisions in ADRs ([Charter](../../standards/CHARTER.md)). |
| Fleet enrollment and daemon intent | `daemon.py`; operator controls the local `enabled` flag | Enrolled repositories are currently declared in source; the local flag controls whether dispatch runs ([`daemon.py`](../../agentflow/daemon.py#L62-L97), [`daemon.py`](../../agentflow/daemon.py#L231-L247)). This is local operating intent, not a GitHub task state. |
| Dispatch/admission | Daemon dispatch loop; legacy `Governor`; session coordinator for migrated stages | The legacy governor keeps machine/stage/pacing counters in memory ([`dispatch.py`](../../agentflow/dispatch.py#L46-L100)). The daemon runs repo dispatch, then the coordinator reconciles Build/Review/Revise and republishes their live projection; merges remain outside this concurrent path ([`dispatch.py`](../../agentflow/dispatch.py#L181-L263)). |
| Logical-stage continuation, claims, attempts, and permits | Session coordinator package | A private, versioned SQLite store under `AGENTFLOW_STATE` is the crash-recovery source for stage records; running rows are the permit ledger and unreadable state fails closed ([`store.py`](../../agentflow/coordinator/store.py#L1-L14), [`store.py`](../../agentflow/coordinator/store.py#L38-L61), [`store.py`](../../agentflow/coordinator/store.py#L219-L252)). Stable stage identity makes submission idempotent ([`record.py`](../../agentflow/coordinator/record.py#L27-L56), [`coordinator.py`](../../agentflow/coordinator/coordinator.py#L123-L162)). |
| Provider-attempt observations | Detached supervisor/session files beside the coordinator store | Structured events and terminal process facts survive a daemon crash and are keyed by the launch token; they are execution evidence, not project history ([`session.py`](../../agentflow/coordinator/session.py#L1-L35), [`session.py`](../../agentflow/coordinator/session.py#L51-L105)). |
| Live sessions, daemon liveness, fleet snapshot | Daemon/runtime code | `live-sessions.json`, `daemon-status.json`, and `snapshot.json` are atomically replaced local projections ([`live.py`](../../agentflow/live.py#L24-L27), [`live.py`](../../agentflow/live.py#L66-L85), [`live.py`](../../agentflow/live.py#L136-L158)). ADR 0030 further fixes live state as a projection of coordinator records, not an ownership/recovery source ([ADR 0030](../adr/0030-session-coordinator-seam.md#L204-L218)). |
| Snapshot production and serving | Daemon writes; FastAPI reads | The daemon catches failed production and leaves the prior snapshot intact ([`daemon.py`](../../agentflow/daemon.py#L157-L165)). The web app exposes only `GET /api/snapshot`, reads the file afresh, returns an empty contract before first publication, and never queries GitHub ([`webapp.py`](../../agentflow/webapp.py#L1-L8), [`webapp.py`](../../agentflow/webapp.py#L22-L60)). |
| Rollout, worktree/PID, and ratchet facts | Local runtime modules | `rollout.json` stores desired migration mode while phase is re-derived from observed evidence ([`rollout.py`](../../agentflow/coordinator/rollout.py#L1-L21), [`rollout.py`](../../agentflow/coordinator/rollout.py#L65-L137)); worktrees/PID markers are liveness evidence; `ratchet.json` is bounded derived history and explicitly not the system of record ([`ratchet.py`](../../agentflow/ratchet.py#L1-L22), [`ratchet.py`](../../agentflow/ratchet.py#L40-L62)). |

There is currently no durable Project/Conversation/Proposal store. There is also no web
control endpoint: ADR 0023 authorized thin POST adapters over existing verbs
([ADR 0023](../adr/0023-dashboard-replatform-control-plane.md#L29-L64)), but the current
FastAPI surface is read-only. ADR 0027 already keeps Wayfinder planning objects outside
intake and requires published build issues to stand alone before normal intake sees them
([ADR 0027](../adr/0027-wayfinder-planning-boundary.md#L16-L44)).

## Recommended authority matrix

This matrix is the core decision. “Canonical” is intentionally narrower than “retained
locally.”

| Object/fact | Canonical authority | Writer | What the Project workspace retains | Conflict/rebuild rule |
|---|---|---|---|---|
| Project identity and enrollment | Repository enrollment configuration plus stable repository identity | Enrollment flow | Stable Project ID, repository identity, archive status, projection cursor | Re-enrollment reuses the Project; removal archives rather than deletes, matching the glossary. |
| Conversation | Project workspace | Daemon-side workspace module | Immutable turns/events, parent/head revision, one question/outcome, status, context references | It is authoritative only for its own working history. It closes on resolution/abandonment and reopens only for the same outcome; it cannot override a published artifact or an ADR. |
| Proposal | Project workspace | Daemon-side workspace module after an explicit command | Immutable content-addressed versions, one primary target plus inseparable attachments, lifecycle (`staged`, `approved`, `published`, `discarded`), exact approved version/hash | Editing creates a new staged version. Approval binds one hash. A failed Publication leaves that version approved and retryable. |
| Publication | Target authority for the effect; workspace receipt for causation/idempotency | Daemon invokes the target-specific publisher | Intent key, attempt/result, target reference, observed content/version/hash, timestamps, provenance edges | Never mark `published` from a successful process exit alone. Read the target and verify the effect; target wins on disagreement. |
| GitHub artifacts | GitHub | Existing GitHub domain actions invoked by the daemon | Stable URL/node or issue number, observed revision/hash, freshness, provenance only | Re-read GitHub. Do not persist an independently mutable issue/PR/check state machine. |
| Repository artifacts | Default-branch Git history at a commit SHA | Normal git/PR publication path | Repository/path/commit/blob hash and provenance only; staged bytes remain local until published | A worktree is not authority. Verify the artifact at the recorded commit; a later commit is a new observed version. |
| Coordinator records | Coordinator's private store | Coordinator package (including its guarded child-start handshake) | Only typed references needed to explain a running/held projection | Never copy or edit continuation state in the Project store. Rebuild the workspace view through a coordinator projection. |
| Snapshots/read models | None: derived and disposable | Daemon only | Generation/revision and per-source freshness stamps | Rebuild from the workspace, coordinator interface, GitHub, and repository refs. Never use a snapshot as recovery truth or a command surface. |

## Persistence, writer, and control-plane options

| Option | Persistence and writer | Benefit | Decisive problem | Verdict |
|---|---|---|---|---|
| GitHub/repository only | Every turn and staged change becomes an issue/comment or checked-in file | One obvious external authority | Pollutes durable truth with exploratory history, makes resume depend on network/indexing, and collapses the explicit approval boundary | Reject |
| Repo-side working branch | Conversation and Proposal files live on a long-lived branch/worktree | Diffable and portable | A working branch becomes an undeclared database with merge conflicts, cleanup rules, and accidental publication; it also cannot safely coordinate external Publication | Reject as canonical working state; allow export only |
| Webapp-owned or shared database | FastAPI writes local state and calls GitHub/repo actions | Direct request/response path | Makes each server a state writer, embeds policy in the adapter, creates multi-server races, and weakens ADR 0026's independently bounded read path | Reject |
| Daemon-owned JSON/JSONL | One process appends turns/events and rewrites current state; web sends commands | Small dependency-free start; append history is inspectable | Cross-entity approval, unique idempotency keys, Publication receipts, pagination, and crash-safe materialized state require a growing hand-built transaction layer | Credible prototype only |
| **Daemon-owned Project SQLite plus generated JSON projections** | One daemon-side module owns a separate per-Project database and immutable blob directory; web POSTs commands over local IPC and GETs generated files | Atomic local transitions, stable resume, one writer, queryable provenance, bounded read models, and no new dependency | Requires a small command protocol and explicit schema migration policy | **Recommend** |

This recommendation adapts two first-party patterns narrowly: OpenHands keeps stable-ID,
appendable Conversation events with parent links and duplicate protection
([source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/event_store.py)); OpenSpec models proposed artifacts and dependency gates separately from current durable specifications
([source](https://github.com/Fission-AI/OpenSpec/blob/main/schemas/spec-driven/schema.yaml)).
Neither pattern justifies event-sourcing agentflow's execution engine or copying external
task state. The existing [prior-art research](open-source-prior-art-for-agentflow.md) reaches
the same “facts first, derived display, no second tracker” boundary.

## Concrete recommended shape

### Writer and persistence ownership

Add one deep `ProjectWorkspace`-like module inside the daemon boundary. Its public behavior
is domain commands and read-model production, not CRUD tables. It alone mutates a separate
store such as `AGENTFLOW_STATE/projects/<project-id>/workspace.db`; large staged attachments
may live in a content-addressed blob directory beside it. Do not add Project tables to
`coordinator/records.db`: ADR 0030 deliberately keeps that store private to operational
continuation and permit accounting ([ADR 0030](../adr/0030-session-coordinator-seam.md#L84-L126)).

The persistence shape should contain:

- immutable Conversation turns/events with stable IDs, sequence/parent, author, timestamp,
  content hash, and authoritative-context references;
- immutable Proposal versions and attachment hashes, with a small current lifecycle row;
- append-only approval, Publication, and provenance facts plus materialized current status;
- commands keyed by client idempotency key and expected aggregate revision; and
- Publication intents/receipts keyed by Proposal version, target, and operation.

Do not store mutable replicas of issue bodies, PR status, CI, labels, merge state, or default-
branch files. A bounded observation cache is acceptable only when it carries source identity,
revision, and freshness and can be discarded.

### Command/control path

Keep reads and commands physically separate:

```text
GET -> FastAPI -> daemon-generated JSON projection
POST -> same-origin/token check -> local daemon socket -> domain command -> canonical writer
```

The web server is an authenticated transport adapter. It does not open the workspace or
coordinator databases. A command carries a stable idempotency key and the Proposal or
Conversation revision the operator saw. The daemon serializes commands per aggregate,
rejects stale revisions, applies local transitions transactionally, and invokes existing
GitHub/git/domain actions where an external effect is authorized. If the daemon is down,
GET continues serving the last honestly aged projection while POST returns unavailable;
there is no direct-write fallback.

Pause/resume and rollout commands set durable local intent idempotently. Publish, pickup,
merge, or loosen commands use target-specific preconditions and then verify the authoritative
target. No control is encoded by editing `snapshot.json`.

### Projection and snapshot path

The daemon composes read models from four sources: Project workspace facts, coordinator-
owned projections, GitHub observations, and immutable repository references. It continues
to write the fleet `snapshot.json` atomically. Add compact Project/Conversation/Proposal
summaries to that snapshot and materialize large Conversation detail as bounded, paginated
JSON projections. For multiple files, write a complete generation and atomically replace a
small current-generation manifest so one request cannot mix revisions.

Every read model carries `generated_at`, `workspace_revision`, and source-specific freshness
such as `github_fresh_at`; external failure leaves the last verified external view in place.
The daemon may publish a local-only change using its last external observation without
pretending GitHub became fresher. The browser can poll any number of times without causing
GitHub or repository reads. This is ADR 0026's cost and failure boundary, extended rather
than bypassed ([ADR 0026](../adr/0026-daemon-owned-snapshot.md#L30-L74)).

### Crash and idempotency model

Local state can be transactional; a local transaction and a GitHub/git effect cannot be one
atomic commit. Treat Publication as a reconciled operation:

1. Transactionally record the approved Proposal version, deterministic Publication key,
   target, expected source revision, and `pending` command before the external call.
2. Put the Publication key in a non-semantic provenance marker/trailer where the target
   permits it. For a GitHub issue creation, include it in the body; for a repository artifact,
   bind it to deterministic path/content and commit metadata. GitHub's documented create-
   issue request has title/body/assignment/milestone/labels fields but no idempotency field,
   so agentflow must supply reconciliation identity itself
   ([GitHub REST API](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#create-an-issue)).
3. Perform the external operation, then read the target back and verify identity, expected
   content/hash, and preconditions.
4. Transactionally store the receipt/provenance and mark the exact Proposal version
   `published`.
5. After a crash or timeout, reconcile by the deterministic target and marker before retrying.
   A proven absence may retry; a proven effect completes the receipt; an ambiguous result
   stays approved/unknown and fails closed rather than blindly creating a duplicate.

Repeated set/update commands are naturally convergent when guarded by target revision.
Create commands are effectively-once only through the marker-and-reconcile rule. Command
rows retain terminal results so a repeated client key returns the same receipt.

### Artifact provenance

Represent provenance as typed, immutable references, not copied artifact state. A minimal
edge/receipt needs:

```json
{
  "source": {"kind": "proposal", "id": "prop_...", "version": 3, "sha256": "..."},
  "origin": {"conversation": "conv_...", "decision_ticket": "github:owner/repo#42"},
  "publication": {"kind": "github_issue", "repo": "owner/repo", "number": 180},
  "related": [{"kind": "git_artifact", "commit": "...", "path": "docs/adr/....md"}]
}
```

Use stable internal IDs for Project, Conversation, Proposal, and Proposal version; canonical
GitHub repository plus issue/PR/node IDs for GitHub; and repository, commit SHA, path, and
blob hash for checked-in artifacts. Store relations such as `originated_in`, `published_as`,
`implements`, and `evidenced_by`; typed related references include Decision ticket, Decision
map, Milestone, Visual specification, and Acceptance evidence where applicable. Put the
immutable Publication marker/backlink in the published artifact where practical, but keep
the artifact self-contained as the glossary requires
([`CONTEXT.md`](../../CONTEXT.md#L55-L60)).

## Why this does not create a second task tracker

The Project store owns only facts GitHub and git do not own: Conversation history, Proposal
versions/approval, command receipts, and provenance. It can say “Proposal version 3 published
as issue 180”; it cannot say issue 180 is open, ready, blocked, reviewed, green, or merged as
an independently writable fact. Those states are read from GitHub into disposable projections.
Likewise, repository artifact content is identified at a commit rather than copied into a
mutable local truth after Publication.

The command path changes the real authority through existing domain verbs. The projection
path only observes. Planning remains upstream: an approved build handoff creates an ordinary,
standalone GitHub issue without `wayfinder:*`, after which normal intake and GitHub state own
the pipeline ([ADR 0027](../adr/0027-wayfinder-planning-boundary.md#L16-L44)).

ADR 0026 is preserved because the daemon is still the only snapshot/projection writer, every
web GET remains a local-file read, browser count does not multiply GitHub traffic, stale data
remains honestly stamped, and there is no live-query fallback. ADR 0023's promised POST controls
coexist as a separate command channel; a command response is not a new read model.

## Load-bearing invariants for an ADR

1. Exactly one Project exists per enrolled repository; unenrollment archives it and never
   silently deletes its Conversation or provenance history.
2. GitHub is authoritative for GitHub artifacts and delivery/task state; default-branch git
   history is authoritative for repository artifacts.
3. A Conversation has one bounded outcome, closes only on resolution/abandonment, and
   reopens only for that same outcome. Its durable history is not authoritative project
   knowledge.
4. Proposal versions are immutable; approval names one exact hash; changed content is a new
   staged version; failed Publication remains approved and retryable.
5. The daemon-side workspace module is the only logical writer of Project state. The web app,
   skills, agents, and snapshot readers never write it directly.
6. The coordinator store remains private operational state. Project history neither replaces
   it nor treats live/PID/snapshot state as recovery truth.
7. The daemon alone writes snapshots and detail projections. Web GETs never query GitHub,
   repositories, or mutable stores and never fall back when data is stale.
8. Controls use an authenticated, revision-checked, idempotent command path separate from the
   read path. A snapshot mutation is never a command.
9. A Publication becomes `published` only after the authoritative target is read and verified.
   Unknown external outcomes reconcile or fail closed; they do not blind-retry creation.
10. Local Project state stores references, revisions, and provenance for GitHub/repo artifacts,
    not a second mutable lifecycle for them.
11. Every derived state exposes its source revision/freshness and can be rebuilt without being
    used as a policy input.
12. A published artifact stands alone; provenance is traceability, not required context.

## What #127 and the workspace prototype may assume

Issue [#127](https://github.com/ConnorGriffin/agentflow/issues/127) may treat the storage and
authority boundary as settled and focus on methodology-session orchestration:

- Ask and Chart operate inside a stable Conversation ID with an append-only turn head and
  optimistic revision; resume reconstructs from retained turns plus fresh authoritative refs.
- A skill receives an immutable context bundle and returns observations, turns, attachments,
  or a Proposal payload. It does not write the workspace, GitHub, coordinator records,
  projections, or default-branch repository truth. It may produce staged attachments in
  isolated working state; the daemon-side orchestration layer adopts accepted results.
- Staging a Proposal is not Publication. Only an explicit approval command can bind a version
  hash and authorize the target-specific publisher.
- A methodology session may retain opaque provider/session refs for diagnosis, but coordinator
  records and raw live state are not its long-term memory.
- Handoff creates a standalone ordinary build issue; downstream intake, coordinator, review,
  and merge behavior stay unchanged.
- Crash/retry semantics are stable IDs, expected revisions, command idempotency keys, and
  reconcile-before-repeat—not “rerun the whole prompt and hope.”

The workspace prototype may use one local SQLite file plus content-addressed fixtures, a fake
daemon command handler, and daemon-generated JSON read models. It may seed GitHub/repository
facts as read-only fixtures, but must not implement mutable issue/PR/CI state or let the UI read
the database directly. It can defer the real socket, external publishers, migrations, blob
retention policy, and multi-machine replication. Any user-facing prototype that becomes a
product UI still requires the repository's `/ui-mockups` gate before implementation.
