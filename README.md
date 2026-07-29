# AgentFlow

[![CI](https://github.com/ConnorGriffin/agentflow/actions/workflows/ci.yml/badge.svg)](https://github.com/ConnorGriffin/agentflow/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](docs/public-beta.md)
[![Sponsor](https://img.shields.io/badge/sponsor-ConnorGriffin-ea4aaa?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/ConnorGriffin)

AgentFlow is an unattended GitHub issue → pull request engine. It grounds work,
builds it with Claude Code or Codex, reviews the exact pushed commit, and applies
the repository's merge policy.

> **macOS beta:** AgentFlow launches authenticated local coding agents, GitHub
> CLI, Git, and uv. Run it only on a machine where unattended coding sessions
> are acceptable.

## The mental model

AgentFlow does not decide what deserves to become work. It executes work that a
human has already approved as an ordinary GitHub issue.

For large or uncertain efforts, [Wayfinder](docs/adr/0027-wayfinder-planning-boundary.md)
is the upstream planning layer:

```text
foggy destination
      ↓
Wayfinder Decision Map
      ↓
research · prototypes · grilling · operator decisions
      ↓
clear, independently shippable subtree
      ↓
ordinary standalone Build Issue
      ↓
AgentFlow intake → build → review → gate
```

Wayfinder makes uncertainty explicit and earns the right to file work.
AgentFlow turns approved work into a safe, reviewed change.

GitHub and the repository remain the durable authority. The console is a
read-only projection; it is not another planner, tracker, or mutation surface.

## Issue intake and routing

The daemon selects an open issue only when it has no AgentFlow state or claim
and no `wayfinder:*` label. Planning artifacts cannot accidentally become
builds.

Intake reads the issue and repository, rewrites the request as a grounded Agent
Brief, and returns one structured route:

| Route | Meaning | Result |
| --- | --- | --- |
| `ready` | The outcome and constraints are clear | Add complexity and effort dials, then enter the build queue |
| `mockup` | A visual decision is missing | Hold for the human-selected `/ui-mockups` flow |
| `grill` | Missing intent would change the outcome | Hold until a maintainer resolves the fork |
| `nothing-new` | A resumed issue has no new actionable reply | Make no duplicate projection |

Malformed output, missing dials, and unreadable routes fail closed to a
human-visible hold. They never become accidental builds.

## How the engine works an issue

1. **Intake** grounds the issue and writes the Agent Brief.
2. **Dispatch** selects a provider with headroom and reserves durable capacity.
3. **Build** works in an isolated worktree, tests, pushes, and opens a PR.
4. **Review** sends the exact pushed head to an independent tool when required.
5. **Revise** addresses findings, feedback, and conflicts with bounded retries.
6. **Gate** checks CI, review proof, taint, collision safety, and repo policy.
7. **Recover** resumes from durable facts without duplicating work or claims.

PR-bound work drains before new issues. Live operator activity reduces
unattended dispatch without killing sessions already running.

Reviewers may ship clear fixes. When they do, the other tool must inspect the
new exact head. Repeated cross-tool disagreement parks for a human instead of
looping forever.

## Autonomy profiles

| Profile | Grounding and review | Merge authority |
| --- | --- | --- |
| `autonomous` | Agent grounding; mandatory cross-tool exact-head review | Auto-merge after green CI and a clean, untainted review |
| `reviewed` | Agent grounding; cross-tool review when available | Human glances, then merges |
| `guarded` | Mandatory real-data or running-app grounding; dual-tool or human review | Human merges after full review |

New repositories default to `reviewed`. A repository declares its profile with
`profile: autonomous`, `profile: reviewed`, or `profile: guarded` in its
`AGENTS.md` or `CLAUDE.md`.

There is one engine, not a different pipeline per profile. The profile controls
trust and merge authority. Declared capabilities control available tools and
required proof.

## Wayfinder and AgentFlow

Wayfinder owns destination-setting, dependency-ordered decisions, and the human
judgment required to turn a clear subtree into a Build Issue.

Decision tickets have one type:

- `wayfinder:research`: a bounded question that may run unattended.
- `wayfinder:prototype`: human-selected UI exploration.
- `wayfinder:grilling`: a human-in-the-loop domain decision.
- `wayfinder:task`: an operator prerequisite that unblocks a decision.

AgentFlow excludes every `wayfinder:*` ticket from normal intake. The one narrow
exception is open, unblocked, unclaimed `wayfinder:research`.

That exception executes research, not planning judgment. A human still decides
whether a recommendation becomes a Build Issue.

## How skills are invoked

“Installed” does not mean “automatically dispatched.” Skills enter the system in
three ways:

1. **Chat triggers.** A conversational agent selects a skill when the user names
   it or the request matches its `SKILL.md` description.
2. **Skill composition.** Wayfinder invokes tools such as `/grilling`,
   `/domain-modeling`, `/research`, and `/ui-mockups` at defined boundaries.
3. **Engine prompts.** AgentFlow supplies stage-specific contracts and only the
   checked capabilities enrolled in that repository.

The Python engine does not scan or dispatch every globally installed personal
skill. User-global skills and connectors are intentionally absent from
unattended sessions.

## Where the project is today

The issue-to-PR engine, structured intake, dual-provider dispatch, isolated
builds, exact-head review, bounded recovery, autonomy profiles, and read-only
console exist on `main`.

The main open boundary bug is research disposition. Today, non-empty findings
can close a research ticket even when they recommend new implementation work.

The target contract is:

- Every result becomes `handoff_required`, `no_build`, or concretely `deferred`.
- Handoff candidates remain visible until a human disposes every one.
- Selected candidates become standalone Build Issues with durable provenance.
- Research closes only after reconciliation.
- Crash replay creates no duplicate comments, labels, map lines, or issues.

The broader destination is one read-only fleet → repository → map console. It
should show frontiers, blockers, pipeline state, evidence, provider headroom,
required actions, and honest freshness.

That console remains behind its whole-surface prototype and implementation
slicing decisions: [#183](https://github.com/ConnorGriffin/agentflow/issues/183)
and [#184](https://github.com/ConnorGriffin/agentflow/issues/184).

## Quick start

### Requirements

- macOS
- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/)
- Git and an authenticated [GitHub CLI](https://cli.github.com/)
- At least one authenticated Claude Code or Codex installation
- For UI repository enrollment: Node.js 18 or newer, npm, and npx (Node.js 20
  or newer is recommended)

Both providers are required for automatic cross-tool review in the
`autonomous` profile. With one provider, AgentFlow can still build work;
`reviewed` repositories may use a fresh same-tool review and still require a
human merge.

### Install

```bash
git clone https://github.com/ConnorGriffin/agentflow.git
cd agentflow
uv sync --group dev
```

The built console is tracked in the clone. Headless repository enrollment needs
no Node.js, npm, or npx. UI enrollment requires them and installs pinned
Playwright and Chromium tooling. Console development also requires Node.js.

### Enroll a repository

Start with a clean checkout of the repository you want AgentFlow to manage.
Inspect its current readiness:

```bash
uv run agentflow doctor --repo /absolute/path/to/repository
```

The first check may report missing setup. Preview the files and capabilities
AgentFlow would add, then apply them:

```bash
uv run agentflow enroll /absolute/path/to/repository --profile reviewed
uv run agentflow enroll /absolute/path/to/repository --profile reviewed --apply
```

Review and commit the generated files in the target repository. Then verify the
repository and AgentFlow configuration:

```bash
uv run agentflow doctor --repo /absolute/path/to/repository
uv run agentflow check
```

Enrollment does not create GitHub queue labels or harden public-pull-request CI.
Complete the printed GitHub verification step before starting the daemon.

See [repository capabilities](docs/capabilities.md) for the generated contract,
skill manifest, UI tooling, and optional integrations.

### Calibrate provider capacity

AgentFlow reads provider-authored capacity facts from the authenticated Claude
and Codex session histories already on the machine. It does not contact a
separate service.

If you use Claude, calibrate its five-hour baseline once:

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

## Repository capabilities

Enrollment installs AgentFlow's operating skill inside each repository. UI
repositories also get pinned `ui-mockups` and `drive-local-webapp` skills, a
screenshot harness, and the required browser runtime.

The UI skills are also available independently from
[Connor Griffin's public skills repository](https://github.com/ConnorGriffin/skills):

```bash
npx skills add ConnorGriffin/skills \
  --skill ui-mockups \
  --skill drive-local-webapp
```

[Codebase Memory onboarding](https://github.com/ConnorGriffin/skills/tree/main/skills/cbm-onboard)
and [Matt Pocock's upstream skills](https://github.com/mattpocock/skills) are
optional.

Unattended stages receive AgentFlow's canonical charter, but not personal
global instructions or connectors. An optional local Codebase Memory server is
the only MCP configuration re-supplied, and its configured environment is not
forwarded.

## Recover on a new machine

AgentFlow's required setup is reproducible from public repositories and
checked-in project files:

1. Install Git, GitHub CLI, uv, and at least one supported coding agent; then
   authenticate them.
2. Clone AgentFlow and run `uv sync --group dev`.
3. Clone the project into a clean checkout.
4. Run enrollment as a preview, apply it, commit its generated files, then run
   `doctor` and `check`.
5. Install the AgentFlow service and resume it.

The capability manifest pins required skill and runtime versions. Recovery does
not depend on remembered user-global configuration.

## Foreground notifications

Notifications are disabled by default. `AGENTFLOW_NTFY_URL` applies only to a
foreground daemon launched from the same shell:

```bash
export AGENTFLOW_NTFY_URL="https://ntfy.example.com/your-private-topic"
uv run agentflow daemon
```

The installed LaunchAgent does not inherit `AGENTFLOW_NTFY_URL` from your shell.
`service install` writes selected runtime values to its plist, but does not copy
the topic URL. Notifications therefore remain disabled for the service. Do not
add the topic URL to the plist.

Treat an unprotected topic URL as sensitive configuration. Do not commit it.

## Operations and development

- [Coordinator operations](docs/coordinator-operations.md) covers pause, drain,
  upgrade, diagnosis, and rollback.
- [Repository capabilities](docs/capabilities.md) explains enrollment,
  generated skills, and optional integrations.
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
