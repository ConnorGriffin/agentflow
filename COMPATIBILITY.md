# Compatibility

AgentFlow is a pre-1.0, clone-only macOS beta.

## Supported surface

- macOS
- Python 3.11 or newer
- Git, GitHub CLI, and uv available on `PATH`
- Locally authenticated Claude Code and/or Codex CLI installations
- For UI repository enrollment: Node.js 18 or newer, npm, and npx (Node.js 20
  or newer is recommended)
- The current `main` branch

The repository tracks the built Svelte console. Headless repository enrollment
does not require Node.js, npm, or npx. UI enrollment and console development do.

## Stability

The command-line interface, configuration schema, state layout, Python modules,
snapshot schema, agent-provider adapters, and operational behavior may change
between commits. Before 1.0, compatibility is documented at the repository
level rather than promised for a fixed time window.

Changes that intentionally invalidate configuration or state should include
migration or rollback instructions. Accidental regressions are bugs, but fixes
and backports remain best effort. Only documented CLI commands and configuration
fields are public interfaces; Python imports and internal data structures are
not stable APIs.
