# ADR 0049 — Repository capabilities are manifest-pinned and locally enrolled

- Status: Accepted
- Date: 2026-07-28

## Context

Installing the AgentFlow Python package did not reproduce the environment its
sessions assumed. UI stages invoked `/ui-mockups`, screenshot prompts assumed a
portable harness and Playwright, and private enrollment tooling configured these
facts outside the public repository. A clean machine could install the engine
successfully and still fail during an unattended stage.

Claude Code and Codex also discover project-local skills in different paths.
Relying on a maintainer's user-global skills makes a repository appear ready on
one machine while silently dropping capabilities on another.

## Decision

AgentFlow owns one versioned capability manifest. It pins the external skills
installer, the public Connor skills release tag and expected commit, the
Playwright version, and a deterministic path/SHA-256 manifest for every tracked
file in each required skill directory plus the screenshot harness. The release
tag is installable by the skills CLI; the file manifest makes a moved tag fail
closed. Apply resolves lightweight and annotated tags and requires the resolved
commit to equal the manifest before any write, then installs from a temporary
detached checkout of that exact commit rather than the movable tag. An
unexpected executable or prompt is drift, not a ready installation, and the
public installer runs only when all four destinations are truly absent—not when
a regular file, broken symlink, empty directory, partial install, or conflict
already occupies one.

`agentflow doctor --repo PATH` is the inspection interface. It reports required
and optional capabilities as `ok`, `missing`, or `drifted`, with a versioned JSON
form for automation. UI-only requirements activate from the repository's
declared or detected user-facing surfaces. Readiness requires at least one
installed runner; the second runner and Codebase Memory are optional.
Repository instructions are ready only when both tools resolve the same bytes,
one supported autonomy profile is present, and `ui-surfaces` is explicit.
Doctor never executes repository-managed code: static readiness requires exact
harness and complete skill integrity before installed runtime metadata is read.
The apply path performs the pinned runtime self-check after verification.

`agentflow enroll PATH --profile PROFILE [--apply]` is the mutation interface.
It is dry-run by default and defaults to `reviewed`. Apply preflights the exact
clean Git root and GitHub origin, instruction shapes, runtime-valid existing
configuration, required commands, public skill destinations, and release commit
before any mutation. Existing explicit UI declarations are authoritative;
undeclared repositories retain the conservative surface proposal. Claude-only
instructions are backed up and promoted, while unsupported or conflicting
shapes are left wholly unchanged. It installs the bundled AgentFlow skill into
the shared project-local skill layout and, for UI repositories, installs the
pinned Connor pack for both tools, copies the canonical screenshot harness, runs
the skill's locked `npm ci`, installs its pinned Chromium, and validates both
skill and harness self-checks.
Existing relative `config.toml` workdirs resolve from the config file's
directory, exactly as the runtime loader resolves them.
Apply journals every managed path and the fleet configuration. A failure in the
skills installer, npm, Chromium installer, or either self-check restores the
original bytes and symlinks before returning not-ready, leaving an immediate
retry possible.

The existing enrollment module remains the seam. Its legacy label sweep and UI
surface audit commands remain supported; the public CLI now exposes the deeper
repository enrollment and doctor interfaces.

## Alternatives

- **Vendor every methodology skill into AgentFlow.** Rejected: it forks upstream
  ownership and couples the engine release to unrelated methods.
- **Depend on user-global skills and Playwright.** Rejected: provider sessions may
  drop user configuration, and a new machine cannot reproduce it.
- **Track the skills repository's default branch.** Rejected: installation would
  change without an AgentFlow review.
- **Make Codebase Memory required.** Rejected: it improves structural discovery
  but is not necessary for the base pipeline.

## Consequences

- A clean machine can identify and repair every local prerequisite without
  reconstructing the maintainer's dotfiles.
- Updating any required skill file, harness, installer, or Playwright is an
  explicit manifest change reviewed with its content hash.
- UI enrollment installs more local tooling than headless enrollment; headless
  repositories do not require Node.
- GitHub labels and pull-request CI remain an explicit, separately verified
  enrollment step rather than an unreported side effect of the local command.
