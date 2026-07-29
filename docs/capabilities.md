# Repository capabilities

AgentFlow separates the workflow engine from the capabilities a repository gives
its Claude and Codex sessions. The checked-in
`agentflow/capabilities.toml` manifest is the source of truth for those
capabilities, their versions, and their content hashes.

## Inspect a repository

```sh
agentflow doctor --repo /path/to/repository
agentflow doctor --repo /path/to/repository --json
```

The command exits nonzero when a required capability is missing or drifted.
At least one runner (`claude` or `codex`) is required; installing the second is
recommended but does not block readiness. Codebase Memory is optional.
UI capabilities become required only when the repository declares or contains a
user-facing surface. An explicit declaration is authoritative even when its path
does not match AgentFlow's conservative directory heuristics.

The JSON report is versioned with `schema_version`. Each capability has a
`status` of `ok`, `missing`, or `drifted`, plus the exact repair command where
one is available.

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

The Connor pack is installed from public release tag `v0.1.0`, whose expected
commit is recorded in the manifest, with a pinned version of the skills
installer. Every tracked file in each required skill directory—including
executable scripts, package locks, agent metadata, and referenced prompts—is
checked against the deterministic file list and SHA-256 values in the manifest.
Missing, changed, and unexpected files fail readiness.
Before invoking the installer, AgentFlow resolves lightweight or annotated
release tags with Git and requires the peeled commit to equal the manifest pin.
It then clones and checks out that exact commit into a temporary local source;
the installer never reads the movable tag. It invokes the installer only when
all managed destinations are truly absent. A regular file, broken symlink,
empty directory, partial install, or edited destination fails closed without
overwrite.

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
