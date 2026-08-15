# AgentFlow

[![CI](https://github.com/ConnorGriffin/agentflow/actions/workflows/ci.yml/badge.svg)](https://github.com/ConnorGriffin/agentflow/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](docs/public-beta.md)

AgentFlow is an unattended GitHub issue → pull request engine for one operator.
It grounds an approved issue, builds in an isolated worktree, reviews the exact
pushed commit, and applies the repository's merge policy.

> **macOS beta:** AgentFlow launches authenticated local coding agents, GitHub
> CLI, Git, and uv. Run it only where unattended coding sessions are acceptable.

## Mental model

GitHub and the repository are the durable authority. AgentFlow executes ordinary
build issues; it does not own planning conversations, issue tracking, or
repository decisions. The console is a read-only projection.

[Wayfinder](docs/adr/0027-wayfinder-planning-boundary.md) explores uncertainty
and hands a cleared, independently shippable subtree to AgentFlow as an ordinary
Build Issue:

```text
uncertainty → Wayfinder decision map → clear Build Issue → AgentFlow pipeline
```

The planning/execution boundary is the product's central promise: Wayfinder and
the operator decide what work means; AgentFlow executes and reviews that work.

## Shipped status

The issue-to-PR pipeline, structured intake, provider dispatch, isolated builds,
exact-head review, bounded recovery, autonomy profiles, and read-only console
are on `main`. The observational learning report is read-only and reports real
terminal review/revise facts; it does not diagnose causes or change the engine.

## Tiny start path

```bash
git clone https://github.com/ConnorGriffin/agentflow.git
cd agentflow
uv sync --group dev
uv run agentflow doctor --repo /absolute/path/to/repository
uv run agentflow enroll /absolute/path/to/repository --profile reviewed --apply
uv run agentflow check
uv run agentflow service install
uv run agentflow resume
```

The daemon starts paused. Requirements, enrollment, capacity calibration, first
run, and new-machine recovery live in [Get started](docs/getting-started.md).

## Find the right guide

- [Get started](docs/getting-started.md) — requirements, install, enrollment, calibration, first run, and recovery.
- [Understand the pipeline](docs/pipeline.md) — stages, authority, review, revise, merge, and recovery.
- [Operate AgentFlow](docs/coordinator-operations.md) — pause, drain, upgrade, diagnosis, and rollback.
- [Repository capabilities](docs/capabilities.md) — generated contracts and readiness.
- [Evidence contracts](docs/evidence/README.md) — evidence interfaces and wire contracts.
- [Learning pipeline](docs/learning-pipeline.md) — observed outcomes, human methodology review, and deferred evaluation.
- [Contribute](CONTRIBUTING.md) — development, tests, pull requests, and DCO.
- [Product and design](PRODUCT.md) / [DESIGN.md](DESIGN.md) — product boundary and UI/design authority.
- [Domain glossary](CONTEXT.md) — shared terms.
- [Decisions](docs/adr/README.md) — accepted architecture decisions.
- [Policy and support](docs/public-beta.md) / [SUPPORT.md](SUPPORT.md) — beta boundary, compatibility, security, governance, and support.

AgentFlow is an Apache-2.0, clone-only macOS beta. Only current `main` is
supported; there is no API-stability, response-time, or paid-support guarantee.
