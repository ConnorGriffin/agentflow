# Research: executable evaluation prerequisite gate

Status: research finding for Wayfinder #608.  This report does not implement the
checker or change the Evaluation Contract.

## Recommended ruling

Adopt one closed, canonical dependency-facts record and one fail-closed checker
with two call sites: `pre-adr` and `complete`.  The record is the smallest durable
join of the four facts that prose currently leaves separable:

```yaml
format: evaluation-prerequisite-v1
subject: agentflow-evaluation-v1
prerequisites:
  - id: grammar
    issue: 603
    review_verdict: CONVERGED
    merge_commit: <full-40-hex-merge-sha>
    artifact_digests:
      <repository-relative-path>: <full-64-hex-sha256>
  - id: blinded-authority
    issue: 604
    review_verdict: CONVERGED
    merge_commit: <full-40-hex-merge-sha>
    artifact_digests:
      <repository-relative-path>: <full-64-hex-sha256>
adr:
  issue: 583
  path: docs/adr/adr-583-frozen-evaluation-contract.md
  sha256: <full-64-hex-sha256>
  binds:
    - id: grammar
      merge_commit: <same-grammar-merge-sha>
      artifact_digests: <same-map-as-above>
    - id: blinded-authority
      merge_commit: <same-authority-merge-sha>
      artifact_digests: <same-map-as-above>
```

The proposed implementation may serialize this as canonical JSON; the shape above
is the recommended contract, not a requirement to add a YAML parser. Object keys and
prerequisite IDs are closed; IDs are unique; paths are repository-relative; SHA
values have exact lengths and lowercase hex; and the ADR binding must repeat the
same commit/digest values rather than merely naming issues.

