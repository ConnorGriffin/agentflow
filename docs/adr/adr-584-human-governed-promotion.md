# ADR 584 — Human-governed promotion

Status: Accepted

Date: 2026-08-13

## Context

Evidence candidates are immutable facts, but activating a policy needs a human authority that is
independently verifiable without retaining GitHub prose or writing to GitHub. Fleet policy needs
one control repository; repository overlays need their owning repository.

## Decision

Promotion accepts only an exact merged GitHub PR authority. The canonical, revision-bound
`docs/evidence/promotion-scope-registry-v1.json` assigns fleet policy to
`ConnorGriffin/agentflow` and requires a repository overlay to originate in its target
repository. The loader accepts only that relative regular non-symlink path, its exact canonical
three-field JSON bytes, and matching Git revision and whole-file SHA-256.

`GitHubAuthorityVerifier` reads immutable PR merge, head, tree, artifact, linked-issue,
merge-actor, and permission facts through one injected read-only source. It returns a
deterministic approval ID only when every fact binds the authority pointer; the approval identity
also binds the PR head and merge-tree digests. It adds no Evidence verb.

`EvidenceStore.promote` atomically binds the candidate proposal digest and new policy version to
the pointer and requires the active scope version to be the declared prior version. Exact replay
returns its durable receipt without reconstructing unavailable source content.

## Alternatives

- Trust a mutable branch, issue body, or current file: rejected because it cannot prove the exact
  artifact a human approved.
- Accept fleet approval from any enrolled repository: rejected because ownership would be
  ambiguous and forgeable.
- Let the verifier write or widen Evidence: rejected because authority lookup is a read-only seam
  behind the existing promotion verb.

## Consequences

Promotion fails closed when registry or GitHub facts are unavailable, stale, edited, deleted,
cross-repository, mismatched, or permission-insufficient. Receipts retain immutable identifiers
and digests, while existing Evidence migrations and legacy receipt handling remain unchanged.
