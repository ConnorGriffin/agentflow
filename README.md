# agentflow

A tool-agnostic, **autonomous issue → PR → review pipeline** that runs on either
Claude (Opus) or Codex (GPT-5.6 Sol), tuned per repo by a single **autonomy
profile** dial — from a vibe-code project that merges its own green PRs to a
medical-adjacent repo that waits for a human.

> **Status: macOS beta.** AgentFlow launches authenticated local Claude, Codex,
> GitHub CLI, Git, and uv processes. Run it only on a machine where those tools
> are installed and where unattended coding sessions are acceptable.

## Why this exists

Both coding agents are now full-loop autonomous (GPT-5.6 Sol shipped 2026-07-09).
AgentFlow treats the tool as an interchangeable **runner**. The durable difference
between repositories is **domain risk**, expressed as an autonomy profile.

## Install from a clone

AgentFlow currently supports a clone-based beta installation:

```bash
git clone <repository-url> agentflow
cd agentflow
uv sync --group dev
```

The built console is tracked in the repository and included in release artifacts;
running AgentFlow does not require Node.

## Configure

Create `~/.config/agentflow/config.toml`:

```toml
[[repositories]]
repo = "owner/repository"
workdir = "/absolute/path/to/repository"

# Optional: expose this repository in the Project workspace.
workspace = true
```

Add one table per enrolled repository. Relative `workdir` values resolve from the
configuration file. Override the location with `AGENTFLOW_CONFIG` or `--config`.
Validate without starting the daemon:

```bash
uv run agentflow check
```

AgentFlow keeps runtime state in `~/.agentflow`; `AGENTFLOW_STATE` overrides that
location.

## Run

The daemon starts paused: it reconciles owned work but submits no cold work until
explicitly resumed.

```bash
uv run agentflow daemon
uv run agentflow console
uv run agentflow status
uv run agentflow resume
```

Use `agentflow pause` before maintenance. `agentflow daemon --once` runs one real
dispatch cycle and exits; it bypasses the pause flag.

The optional `AGENTFLOW_CAPACITY_HELPER` environment variable may point to a local
capacity-helper executable. Without it, AgentFlow starts safely but Codex capacity
and operator-activity detection are unavailable; Claude dispatch requires durable
provider quota facts. The daemon logs this limitation at startup.

See [`docs/coordinator-operations.md`](docs/coordinator-operations.md) for pause,
drain, upgrade, and rollback behavior.
