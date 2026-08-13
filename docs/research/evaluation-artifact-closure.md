# Research: evaluation-artifact closure

## Scope and ruling

This is a repository-native research result for [#607](https://github.com/ConnorGriffin/agentflow/issues/607), based on the current checkout at `7a23ccc` (`origin/main`) and the current GitHub findings for [#603](https://github.com/ConnorGriffin/agentflow/issues/603) and [#604](https://github.com/ConnorGriffin/agentflow/issues/604). No product code was changed.

**Recommended ruling:** use one committed, canonical lock at
`docs/evaluation/preflight/evaluation-artifacts.lock.json` as the sole artifact
selection authority. It must contain:

- `reviewed_source_revision` as the immutable full Git commit ID of the source
  snapshot reviewed before the lock was added; it is provenance and must be an
  ancestor of the checkout, not equal to the lock-containing checkout `HEAD`;
- one closed, exact relative-path set for every reviewed verifier, grammar,
  fixture, negative fixture, digest/receipt, matrix, and focused test;
- one lowercase SHA-256 for every listed regular file;
- a canonical `root_sha256` over the complete normalized lock payload with the
  `root_sha256` field omitted from that calculation, including the path/digest
  entries and the lock's own declared schema/provenance fields;
- the exact public command and its exact path arguments, represented as data and
  executed without caller-side manifest reconstruction.

The lock verifier is the first executable in that command. It must fail closed
if the lock is absent, malformed, has duplicate or unexpected paths, contains
symlinks/path escapes, has a missing or drifted listed file, has a wrong root,
or names a malformed, missing, or non-ancestral `reviewed_source_revision`. It must
also reject any reviewed artifact under the evaluation root that is not in the
closed path set. After the lock passes, the same public command invokes every
listed verifier and focused test by the exact paths in the lock. The command
must exit non-zero on any failure and must not discover tests by basename,
directory glob, or a recomputed caller manifest.

CI should run this command in the existing Python job after `uv sync --frozen
--group dev`, with the checkout selected from the event SHA (`github.sha`) and
verified by `git rev-parse HEAD`. On the post-merge `push` to `main`, this is
the merged commit itself; it does not depend on a workstation checkout,
`origin/main`, or a later remote fetch. A pull-request job may run the same
command against its checked-out event revision, but the post-merge proof is the
`push` job's event SHA and exact checkout.

This is the smallest contract that proves the reviewed set and executed set
are equal: one reviewed lock supplies the set and digests, one command consumes
that set, and CI pins execution to the commit containing both.

## Repository evidence

### Existing locks and closed digest patterns

- `agentflow/capabilities.toml` already pins immutable source commits, selected
  capability file paths, and SHA-256 values. Its `files` arrays are the useful
  repository-native pattern for an exact path set; its `methodology_skills` and
  `connor_skills` entries show that a moving tag is not sufficient.
  [Source](https://github.com/ConnorGriffin/agentflow/blob/7a23ccc/agentflow/capabilities.toml)
- `docs/capabilities.md` states the intended invariant explicitly: every
  tracked file is checked against a deterministic file list and SHA-256 values,
  and missing, changed, or unexpected files fail readiness. It also requires
  the peeled release commit to equal the manifest pin.
  [Source](https://github.com/ConnorGriffin/agentflow/blob/7a23ccc/docs/capabilities.md#L90-L110)
- `tests/test_bootstrap_cli.py` tests the manifest as data, including exact
  commit/version pins and per-file digest maps. This is the strongest existing
  test-discovered convention for a closed repository contract.
  [Source](https://github.com/ConnorGriffin/agentflow/blob/7a23ccc/tests/test_bootstrap_cli.py#L57-L87)
- `scripts/audit-public-tree.py` demonstrates the public-check style: a fixed
  tracked-tree or reachable-history scope, deterministic enumeration, and
  non-zero failure rather than an advisory report.
  [Source](https://github.com/ConnorGriffin/agentflow/blob/7a23ccc/scripts/audit-public-tree.py#L45-L105)

The lock must include the terminal digest/receipt file as an ordinary listed
artifact and must verify the terminal file's bytes. The terminal file must not
become a second authority that can be omitted or silently replaced. The
`root_sha256` is over the entire normalized lock payload except its own value;
the declared path/digest set and provenance cannot be altered without detection
by the lock verifier. The verifier must reject duplicate JSON keys and non-canonical
or non-UTF-8 input, following the canonical-JSON and duplicate-key constraints
already described by #603.

### Test discovery and naming

The project-wide Python gate is `uv run pytest -q` (`CONTRIBUTING.md`,
`AGENTS.md`, and `.github/workflows/ci.yml`). Pytest discovery therefore proves
that a file matching project conventions is collected, not that a particular
reviewed test is executed. The focused test in the evaluation contract must
be named by its exact repository path in the lock and invoked as an explicit
pytest argument. The verifier must likewise be invoked as an explicit script
path. This prevents a reviewed basename mismatch or a hashed-but-unexecuted
test—the exact failure reported in #607—from passing broad discovery.

The existing console job uses the stronger explicit sequence `npm ci`, `npm
test`, `npm run build`, followed by `git diff --exit-code -- dist`; that is a
useful precedent for an explicit public command with a post-command tree check,
but it does not replace the evaluation lock.
[CI source](https://github.com/ConnorGriffin/agentflow/blob/7a23ccc/.github/workflows/ci.yml#L42-L62)

### Checkout and revision behavior

The Python CI job uses `actions/checkout` but does not currently set or verify
an explicit revision; the default checkout behavior is therefore not a
repository proof that the later command used the event commit. The proposed
contract separates two proofs that cannot share one self-referential field:
the lock's path/digest/root set proves reviewed artifact closure, while CI
supplies the event SHA and the command requires it to equal `git rev-parse HEAD`.
[CI source](https://github.com/ConnorGriffin/agentflow/blob/7a23ccc/.github/workflows/ci.yml#L17-L35)

The application has a separate, intentional `origin/main` convention for
interactive conversation worktrees: `agentflow/coordinated_converse.py` fetches
and detaches `origin/main`, and its tests construct a repository where that ref
exists. That behavior is unsuitable as the evaluation authority because it
depends on a remote ref and can differ from the event commit. The evaluation
contract must use the event SHA/current `HEAD`, not copy the conversation
worktree convention.
[Implementation source](https://github.com/ConnorGriffin/agentflow/blob/7a23ccc/agentflow/coordinated_converse.py#L198-L218)
[Test source](https://github.com/ConnorGriffin/agentflow/blob/7a23ccc/tests/test_converse_tracer.py#L269-L301)

## Reproduced #603/#604 findings

The #603 body records the right semantic shape: one read-only verifier, one
normative grammar, explicit artifact paths, a final digest manifest, and a
public command that runs the verifier plus focused layout test. Its acceptance
text also requires rejection of changed digests, grammar, vector IDs, expected
results, and negative-code mappings.
[Findings](https://github.com/ConnorGriffin/agentflow/issues/603)

The #604 final panel says the package was not ready because three independent
properties were still open: CI relied on a machine-specific checkout and
`origin/main`; the terminal digest manifest was not itself closed by the
verification chain; and the parent checker did not enforce the prerequisite
merges/digests it named.
[Final panel](https://github.com/ConnorGriffin/agentflow/issues/604#issuecomment-5280377247)

The contract above resolves those findings at the smallest boundary relevant to
#607: the lock owns the complete reviewed path/digest set and source provenance;
the lock verifier consumes and validates that set; the public command executes
every listed verifier and focused test; and post-merge CI separately binds the
run to `github.sha`/`HEAD`. Prerequisite ordering and semantic authority remain the
separate decisions in #605 and #608; #607 should not duplicate them.

## Rejected alternatives

1. **Caller-recomputed manifests.** Rejected: the caller can silently compute a
   different path or digest set from the checkout, which is the semantic
   substitution identified in #607. The lock is the authority; callers only
   consume it.
2. **A terminal digest manifest alone.** Rejected: #604 found that it could be
   absent or altered without a failure. It must be a listed artifact covered by
   the closed lock/root and exact-path verifier.
3. **`uv run pytest -q` as the proof.** Rejected: broad discovery can collect a
   changed test, miss a reviewed basename, or leave a hashed test unexecuted.
   Keep the broad gate, but add one public explicit evaluation command.
4. **`origin/main` or a local checkout path.** Rejected: both are environment
   or remote-ref assumptions. Use the CI event SHA and current detached `HEAD`.
5. **A second prerequisite/semantic registry in #607.** Rejected: #603 names
   #599/#600 as semantic authorities and #604 exposes the cost of naming
   prerequisites without enforcing them. #607 should prove artifact closure;
   #605/#608 own semantic authority and prerequisite gating.

## Verification contract summary

The recommended public command has this exact repository path and observable
shape:

```text
uv run python scripts/check-evaluation-artifacts-v1.py \
  --lock docs/evaluation/preflight/evaluation-artifacts.lock.json \
  --expected-head "$GITHUB_SHA"
```

The recommended CI change invokes that exact command after dependency
installation. The future command must require `--expected-head` to equal the
checkout's `git rev-parse HEAD`; verify lock schema/root, ancestral source
provenance, exact paths, and every digest; then
execute the exact reviewed verifier and focused-test paths from the lock. CI
must invoke only this command for the evaluation proof and fail if it returns
non-zero. The lock's `reviewed_source_revision` must identify an existing
ancestor as a full commit ID but must not be required to equal `HEAD`. The
command must print the event-`HEAD` identity and lock root in bounded output so
the post-merge run is auditable.
