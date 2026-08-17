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
project-local discovery contract. Codex reads the `.agents/skills` contract; Claude reads the
separately materialized `.claude/skills` contract. Skill roots, directories, tracked files, and
manifest paths must be real and contained in that provider root; symlinks and escapes are
incompatible. Ambient/global skills never count. Manifest version and dependency mismatches,
invalid provider discovery, and unavailable runtimes are incompatible; absent and hash-mismatched
project contracts are missing and drifted. Every state fails closed.

Static files cannot prove native provider discovery. A real positive release probe records a
repository-scoped discovery receipt bound to the provider name, resolved executable bytes, and
capability-manifest bytes only after the expected native invocation evidence is observed. Production
preflight validates that receipt as well as the actual launch checkout. Missing, unreadable, stale,
or incompatible receipts fail closed. The named `agentflow capability-probe --repo … --provider …`
repair temporarily installs a reserved fixture in the selected project-local provider root, records
the receipt only after native invocation evidence, removes the fixture, and is idempotent when the
receipt remains valid. The deterministic CI probe seam never creates a receipt.

Doctor evaluates the same preflight over every supported stage/context/provider cell. Headless
repositories omit UI-only contexts, and Mockup explicitly supports only UI context; `--stage` and
`--provider` narrow this matrix rather than selecting a separate check.

Missing, drifted, and incompatible contracts are environment failures: they retain the internal and
visible claim on the clocked capability refusal path and create neither Evidence nor attempt
telemetry. After the authoritative launch-root observation, a deterministic missing subset earns
one named Capability repair through enrollment's locked seam: absent pinned Claude destinations
may be materialized from intact `.agents` sources, and an entirely absent provider-local Playwright
runtime may be installed from its committed lockfile. The result is re-probed once in that cycle;
only a ready re-probe admits. A failed unchanged repair is durably fingerprinted so later clock
observations do not repeat it. Drift, symlinks, partial or occupied runtime trees, and unknown
content are never rewritten and continue toward human escalation. Missing and verbatim-stale
discovery receipts are since [ADR 729](adr-729-receipt-repair-convergence.md) re-proven by the
discovery probe as a tail step of the same repair call — unreadable receipts still are not. A
non-mutating source/provider preflight remains before stage preparation. After preparation copies
missing pinned contracts from the enrolled checkout, a second preflight verifies `record.source` —
including retained Review and Revise worktrees — before permits, attempts, or provider launch.
Historical records derive a safe source root or fail into the same hold. A speculative migration
checks its destination provider before mutating the active record and cannot hold the healthy home
stage. Shell failures after launch remain charged attempt outcomes.

The release probe uses runner-equivalent Claude and Codex invocations with positive and negative
project-local discovery controls. It does not claim those flags eliminate ambient instructions.

## Consequences

Enrollment and doctor expose one deterministic repair command. Enrollment refuses partial,
conflicting, or drifted destinations and installs the manifest's exact release into both provider
paths. Coordinator may invoke only the absent-destination and absent-runtime subset described above;
its audit names the root, requirements, and outcome. AgentFlow does not fall back to a weaker or differently equipped provider, and optional
Codebase Memory remains optional.

The methodology source is pinned to exact published commit
`08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666`, not the older `v0.3.0` tag: that tag
does not contain the declared `codebase-design` and `domain-modeling` contracts. Enrollment
checks out the immutable commit, installs each declared skill separately, materializes both
provider-local copies, and verifies every pinned file before reporting readiness.
