# Compatibility

AgentFlow is a pre-1.0, clone-only macOS beta.

## Supported surface

- macOS
- Python 3.11 or newer
- Git, GitHub CLI, and uv available on `PATH`
- Locally authenticated Claude Code and/or Codex CLI installations
- The current `main` branch

The repository tracks the built Svelte console, so Node is required only for
console development.

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
