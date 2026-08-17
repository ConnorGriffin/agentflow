# ADR 732 — Permit budget is runtime configuration

Status: Accepted

Date: 2026-08-16

## Context

Each provider pool's permit budget (PERMIT_BUDGET in agentflow/coordinator/admission.py)
caps the total concurrent reserved work; sessions reserve their whole demand atomically.
PR #731 changed the packaged default from 5 to 25 to allow multiple concurrent builds in
production. This broke approximately 10 capacity-sensitive tests whose saturation
arithmetic hardcodes the permit budget constant, causing main's full CI to fail red. The
tests encode the defensible value 5 as the test-semantics anchor; the production need for
25 conflicts with that constant.

The flip-flop happened without an ADR, leaving operators without a durable record of which
value belongs where and why.

## Decision

Capacity numbers that behave like operational policy live in daemon launch configuration,
not in the packaged constant. The packaged default for PERMIT_BUDGET remains 5 — the
reviewed value the concurrency-semantics tests encode — and is changed only deliberately
with its test suite.

Production operators set the effective value via AGENTFLOW_PERMIT_BUDGET environment
variable (set to 25 in the operator's launchd plist in their dotfiles repository). Changing
the env var requires no code change and no test churn.

## Alternatives

- Keep the packaged default at 25, rewrite all tests: rejected because it moves the test
  anchor (5) outside the codebase, making concurrency semantics implicit rather than pinned.
- Parameterize the tests without changing the code default: rejected because it obscures
  which value is production-reviewed and enables gradual test-skew drift.

## Consequences

The packaged constant and production deployment have independent values. By-hand invocation
without the env var sees 5, acceptable since the daemon is the only unattended dispatcher.
Test saturation arithmetic derives from the constant, stable across deployments. Operators
must know the effective value comes from the plist; documentation should note that
omitting AGENTFLOW_PERMIT_BUDGET reverts to the packaged default for diagnostic or
single-run use.
