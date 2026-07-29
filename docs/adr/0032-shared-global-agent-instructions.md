# ADR 0032 — One machine-global instruction file for Claude and Codex

- Status: Accepted
- Date: 2026-07-15

## Context

ADR 0013 made the engineering charter canonical, but wired the tools to different
machine-global files: Claude kept personal preferences and imported the charter;
Codex pointed directly at the charter. Personal guidance therefore applied to
Claude only and drifted silently.

Claude supports direct Markdown imports. Codex loads one global `AGENTS.md`, but
can follow Markdown documents referenced from it. That allows both tools to share
one global file without copying the charter into it.

## Decision

Private tooling owns one machine-global instruction file. Both
`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` symlink to those same canonical
bytes.

That shared file references agentflow's `standards/CHARTER.md`; Claude imports the
reference directly and Codex follows the referenced guidance. The charter remains
its own canonical document and cross-review rubric. Codex reference traversal was
verified with an ephemeral read-only session against the shared global file.

`enroll-standards.sh` verifies the neutral private-tooling source, ensures it references
the charter, and points both tools at it. Only the known retired links—Claude's
old private-tooling path and Codex's direct charter link—are migrated automatically.

Per-repository wiring remains unchanged: `AGENTS.md` is canonical and
`CLAUDE.md` symlinks to it.

Automated provider stages are the deliberate exception to global-file delivery.
Their launchers suppress user configuration to keep private instructions and
connectors out of unattended sessions, then inject the exact contents of
agentflow's canonical `standards/CHARTER.md` through the shared prompt seam.
Source clones keep that file as the sole canonical source; release builds embed
the same bytes as an `agentflow` package resource for installed runners.
The launch fails closed when that file is missing or empty. Interactive Claude
and Codex sessions continue to receive the shared machine-global file described
above.

## Alternatives

- **Keep separate native global files.** Rejected: personal instructions can
  silently reach one tool but not the other.
- **Generate a combined Codex file.** Rejected: generation introduces a sync step
  and stale output—the failure this decision removes.
- **Move personal preferences into the engineering charter.** Rejected: operator
  preferences and the cross-review rubric are different concerns.

## Consequences

- A personal instruction edit reaches Claude and Codex immediately.
- Charter edits remain single-source and reach both tools through the shared file.
- Automated stages receive the same canonical charter without receiving personal
  machine-global instructions.
- No generated files or manual synchronization step exists.
- The global source uses tool-neutral wording; tool-specific preferences must be
  labeled explicitly.
