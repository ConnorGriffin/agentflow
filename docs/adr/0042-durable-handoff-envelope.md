# ADR 0042 — One module owns the crash-safe human-handoff envelope

- Status: Accepted
- Date: 2026-07-19

## Context

Whenever a stage hands work to a human — parks a reviewed PR, holds an issue at intake or
mockup, settles a respond — it runs the same crash-safe recipe so a daemon that dies
mid-handoff and restarts does not double-act:

1. read the subject's comments;
2. post a marker comment **only if it is not already present**;
3. re-read and **prove** the marker landed — if it cannot be proven, return nothing so the
   next cycle retries (proof is deliberately withheld to force the retry);
4. notify the operator **exactly once**, only when the marker was newly posted, keyed by a
   sequence id derived from the record identity.

This envelope is re-implemented across `_hold_build`, `_hold_mockup`, `_park_pr`,
`_park_respond`, `_park_review_settlement`, `_settle_respond`, `_settle_mockup`, and intake's
two holds. The `sha256(record.identity)` sequence-id idiom is copy-pasted with inconsistent
lengths (12 versus 24 characters). The hard invariant — exactly-once notification across
crashes, proof withheld to force a retry — therefore has seven-plus homes, and this is the
highest-stakes duplication in the engine: a double-ping or a park that notifies before the
comment is proven is an operator-facing failure.

## Decision

### One call owns the whole envelope

Introduce a `DurableHandoff` module whose single operation runs the entire crash-safe
envelope. The caller supplies the marker text, the action to perform when the marker is
absent, and the notification payload; the module does read → act-iff-absent → re-read-and-
prove → notify-once and returns the durable proof (the subject URL) or `None`. The ordering —
the exact thing that must never be reassembled wrong — lives in one place and is impossible
for a stage to get out of order.

Separate composable primitives (`ensure_comment`, `prove`, `notify_once`) were rejected: they
would let each stage re-wire the ordering and reintroduce the double-ping / prove-before-notify
class of bug this module exists to delete.

### The module owns the notify-once key

`DurableHandoff` derives the notification sequence id itself, from the record identity plus a
stage tag, at one fixed length — replacing the copy-pasted 12-versus-24-character hashes. The
marker comment's presence remains the idempotency signal for the *action*; the derived
sequence id is the idempotency signal for the *notification*. Both are owned here.

### The envelope is thin; stage bookkeeping stays with the stage

The module owns only the comment-marker-plus-notify-once envelope. Stage-specific bookkeeping
that some paths do — removing a finished review checkout, recording ratchet state — stays in
the stage and runs after the call confirms (returns non-`None`). It therefore runs a moment
after the notification instead of before, which is immaterial: both are idempotent and
independent. Keeping bookkeeping out preserves the module's focus and locality, mirroring the
thin-seam choice in [ADR 0040](0040-github-access-module.md) (mutations report status; proof
is layered, not baked into every write).

### Built on the GitHub-access module; additive keystone

`DurableHandoff` reads comments and posts the marker through the `github` module
([ADR 0040](0040-github-access-module.md)), so its introduction is ordered after that module
exists. The keystone adds `DurableHandoff` and its tests and migrates zero callers, so it
cannot change pipeline behavior. The seven-plus hold/park/settle sites migrate to it later, in
behavior-preserving batches, serialized on `coordinated_build.py` against the other
candidates' migrations ([ADR 0038](0038-conflict-resolution-as-revise.md)). It directly
implements [ADR 0028](0028-stage-scoped-continuations.md)'s exhaustion-handoff contract.

## Alternatives considered

- **Composable primitives instead of one call.** Rejected: the ordering is the bug; loose
  pieces let stages get it wrong again.
- **Prove-on-write baked into the `github` module's mutations.** Rejected in
  [ADR 0040](0040-github-access-module.md): fire-and-forget callers would change behavior and
  the prove-then-notify recipe belongs here, layered on top.
- **Pull stage bookkeeping (worktree cleanup, ratchet) into the shared call.** Rejected: it is
  stage-specific and would widen the envelope's interface with per-stage concerns, eroding the
  locality the module exists to create.

## Consequences

- The crash-safe ordering is fixed once and cannot be reassembled wrong; one exhaustive
  crash-window test replaces the partial re-verification scattered across four tracer test files.
- Notification keying becomes uniform across every handoff.
- **The notification is at-least-once, not exactly once.** The Decision above asks for
  exactly-once, and the envelope cannot deliver it. Pinging only on the cycle that *posts* the
  marker means a daemon that dies after the comment reaches GitHub and before the push goes out
  never pings at all — the next cycle sees the marker, reads itself as a repeat, and the human
  the pipeline is waiting on is never told. The window is seconds wide, because the posting
  action goes on to edit the title and shuffle labels before it returns. Exactly-once would
  require durable notification state, which this module deliberately does not keep, so the
  envelope pings on every cycle that can *prove* the marker. The accepted cost: a crash between
  the call returning and the stage retiring its record, or bookkeeping the stage retries
  afterwards, sends the operator a second copy of the same ping. For "the pipeline needs a
  human", a duplicate ping is plainly better than a silent drop.
- **Nothing may lean on the client collapsing that duplicate.** The sequence id is sent to ntfy
  as a header, and ntfy has no header that deduplicates or replaces a notification: its own
  sequence ids are a URL path segment and only update a *scheduled* message before it is
  delivered. The id remains a stable per-handoff key; the reader really does see two pings.
- **The marker is derived, never the comment's own text.** Two genuinely different holds that
  compose the same words would otherwise read as one, and the second would post nothing and ping
  no one. The envelope derives markers from the record identity plus the reason for the handoff —
  the shape the PR park already used — and accepts a second "also proves this" string so a hold
  posted under an older marker format is not commented on twice after a deploy.
- **Durable writes alongside an envelope action must converge after the marker suppresses replay.**
  The envelope's `action` runs only on cycles where the marker is absent, so once the comment
  exists it never runs again. Any other durable write the action performs — a route's labels and
  title, or a state label — is therefore never retried by the envelope, and a stage that gates its
  own proof on such a write must be able to converge without a second envelope-run action. Two
  placements do that. The write may live outside the action and run after `hand_off` returns
  non-`None`, unconditionally, when it is cheap and idempotent, as with Research's state and claim
  bookkeeping. Or it may live inside the action and be repaired once after the call, guarded by a
  read, when unconditional replay could revert a maintainer's edit, as with the Build hold and
  Intake route projection. What is not allowed is proof gated on a write that exists only inside
  the action: after a crash between the comment landing and that write, the marker suppresses the
  action forever and the proof cannot converge.
- Ordered after the `github` module; additive keystone. Its migrations (holds, parks, settles,
  intake holds) serialize with the other candidates on shared files.
