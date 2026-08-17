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

`GitHubAuthoritySourceAdapter` is the production read-only source. Its typed GitHub reader fetches
the exact PR merge status, merge commit, head and merge tree, linked-issue disposition, merge
actor permission, and artifact file bytes at the pointer revision. Tests may inject that typed
reader without replacing the verifier. `GitHubAuthorityVerifier` returns a deterministic approval
ID only when every fact binds the authority pointer; the approval identity also binds the PR head
and merge-tree digests. Neither component adds an Evidence verb.

`EvidenceStore.promote` atomically binds the candidate proposal digest and new policy version to
the pointer and requires the active scope version to be the declared prior version. Exact replay
returns its durable receipt without reconstructing unavailable source content.

Issue #694 makes allocation explicit: a promotion must name the immediate successor of its prior
version, not merely a greater integer. The Evaluation bootstrap owns fleet-policy version 1, so the
first later fleet promotion is `1-to-2`; skipped versions and a second `0-to-1` transition are
rejected without weakening exact authority verification or the single-winner transaction.

SQLite schema v4 marks every newly verified receipt with the exact merged-PR promotion contract.
The transactional v3→v4 migration demotes all pre-584 `verified` receipts to
`legacy_unverifiable`; those receipts remain durable history but cannot replay as active policy.
The v1→v2→v3 migration chain remains intact and continues into v4.

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
and digests. Legacy receipts are deliberately inactive because their earlier authority scopes
cannot establish the merged-PR contract.
