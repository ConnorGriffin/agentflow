# ADR 628 — Read-only effective-policy briefings

Status: Accepted

Date: 2026-08-14

## Context

AgentFlow needs a bounded policy briefing before coordinator admission, but promotion,
Evaluation semantics, repository configuration, and coordinator persistence have separate
owners. Combining those authorities in a mutable coordinator service would let policy reads
silently acquire promotion or persistence powers and would make a missing or malformed input
ambiguous at launch time.

This decision begins from exact dependency merges:

- human-governed promotion #584: `ef08dd3d2f691aa154ddaa193e6161b559099396`;
- read-only promotion receipt authority #585: `bd818fa1d65c92def671192464207e6bc3904a34`;
- Evaluation v1 authority bundle #617: `121bc28b9dc65bbddf537396dae479bb259e7f52`.

The pinned Evaluation candidate SHA-256 is
`53359f35de57047441defa76a477564580b956f968ab6425356cca3a1c5a8409`; its bound
module SHA-256 is
`185f41a5e4549cc1ccbc4615af5846c3ed0f95285790d193e1b2f43aa3dc8554`.

## Decision

One `EffectivePolicyResolver` exposes `brief_for(repository, stage, subject_revision)` and
returns one immutable `briefing-v1` result. It reads only exact #584 receipts through
`PromotionReceiptReader.read`, the pinned Evaluation policy bundle, and an injected
`RepositoryOverlaySource.read`. It has no GitHub adapter, Evidence verb, coordinator Store
type, persistence operation, policy transition, or mutable result state.

The resolver folds fleet policy, then a same-repository overlay, then stage applicability.
An overlay can only remove existing receipt or capability entries, narrow an existing named
numeric maximum, add a closed hold, or mark an existing stage not applicable. Every new target,
wider bound, conflicting restriction, wrong repository/version, unreadable value, or malformed
canonical input fails closed. Receipt resolution binds every receipt and nested authority field
to the fleet policy and accepts only the exact `github-authority/v1` verified outcome and declared
scope.

`briefing-overlay-v1` and `briefing-v1` share one UTF-8 canonical JSON encoder: NFC strings,
sorted keys, compact separators, non-ASCII preservation, JSON integers only, and no non-finite
numbers. Duplicate members are rejected recursively. Object arrays are ordered by element
canonical bytes. Overlay and briefing self-digests omit only their declared identity fields.
The overlay is bounded at 8 KiB and the final briefing at 16 KiB; collection and nested-bound
limits are part of the closed contract. The exact effective-policy contract digest is
`783ebc4a6de2217b49130ae448f353a8c4ce62b712f0ce94cea49c53a7215c0d`.

The result vocabulary is ready, not applicable, or a hold with one of the eight closed codes.
Holds contain only validated tokens or digests as references. Rejected content, source prose,
provider output, findings, prompts, transcripts, and secrets never enter a result.

## Alternatives

- Resolve policy through `EvidenceStore.brief_for`: rejected because it is a mutable governed
  Evidence verb with retention behavior, not a read-only promotion-receipt authority.
- Let repository configuration add capabilities or permissions: rejected because an overlay
  would become a second promotion authority.
- Persist briefing identity in the resolver: rejected because coordinator admission #627 owns
  the transaction, identity reuse, zero-consumption holds, and receipt propagation.
- Return parser or source exceptions: rejected because unbounded rejected content would escape
  the trust boundary and expand the public failure vocabulary.

## Consequences

Briefing delivery is deterministic, content-free, and independently reusable by admission.
Overlay read errors, timeouts, and repositories or revisions unavailable at read time stop launch
through retryable `invalid_overlay`. Malformed or invalid successfully read immutable objects and
authority mismatches stop launch through `invalid_overlay_authority`. Repository overlays can
restrict but never widen fleet policy. Promotion and Evaluation remain unchanged; #627 must
combine the returned briefing with coordinator identity and persist it atomically before permit
acquisition.

Issue #571 adds one consumer check without widening resolver authority: a promoted method's exact
artifact locator scopes it to its owning stage before the resolver constructs the stage briefing.
Unrelated stages therefore keep the same briefing identity, prompt, admission receipt, and absence
of use attribution before and after that method is promoted. Before Review uses its advisory
receipt, its stage prompt verifies that the locator and SHA-256 name the deployed Review
methodology artifact. The briefing still delivers only receipt authority and never method prose.
A mismatch refuses before admission, and Store records the same receipt and method revision only
after this check succeeds.
