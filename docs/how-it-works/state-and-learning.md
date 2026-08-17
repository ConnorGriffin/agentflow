# State, tags, and learning

*What the engine persists, how it pins its own tooling, and how it learns from outcomes.*

## State and persistence

The split of authority is explicit: **GitHub owns** issues, pull requests, branch state,
CI results, policy, and merge authority. **The coordinator owns** local claims, attempts,
permits, recovery, and state transitions.

Local state lives under `AGENTFLOW_STATE` (default `~/.agentflow`), and path construction
refuses escaping segments:

| Path | Contents |
|---|---|
| `coordinator/records.db` | Continuation store; the permit ledger authority |
| `coordinator/quota/` | Durable per-pool, per-window provider quota facts |
| `coordinator/sessions/` | Per-attempt provider event and result artifacts |
| `snapshot.json`, `live-sessions.json`, and peers | Atomically written console projections |

The projections carry a hard rule: **no production decision reads them.** They exist so a
human can see what is happening, and adding a decision that depends on them would turn a
display artifact into a control input.

??? info "Why the console reads a file instead of GitHub"
    The original dashboard queried GitHub itself, cached, and polled. On its first
    evening it exhausted the GitHub GraphQL quota of 5,000 points per hour — roughly
    8,600 queries per hour from a single dashboard, over the quota by itself — and
    starved the pipeline of the API budget it needed to actually work.

    The fix was to make the daemon the sole producer of the snapshot and the web server a
    pure file reader
    ([ADR 0026](https://github.com/ConnorGriffin/agentflow/blob/main/docs/adr/0026-daemon-owned-snapshot.md)).
    One snapshot production is about 36 GraphQL queries per daemon cycle, and — this is
    the property that mattered — that cost is constant regardless of how many watchers
    are open. The console cannot cost the pipeline anything.

    `POST /api/command` is a thin transport into the daemon's command channel, carrying
    an idempotency key and an expected aggregate revision. The web server never applies a
    domain transition itself.

## Tags, the other kind

Separately from GitHub labels, AgentFlow uses git release **tags** to pin the skill packs
its sessions run with
([ADR 0049](https://github.com/ConnorGriffin/agentflow/blob/main/docs/adr/0049-reproducible-repository-capabilities.md)).

`capabilities.toml` is the manifest of record. A skill pack is pinned by release tag *and*
the tag's peeled commit — for example tag `v0.3.0` alongside its exact commit — while
methodology skills are pinned directly to an exact commit with no tag at all. Per-file
SHA-256 hashes are pinned as well, and `skills-lock.json` mirrors each skill with its
source, source type, computed hash, and ref.

Enrollment resolves the tag with git and requires the peeled commit to equal the manifest
pin before installing anything. Then every tracked file in each required skill directory
is checked against a deterministic file list and its SHA-256. Only the skills a given
launch actually needs are materialized, and a native-discovery receipt — bound to the
provider executable's SHA-256 and the manifest's SHA-256 — is recorded per repository and
provider, proving the tools were actually discovered rather than merely present on disk.

!!! warning "Never move, retag, or delete a release tag"
    The pin is what makes a fleet's behavior reproducible. Moving a tag silently changes
    what every enrolled repository executes, without any diff appearing anywhere a human
    would look. The peeled-commit and per-file hash checks are what turn a moved tag into
    a loud failure instead of a quiet one — but the correct move is to cut a new tag and
    bump the pin deliberately.

## The learning pipeline

The learning pipeline is observational, and aggressively so:

```text
real terminal outcomes → observational report → human-reviewed methodology PR → later bounded observational cohort
```

`agentflow learning report` reads only terminal review and revise records plus per-attempt
telemetry, cohorts them by UTC date on their finalization time, and emits one
deterministic JSON document against a versioned schema. Missing telemetry is counted as
skipped and marks the report `degraded`; it is never coerced to zero. An unreadable or
old-schema store exits with an error and no JSON at all, rather than reporting on a
foundation it cannot verify.

The non-goals are the interesting part. The report has no provider, evaluation,
promotion, policy, admission, routing, safety, or canary action path. It makes no causal
claims and performs no automatic mutation. A human may read one report, propose a
methodology change through an ordinary reviewed pull request, then run a second bounded
report afterward — and that comparison remains observational, not causal. Nothing in this
path can change the engine on its own.
