# Repository capabilities

AgentFlow separates the workflow engine from the capabilities a repository gives
its Claude and Codex sessions. The checked-in
`agentflow/capabilities.toml` manifest is the source of truth for those
capabilities, their versions, and their content hashes.

The manifest's `methodology_skills` entry is the sole authority for the exact
public methodology commit
(`08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666`). That immutable source contains
all three declared methodology skills; the older `v0.3.0` tag does not. Each
methodology capability also pins that commit as its version, plus its tracked
files, hashes, and direct dependency IDs. The
structured stage prompt graph keeps direct and conditional invocations separate
from those dependency edges; admission computes the complete closure in stable
declaration order.

The release-verification discovery controls
are `scripts/provider-discovery-probe.sh {claude|codex} {positive|negative}`;
CI runs only its non-provider helper seam in
`tests/test-provider-discovery-probe.sh`. A successful real positive probe writes
a repository-scoped native-discovery receipt bound to the resolved provider
executable and capability manifest. The deterministic seam writes no receipt.
When admission reports only a missing or stale native-discovery receipt, its
repair command is runnable directly:

```sh
agentflow capability-probe --repo /path/to/repository --provider codex
```

The command temporarily materializes a reserved probe skill in only the selected
provider root, requires provider-native invocation evidence, records the receipt,
and removes the fixture. Repeating it reuses the valid receipt without relaunching
the provider. Static-file failures continue to recommend enrollment instead.

## Inspect a repository

```sh
agentflow doctor --repo /path/to/repository
agentflow doctor --repo /path/to/repository --json
```

The command exits nonzero when a required capability or any selected dispatch
matrix cell is not ready. The default matrix covers every enabled unattended
stage and every context that repository can dispatch, for both `claude` and
`codex`; `--provider` and `--stage` only narrow that same readiness decision.
Mockup explicitly supports UI context only, so it is absent from a headless
repository's matrix rather than reported as a false headless cell.
Codebase Memory is optional.
UI capabilities become required only when the repository declares or contains a
user-facing surface. An explicit declaration is authoritative even when its path
does not match AgentFlow's conservative directory heuristics.

The JSON report is versioned with `schema_version`. Each capability has a
`status` of `ok`, `missing`, `drifted`, or `incompatible`, plus the exact repair
command where one is available. Each matrix cell records stage, context,
provider, required contract IDs and versions, evidence, repair command, and
readiness.

Doctor is a static inspection command: it verifies the trusted harness and skill
manifests before inspecting installed runtime metadata, and never executes
repository-managed JavaScript. Enrollment's explicit apply path runs the pinned
self-check after integrity verification.

## Enroll a repository

Enrollment is a dry run unless `--apply` is present:

```sh
agentflow enroll /path/to/repository --profile reviewed
agentflow enroll /path/to/repository --profile reviewed --apply
```

`reviewed` is the safe default. Apply requires the exact root of a clean Git
checkout with a resolvable GitHub origin. Before writing anything it validates
instruction shapes, the existing runtime configuration, required UI commands,
public skill destinations, and the public release tag. A failed preflight leaves
both the checkout and configuration unchanged. Claude-only instructions are
backed up and promoted into the shared `AGENTS.md`/`CLAUDE.md` layout; conflicting
or unsupported shapes are left wholly unchanged.

Enrollment writes only reproducible local wiring:

- shared `AGENTS.md` / `CLAUDE.md` repository instructions with one supported
  autonomy profile and an explicit `ui-surfaces` declaration;
- the bundled AgentFlow skill in both tools' project-local discovery paths;
- an idempotent `config.toml` repository entry;
- for UI repositories, the pinned Connor skill pack, screenshot harness, and
  the Playwright/Chromium runtime locked by `drive-local-webapp/package-lock.json`.

