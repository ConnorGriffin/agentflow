# ADR 735 — Repo-local screenshot-harness extension seam

## Status

Accepted (issue #735).

## Context

The fleet pins `scripts/screenshots.mjs` by SHA-256 in
`agentflow/capabilities.toml` (ADR 0049): reproducible capture behavior in every
enrolled repo, verified before every launch. The pin treated *any* repo-local
change as tampering, but repos have legitimate capture needs the shared harness
does not cover. That left two bad outlets, both observed in production:

- ciq-autotune PR #659 edited the pinned file in place — every subsequent stage
  for the repo refused to launch (`capability_environment_failure`) until a
  human intervened; nothing at review warned about it.
- The alternative, forking the whole 447-line harness locally, has no contract
  and silently rots; a capability refresh once overwrote such local work
  unnoticed, destroying six merged pull requests' worth of capture behavior.

## Decision

Extension happens through a sanctioned seam; the pinned bytes never move except
in the owning repo's deliberate re-pin.

1. **The harness is a callable module, not just a CLI.** It exports
   `captureShots`, `runConfig`, and `main` (a direct-invocation guard keeps an
   import from running the CLI), and it grew the generic capture features the
   observed forks were built for: `serveRoot` disk serving with prefix
   rewriting (an unmatched request aborts the run), key-wise per-shot
   `defaults`, seeded browser storage, focus-then-keypress steps, element
   cropping, and a scroll-reset opt-out. Repo-specific behavior arrives as
   hooks (`serve`, `applyTheme`, …) passed by a thin local wrapper —
   conventionally `scripts/screenshots.local.mjs`, reference shape in
   `tests/fixtures/screenshots-local-extension.mjs`.
2. **The wrapper is declared, and sessions are pointed at it.** A
   `screenshot-entry:` line in the repo's `AGENTS.md`/`CLAUDE.md`
   (`agentflow/repo_facts.py`) routes every stage's capture instruction
   (`agentflow/screenshot_crib.py`) to the declared entry point; silence keeps
   the canonical instruction unchanged.
3. **Enrollment never destroys local work.** Converge upgrades a harness still
   holding the current or a recorded previous pin, and refuses — naming the
   file and the recovery path — to overwrite one carrying any other bytes
   (`agentflow/enroll.py`).
4. **Pinned-path mutation fails a blocking check before merge.** Review
   settlement parks a PR that mutates a pinned path (`agentflow/gate.py`,
   `pinned_mutation_gap`) with an actionable reason pointing at the seam — the
   signal arrives before merge, not as a post-merge enrollment brick.
5. **The owner repo re-pins in lockstep.** The one sanctioned way through the
   check: the repository that ships the manifest moves the pinned bytes and the
   recorded digest in the same PR, appending the superseded digest to
   `known_old_sha256` so enrolled repos upgrade cleanly. Per-repo pin overrides
   stay rejected — the pin remains fail-closed (ADR 0049).

## Consequences

- A repo adds capture behavior in a ~30-line wrapper; the shared harness keeps
  one digest fleet-wide, and enrollment keeps converging.
- Legacy full forks (ciq-autotune's interim split) migrate to the wrapper in
  their own repos; the seam lands here first.
- The launch-side owner exemption for the harness's own checkout (PR #744) is
  unchanged and orthogonal: it governs launching with in-flight owner edits,
  not merging them.