The two required child records are deliberately explicit.  #599's normative
amendment says #603 must merge with its exact merge commit, final grammar digest,
and focused repository-layout test, and #604 must merge with its exact merge
commit and terminal digest; only after both bindings exist may ADR 583 be created
and indexed.  [#599 amendment](https://github.com/ConnorGriffin/agentflow/issues/599#issuecomment-5280213149)
This imports #603/#604 conformance rules instead of restating them.  The source
payloads and verifier are themselves authoritative issue artifacts:
[#599 contract payload](https://github.com/ConnorGriffin/agentflow/issues/599#issuecomment-5279074982),
[#599 oracle and verifier payload](https://github.com/ConnorGriffin/agentflow/issues/599#issuecomment-5279075529).

## Checker behavior

`pre-adr` must return success only when all of the following hold:

1. The record is canonical, closed, bounded, and has exactly the two required
   prerequisite IDs (`grammar`, `blinded-authority`).
2. Each prerequisite has the required terminal review verdict, an exact full
   merge SHA, and at least the named artifact digests.  A missing, non-terminal,
   conflicting, or unreadable review is a blocking result.
3. GitHub facts prove exactly one merged closing PR for the prerequisite issue;
   its merge commit equals the record; and git proves that full commit is an
   ancestor of the checked target revision.  A label, branch name, issue closure,
   or short SHA is not proof.
4. Every listed artifact is a regular file under the repository root and its
   whole-file SHA-256 equals the record.  Missing, extra-required, changed,
   symlinked, or unreadable artifacts block.

`complete` performs every `pre-adr` check, then requires the ADR file to exist,
hash to `adr.sha256`, contain the exact ADR 583 identity, and bind both child
merge/digest sets byte-for-byte to the record.  It returns nonzero for any
missing or mismatched binding.  The caller must run `pre-adr` before writing or
indexing ADR 583 and run `complete` before reporting the Evaluation Contract
substrate complete; neither phase may create the ADR as a side effect.

The checker should emit one bounded, machine-readable failure code per failed
fact (for example `review.missing`, `pr.not-merged`, `merge.not-ancestor`,
`artifact.digest`, `adr.binding`, `github.unreadable`, or `git.unreadable`) and
exit nonzero.  It must never downgrade an unreadable read to an empty set or a
green result.  Success should print the checked target revision and the record
digest, not artifact contents or subprocess output.

## Why this fits the repository

The existing GitHub seam already makes the important distinction: issue/PR reads
return `None` on command, timeout, or parse failure and preserve real empty values
as empty collections ([`agentflow/github.py`](../../agentflow/github.py#L480-L490),
[`agentflow/github.py`](../../agentflow/github.py#L502-L517)).  `pr_facts` supplies
the branch, head commit, state, and closing-issue references as one snapshot, so
the gate can reject half-confirmed merge facts ([`agentflow/github.py`](../../agentflow/github.py#L502-L517)).
The existing head-check reader is also explicitly tied to one exact commit and
treats a missing commit as unreadable, never green ([`agentflow/github.py`](../../agentflow/github.py#L844-L900)).

Review proof is already exact-head shaped.  The structured verdict requires
`reviewed_sha`, `final_sha`, and `pushed_sha` fields ([`agentflow/reviewer.py`](../../agentflow/reviewer.py#L55-L120));
the public tests show that a missing or mismatched expected SHA is not clean and
that prose-wrapped JSON is rejected ([`tests/test_reviewer.py`](../../tests/test_reviewer.py#L98-L122)).
The new record should join those facts to a merged commit; it should not treat a
PASS string or a closed issue as a substitute.

Dependency ordering has the same fail-safe precedent: ADR 0024 requires every
declared blocker to be closed and says an unreadable dependency graph never
dispatches ([`docs/adr/0024-dependency-aware-dispatch.md`](../adr/0024-dependency-aware-dispatch.md#L27-L59)).
The evaluation gate is stricter in kind, not broader in mechanism: every required
fact must be present and mutually bound before the downstream phase runs.

## Test seam and CI entry point

Add public-interface tests beside the checker.  Use a fake GitHub adapter returning
typed issue/PR snapshots and a fake git executable or injected git runner for
`merge-base --is-ancestor`, object identity, and target revision reads.  Exercise
at minimum: missing review, non-terminal verdict, two closing PRs, wrong merge
SHA, non-ancestor merge, unreadable GitHub/git, changed artifact, symlink artifact,
ADR digest mismatch, ADR binding mismatch, and the positive record.  Existing
tests demonstrate the repository's fake-git pattern and PATH isolation
([`tests/test_coordinator_launcher.py`](../../tests/test_coordinator_launcher.py#L924-L942),
[`tests/test_coordinator_launcher.py`](../../tests/test_coordinator_launcher.py#L1061-L1078)).

The CI entry point is `scripts/check-evaluation-prerequisites-v1.py`, invoked
from the existing Python job after the ordinary test suite and before
public-tree/link audits. CI runs `uv run python
scripts/check-evaluation-prerequisites-v1.py --record
docs/evaluation/preflight/evaluation-prerequisites-v1.json --target
"$GITHUB_SHA" --phase complete`. The command
must use a repository-relative target revision supplied by CI, not a developer's
machine-specific checkout or an implicit `origin/main`; this addresses the
portability blocker recorded by #604.  The current workflow has the natural
single job seam (`uv run pytest -q`, then the two public audits)
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml#L15-L29)).

## Rejected alternatives

- **Issue labels or `blocked-by` edges as completion proof:** rejected.  ADR 0024
  makes those useful for dispatch ordering, but they do not carry review verdicts,
  exact merge SHAs, artifact digests, or ADR content bindings.
- **A prose checklist in #599:** rejected by the question itself and by the #599
  amendment.  It permits a green build while a named prerequisite is absent.
- **One giant “all facts” blob copied from #599:** rejected.  It duplicates the
  child grammars and creates a second semantic authority.  The record should bind
  child outputs by exact commits/digests and import their rules.
- **Trust the latest PR/issue state without ancestor proof:** rejected.  A force
  push, rewritten history, stale branch, or multiple closing PRs can make that
  state ambiguous; exact merge identity plus ancestry is the smaller proof.
- **Have the checker create ADR 583 or synthesize missing facts:** rejected.  That
  makes the gate its own authority and violates the amendment's ordering.  It must
  only attest or block.

## Recommended acceptance test

The smallest acceptance test is a positive record plus one mutation per required
fact.  Every mutation exits nonzero with a stable code; the positive record passes
`pre-adr`; adding the correctly bound ADR makes `complete` pass; and changing any
child review verdict, merge commit, artifact digest, target ancestry, or ADR
binding makes `complete` fail before substrate completion can be reported.
