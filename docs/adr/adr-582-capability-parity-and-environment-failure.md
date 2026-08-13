# ADR 582 — Capability parity and environment failure

Status: accepted

## Context

An unattended prompt could name methodology that a selected provider did not reproducibly have.
Late shell failures then spent a permit and attempt without a named repair path.

## Decision

`StagePromptSpec` owns rendered instructions and structured direct invocations for every enabled
unattended stage: Build, Revise, Mockup, Respond, Intake, Review, Converse, Research, and Attack.
Direct invocations carry conditional context; each requirement carries its own explicit dependency
edges. `requirements_for` walks that graph in declaration order to produce the deterministic complete
closure. The enabled-stage inventory is tested against the structured production render calls, so a
new dispatch cannot exist only in doctor metadata.

Before a provider is admitted, a typed capability preflight checks the selected provider's pinned
project-local discovery contract. Codex reads the `.agents/skills` contract; Claude must have the
matching `.claude/skills` project reference. Ambient/global skills never count. Manifest version and
dependency mismatches, invalid provider discovery, and unavailable runtimes are incompatible;
absent and hash-mismatched project contracts are missing and drifted. Every state fails closed.

Doctor evaluates the same preflight over every supported stage/context/provider cell. Headless
repositories omit UI-only contexts, and Mockup explicitly supports only UI context; `--stage` and
`--provider` narrow this matrix rather than selecting a separate check.

Missing, drifted, and incompatible contracts are environment failures: they retain the claim on a
named human hold and create neither Evidence nor attempt telemetry. Capability preflight remains
before stage preparation, permits, attempts, or process launch. Shell failures after launch remain
charged attempt outcomes.

The release probe uses runner-equivalent Claude and Codex invocations with positive and negative
project-local discovery controls. It does not claim those flags eliminate ambient instructions.

## Consequences

Enrollment and doctor expose one deterministic repair command. Enrollment refuses partial,
conflicting, or drifted destinations and installs the manifest's exact release into both provider
paths. AgentFlow does not fall back to a weaker or differently equipped provider, and optional
Codebase Memory remains optional.

The methodology source is pinned to exact published commit
`08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666`, not the older `v0.3.0` tag: that tag
does not contain the declared `codebase-design` and `domain-modeling` contracts. Enrollment
checks out the immutable commit, installs each declared skill separately, wires Claude's
project-local discovery links, and verifies every pinned file before reporting readiness.