The Connor pack and methodology contracts are installed from the public release
and exact commit recorded in the manifest, with a pinned version of the skills
installer. Every tracked file in each required skill directory—including
executable scripts, package locks, agent metadata, and referenced prompts—is
checked against the deterministic file list and SHA-256 values in the manifest.
Missing, changed, and unexpected files fail readiness.
Readiness is provider-specific: Codex must discover the pinned contract from the
project's `.agents/skills` root, while Claude must have the matching project-local
`.claude/skills` copy. Both the exact static files and the current provider's valid
native-discovery receipt are required. User-global, ambient, and bundled copies are
ignored. Executable presence alone is insufficient, and a missing, drifted,
release-incompatible, dependency-incompatible, or undiscoverable selected-provider
contract fails closed. Project, skill-root, skill-directory, manifest-file, and
tracked-file symlinks or path escapes are incompatible.
Before invoking the installer, AgentFlow resolves lightweight or annotated
release tags with Git and requires the peeled commit to equal the manifest pin.
It then clones and checks out that exact commit into a temporary local source;
the installer never reads the movable tag. It invokes the installer only when
all managed destinations are truly absent. A regular file, broken symlink,
empty directory, partial install, or edited destination fails closed without
overwrite.

Admission checks the enrolled source before stage preparation, then verifies the
prepared `record.source` checkout before permits, attempts, or provider launch.
Pinned contracts may be materialized into a newly prepared worktree; retained
historical worktrees are checked at their actual launch root and otherwise enter a
named, zero-attempt environment hold. Optional provider migration probes are
non-mutating and cannot park the active stage.

Apply journals every managed repository path and the fleet configuration.
Failure during skill installation, npm installation, Chromium installation, or
either self-check restores the original bytes and symlinks, so the same command
can be retried immediately.

Relative repository paths already present in `config.toml` resolve from that
file's directory, matching the runtime configuration loader. Re-enrollment
therefore recognizes an existing relative entry instead of appending a duplicate.

Codebase Memory remains optional. Doctor recognizes a configured executable or
an onboarded checkout and otherwise prints the onboarding direction; AgentFlow
continues without it.

GitHub queue labels and pull-request CI are not mutated by this local command.
Enrollment prints that unverified step explicitly; confirm it before starting
the daemon.

## Extending the screenshot harness locally

The pinned `scripts/screenshots.mjs` is never edited in place: its SHA-256 is
recorded in `agentflow/capabilities.toml`, and a repo whose copy drifts from
that pin stops enrolling. A repo that needs capture behavior the shared harness
does not provide adds it through the sanctioned repo-local seam (ADR 735):

- Keep the extra behavior in a thin extension file — conventionally
  `scripts/screenshots.local.mjs` — that imports the pinned harness's exported
  entry points (`main`, `runConfig`, `captureShots`) and passes its local hooks
  (for example a `serve` stub for a vendor bundle, or an `applyTheme` override).
  `tests/fixtures/screenshots-local-extension.mjs` is the reference shape.
- Declare it with a `screenshot-entry: scripts/screenshots.local.mjs` line in
  the repo's `AGENTS.md`/`CLAUDE.md`. Build, revise, mockup, respond, and review
  sessions in that repo are then told to capture through the declared entry
  point; a repo declaring nothing keeps the unchanged canonical instruction.
- Enrollment upgrades a harness that still holds the current or a recorded
  previous pin, and refuses — with the recovery path named — to overwrite one
  carrying repo-local edits, so a local fork's work is never silently destroyed.
- AgentFlow's autonomous Review settlement parks a pull request that mutates a
  pinned path in an enrolled repo before its own merge arm, with the same
  redirection to this seam. This is not a GitHub required status check: a human
  merge, or any merge path that bypasses AgentFlow Review, is not blocked by it.
  Independently, the manifest-owning repo's normal pytest CI asserts that the
  shipped harness bytes match its recorded pin.

### Deliberately re-pinning the harness (owner repo only)

Changing the shared harness itself happens only in the repository that ships
the manifest, and always in lockstep within one pull request:

1. edit `scripts/screenshots.mjs`;
2. update the `screenshot-harness` capability's `sha256` in
   `agentflow/capabilities.toml` to the new file digest, and append the
   superseded digest to `known_old_sha256` so already-enrolled repos upgrade
   cleanly instead of all failing at once.

AgentFlow Review settlement recognizes exactly this lockstep shape as the
sanctioned path through; a harness edit without the matching manifest update is
parked even in the owner repo, because half a re-pin breaks enrollment for every
enrolled repo. It remains an AgentFlow merge-path guard, not a block on manual
or other non-AgentFlow merges.
