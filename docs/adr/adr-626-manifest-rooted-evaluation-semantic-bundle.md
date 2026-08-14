# ADR 626 — Manifest-rooted Evaluation v1 semantic bundle

Status: accepted
Date: 2026-08-13

## Context

[ADR 605](adr-605-canonical-evaluation-rulebook.md) made one versioned canonical
data contract the sole Evaluation v1 semantic authority. The preflight for
[#617](https://github.com/ConnorGriffin/agentflow/issues/617) then required an
independent generic interpreter for the contract's procedural semantics. The
provenance work on [#622](https://github.com/ConnorGriffin/agentflow/issues/622)
demonstrated that this boundary needs a custom schema and opcode VM, duplicates
algorithmic policy in the checker, and still does not close cold execution.

The settled Evaluation decisions from that provenance remain inputs to #617.
Only their abandoned schema/opcode representation is rejected.

## Decision

Evaluation v1 has one versioned, manifest-rooted semantic bundle. Its root is
`docs/evaluation/design/contract-v1.candidate.json`, which owns all declarative
policy: schemas, enums, artifact roles and paths, thresholds, denominators,
bounds, truth tables, authority parameters, and Evidence projections. The
bundle's one procedural authority is `agentflow/evaluation_semantics_v1.py`,
which owns deterministic schedule, lifecycle evaluation, authority and blinding
transitions, exact arithmetic, bootstrap, and Evidence constructor projections.

The root contract contains exactly this binding shape:

```json
{
  "semantic_module": {
    "interface_version": "evaluation-semantics-v1",
    "path": "agentflow/evaluation_semantics_v1.py",
    "source_sha256": "<64 lowercase hexadecimal characters>"
  }
}
```

`evaluation-semantics-v1` means exactly one public operation:

```text
evaluate_v1(contract, operation_id, input_value) -> result_value
```

The contract versions the binding and supplies every threshold, draw count,
bound, path, role, scope, and policy value. The module has no fallback policy
constants. It uses exact `int` and `fractions.Fraction` arithmetic and returns a
value accepted by the contract's declarative result schema. Production calls
this exact digest-bound operation; it does not translate or reimplement it.

The module source is at most 64 KiB and is pure Python using only the standard
library. Static imports are limited to `fractions.Fraction` and
`hashlib.sha256`. Its source and execution may not access the filesystem,
network, environment, clock, randomness, subprocesses, sockets, dynamic
imports, `eval`, `exec`, or plugin registries. It has no top-level effects other
than those imports and function definitions.

The standalone checker opens exactly three fixed regular files, with no-follow
semantics, from the repository root derived from its own path:

1. `docs/evaluation/design/contract-v1.candidate.json`;
2. `docs/evaluation/design/contract-v1.conformance.json`;
3. `agentflow/evaluation_semantics_v1.py`.

Its reviewed source contains an immutable whole-file SHA-256 lock for each. It
checks the exact bytes before trusting in-file bindings, and requires the
candidate's module path, interface version, and source digest to equal the fixed
binding and opened module. A one-byte mutation of any of the three files fails.

Loading is deterministic and fail-closed. After byte limit, digest, and UTF-8
checks, the checker parses the module with `ast`, rejects any import other than
the two approved symbols, rejects top-level executable statements, forbidden
APIs and names, dunder/reflection access, and any public callable other than
`evaluate_v1`. It compiles the already-audited tree and executes it in a fresh
namespace with the exact builtins mapping and import hook fixed by the
[candidate preflight](../research/evaluation-candidate-preflight.md). The module
source itself may not invoke or refer to `eval`, `exec`, `compile`, `open`, or
`__import__`; the checker's single
restricted load is not an Evaluation operation. Review also audits the complete
source for hidden fallback thresholds or policy constants, and the whole-file
lock binds the audited bytes.

The checker independently validates canonical JSON, schema grammar, structure,
references, coverage, lineage, bounds, paths, source bindings, and all three
whole-file digests. It audits the module's source, AST, imports, and public
surface. For every conformance vector it calls the bound `evaluate_v1` and only
then reads and compares the expected result. The module receives the validated
contract, operation ID, and input value; it receives neither the expected result
nor the conformance report.

This is independent validation of the bundle, not independent rederivation of
its algorithms. A checker-side implementation of schedule, bootstrap,
lifecycle, authority, blinding, eligibility, or Evidence construction would
create a second semantic authority and conflict with ADR 605's surviving
single-authority rule. Conformance vectors are independently reviewed evidence;
they exercise the canonical module but do not supply policy to it.

This decision supersedes only ADR 605's requirement that every Evaluation v1
semantic fact live in one data file. ADR 605's single authority, versioning, and
no-duplicated-policy rules remain in force. ADRs
[606](adr-606-explicit-missing-metrics-and-adjudication-lineage.md) and
[620](adr-620-evaluation-failure-classes.md) are unchanged.

## Alternatives

Keeping one data file plus a generic semantic interpreter was rejected because
the interpreter becomes a second algorithm implementation or a custom VM.
Putting procedural policy only in production runtime was rejected because the
candidate would not bind the reviewed implementation that conformance proves.
Stronger checker-side algorithm rederivation was rejected because it conflicts
with the single-authority rule.

## Consequences

[#617](https://github.com/ConnorGriffin/agentflow/issues/617) must produce and
lock the candidate, conformance report, and semantic module as one bundle.
[#622](https://github.com/ConnorGriffin/agentflow/issues/622) can preserve its
settled artifact, arithmetic, lifecycle, blinding, authority, missingness,
eligibility, and Evidence decisions without transcribing its abandoned custom
schema/opcode VM. The revised
[candidate preflight](../research/evaluation-candidate-preflight.md) owns the
unchanged extraction, canonical-byte, schema, limit, generator, error, CLI,
path-sanitization, no-write, and case-ownership mechanics.

This ADR adds no candidate, semantic module, checker, test, CI, or product
runtime implementation.
