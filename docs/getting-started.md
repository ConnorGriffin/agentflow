# Get started

This is the runnable setup path for the macOS beta. AgentFlow is a local
daemon: it needs GitHub CLI, Git, uv, and at least one authenticated coding
agent (Claude Code or Codex). Python 3.11+ is required. UI repositories also
need Node.js 18+, npm, and npx; Node.js 20+ is recommended.

## Install

```bash
git clone https://github.com/ConnorGriffin/agentflow.git
cd agentflow
uv sync --group dev
```

Authenticate GitHub CLI and at least one provider before enrolling a repository.
Both providers are needed for automatic cross-tool review in `autonomous`; a
`reviewed` repository can use one provider and still requires a human merge.

## Enroll a repository

Start with a clean checkout and inspect it first:

```bash
uv run agentflow doctor --repo /absolute/path/to/repository
uv run agentflow enroll /absolute/path/to/repository --profile reviewed
uv run agentflow enroll /absolute/path/to/repository --profile reviewed --apply
uv run agentflow doctor --repo /absolute/path/to/repository
uv run agentflow check
```

Review and commit the generated files in the target repository. Enrollment does
not create GitHub queue labels or harden public-pull-request CI; complete the
printed GitHub verification step before starting the daemon. See
[Repository capabilities](capabilities.md) for the generated contract.

## Calibrate capacity

Claude capacity is read from authenticated local session history. Calibrate its
five-hour baseline once, and repeat after materially changing the plan:

```bash
uv run agentflow capacity calibrate
```

Codex reports typed rate-limit windows and needs no calibration. Missing or
unreadable capacity facts fail closed.

## First run

Install the per-user services, inspect them while paused, then explicitly allow
cold submissions:

```bash
uv run agentflow service install
uv run agentflow status
uv run agentflow resume
uv run agentflow status
```

The service starts paused. Use `uv run agentflow pause` before maintenance.
Pause one provider without stopping reconciliation:

```bash
uv run agentflow pool pause claude
uv run agentflow pool status claude
uv run agentflow pool resume claude
```

The console is local and read-only at `http://127.0.0.1:8788`. Daemon and
console logs are under `~/Library/Logs/`. The installed LaunchAgent does not
inherit `AGENTFLOW_NTFY_URL` from a shell.

## New-machine recovery

Install and authenticate Git, GitHub CLI, uv, and a supported coding agent;
clone AgentFlow and run `uv sync --group dev`; clone the managed project; run
enrollment as preview, apply it, commit its generated files, then run `doctor`
and `check`; finally install the service and resume it. The checked-in capability
manifest pins required skills and runtime versions, so recovery does not depend
on remembered user-global configuration.
