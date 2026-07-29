# AgentFlow

[![CI](https://github.com/ConnorGriffin/agentflow/actions/workflows/ci.yml/badge.svg)](https://github.com/ConnorGriffin/agentflow/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](docs/public-beta.md)
[![Sponsor](https://img.shields.io/badge/sponsor-ConnorGriffin-ea4aaa?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/ConnorGriffin)

AgentFlow turns GitHub issues into agent-built, cross-reviewed pull requests
using Claude Code and Codex. Each repository chooses how far automation may go.

> **macOS beta:** AgentFlow launches authenticated local coding agents, GitHub
> CLI, Git, and uv. Run it only on a machine where unattended coding sessions
> are acceptable.

![AgentFlow console showing a Codex build in progress and provider capacity](mockups/evidence/issue-70-live-session.png)

## How it works

1. **Intake** turns an issue into a grounded Agent Brief.
2. **Build** gives Claude or Codex an isolated worktree and the brief.
3. **Review** sends the exact pull-request head to the other tool when required.
4. **Gate** merges the result or leaves a clear human action, based on the
   repository's autonomy profile.

The daemon coordinates the work. The read-only console shows provider capacity,
work in flight, repositories awaiting attention, recent merges, and trust-ratchet
state.

## Autonomy profiles

| Profile | Grounding and review | Merge authority |
| --- | --- | --- |
| `autonomous` | Agent grounding; mandatory cross-tool exact-head review | Auto-merge after green CI and a clean, untainted review |
| `reviewed` | Agent grounding; cross-tool review when available | Human glances, then merges |
| `guarded` | Mandatory real-data or running-app grounding; dual-tool or human review | Human merges after full review |

New repositories default to `reviewed`. A repository declares its profile with
`profile: autonomous`, `profile: reviewed`, or `profile: guarded` in its
`AGENTS.md` or `CLAUDE.md`.

## Quick start

### Requirements

- macOS
- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/)
- Git and an authenticated [GitHub CLI](https://cli.github.com/)
- An authenticated Claude Code or Codex installation

### Install

```bash
git clone https://github.com/ConnorGriffin/agentflow.git
cd agentflow
uv sync --group dev
```

The built console is tracked and included in the package. Node is needed only
when changing the console itself.

### Configure

Create `~/.config/agentflow/config.toml`:

```toml
[[repositories]]
repo = "owner/repository"
workdir = "/absolute/path/to/repository"

# Optional: expose this repository in the Project workspace.
workspace = true
```

Add one table per repository, then validate the configuration:

```bash
uv run agentflow check
```

Relative `workdir` values resolve from the configuration file.
`AGENTFLOW_CONFIG` overrides the configuration path. Runtime state lives in
`~/.agentflow`; `AGENTFLOW_STATE` overrides it.

### Calibrate provider capacity

AgentFlow reads provider-authored capacity facts from the authenticated Claude
and Codex session histories already on the machine. It does not contact a
separate service.

Calibrate the Claude five-hour baseline once:

```bash
uv run agentflow capacity calibrate
```

Re-run calibration after materially changing the Claude plan. Codex reports
typed rate-limit windows and needs no calibration. Missing or unreadable facts
fail closed.

### Start

```bash
uv run agentflow service install
uv run agentflow status
uv run agentflow resume
uv run agentflow console
```

The LaunchAgent starts paused and submits no new work until `agentflow resume`.
Use `agentflow pause` before maintenance.

`service install` writes `~/Library/LaunchAgents/agentflow.daemon.plist`.
Daemon output goes to `~/Library/Logs/agentflow.log`. Re-run the install command
after changing paths, environment, or the AgentFlow checkout.

## Optional notifications

Notifications are disabled by default. Set `AGENTFLOW_NTFY_URL` to an ntfy topic
URL for best-effort alerts when a run needs human attention:

```bash
export AGENTFLOW_NTFY_URL="https://ntfy.example.com/your-private-topic"
```

Treat an unprotected topic URL as sensitive configuration. Do not commit it.

## Operations and development

- [Coordinator operations](docs/coordinator-operations.md) covers pause, drain,
  upgrade, diagnosis, and rollback.
- [Contributing](CONTRIBUTING.md) covers development setup, console builds,
  tests, pull requests, and DCO sign-off.
- `uv run pytest -q` is the Python test gate.
- Console changes use `npm ci`, `npm test`, and `npm run build` from
  `agentflow/webui/`.

## Support AgentFlow

If AgentFlow is useful to you, you can
[sponsor its maintenance on GitHub](https://github.com/sponsors/ConnorGriffin).

Sponsorship supports the project. It does not buy support priority, feature
acceptance, or governance rights.

## Project policy

AgentFlow is an Apache-2.0, clone-only macOS beta. Only current `main` is
supported, with no API-stability, response-time, or paid-support guarantee.

- [License](LICENSE) and [NOTICE](NOTICE)
- [Compatibility](COMPATIBILITY.md)
- [Governance](GOVERNANCE.md)
- [Public beta contract](docs/public-beta.md)
- [Security](SECURITY.md)
- [Support](SUPPORT.md)
