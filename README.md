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

## Optional notifications

Notifications are disabled by default. Set `AGENTFLOW_NTFY_URL` to an ntfy topic
URL to receive best-effort alerts when a run needs human attention:

```bash
export AGENTFLOW_NTFY_URL="https://ntfy.example.com/your-private-topic"
```

Treat an unprotected topic URL as sensitive configuration. Do not commit it.

## Calibrate capacity

AgentFlow includes a local capacity helper. It reads provider-authored facts from
the authenticated Claude and Codex session histories already on the machine; it
does not contact a separate service. Calibrate the Claude five-hour baseline once:

```bash
uv run agentflow capacity calibrate
```

Re-run calibration after the Claude plan changes materially. Codex reports typed
rate-limit windows in its session history and needs no calibration. Missing or
unreadable facts fail closed instead of permitting unattended work.

`AGENTFLOW_CAPACITY_HELPER` may point to a different compatible executable. The
bundled helper is the default.

## Run on macOS

The daemon starts paused: it reconciles owned work but submits no cold work until
explicitly resumed.

```bash
uv run agentflow service install
uv run agentflow status
uv run agentflow resume
uv run agentflow console
```

`service install` writes and loads a per-user LaunchAgent at
`~/Library/LaunchAgents/agentflow.daemon.plist`. It supervises only the persistent
daemon; the read-only console remains an on-demand operator command. Daemon output
goes to `~/Library/Logs/agentflow.log`.

The generated service records absolute `AGENTFLOW_CONFIG`, `AGENTFLOW_STATE`, and
`AGENTFLOW_CAPACITY_HELPER` paths plus the current `PATH`. Re-run `service install`
after changing any of them or after upgrading; it replaces and reloads the service
in place. `uv run agentflow service remove` stops the daemon and removes only the
generated LaunchAgent, preserving configuration, state, and logs.

Use `agentflow pause` before maintenance. `agentflow daemon` runs the same daemon
in the foreground for diagnosis. `agentflow daemon --once` runs one real dispatch
cycle and exits; it bypasses the pause flag.

See [`docs/coordinator-operations.md`](docs/coordinator-operations.md) for pause,
drain, upgrade, and rollback behavior.

## Project contract

AgentFlow is licensed under [Apache-2.0](LICENSE). Contributions use the same
inbound and outbound license terms and require
[Developer Certificate of Origin 1.1](DCO) sign-off; see
[CONTRIBUTING.md](CONTRIBUTING.md).

The beta supports only current `main` on macOS and makes no API or response-time
guarantees. The complete terms are:

- [Compatibility](COMPATIBILITY.md)
- [Governance](GOVERNANCE.md)
- [Public beta scope and publication contract](docs/public-beta.md)
- [Security](SECURITY.md)
- [Support](SUPPORT.md)
