# ADR 0035 — Agentflow is a workflow engine with a unified read-only operator console

- Status: Accepted
- Date: 2026-07-17
- Amended by: [ADR 0037](0037-daemon-dispatch-of-afk-research.md)
- Supersedes: [ADR 0033](0033-project-workspace-state-and-control-plane.md),
  [ADR 0034](0034-methodology-session-orchestration.md)
- Amends: [ADR 0010](0010-operator-dashboard.md),
  [ADR 0023](0023-dashboard-replatform-control-plane.md)

## Context

Agentflow began as the autonomous issue → PR → review → merge workflow and an
operator view over a multi-repository fleet. The Wayfinder experiment expanded
the web surface into a Project workspace with conversations, staged Proposals,
hash-bound approval, Publication reconciliation, and a separate visual language.

Dogfooding rejected the central premise. Interactive planning required latency
exceptions and chat-client behavior already provided better by Claude Code and
Codex. The later shelf-first amendment moved interactive work back to chat but
left a substantial durable workspace and approval system behind. That system is
a second source of truth and a second interaction boundary without a demonstrated
failure that justifies it.

The experiment did expose one valuable UI model: the locked workspace mockup's
Decision Map view makes the frontier, blocked work, settled decisions, handoffs,
ADRs, landed changes, and evidence legible together. The current fleet console,
however, is not a visual foundation the operator wants to preserve.

## Decision

### Agentflow's runtime boundary begins at the Build Issue

Planning, Wayfinder, grilling, research, domain modeling, and mockup iteration run
in the operator's chat tool. GitHub owns issues, pull requests, checks, reviews,
dependencies, and merges; the repository owns ADRs, specifications, and history.

After chat produces a standalone Build Issue and the operator explicitly confirms
it, the issue is filed directly in GitHub and normal intake begins. Agentflow owns
no durable pre-issue Project, Conversation, Proposal, approval, or Publication
lifecycle.

*Amended by [ADR 0037](0037-daemon-dispatch-of-afk-research.md):* "the operator's
chat tool" means an agent session, attended or unattended — what this ADR retired
is the bespoke web planning surface, not unattended cognition. The daemon may
dispatch unattended sessions to resolve AFK-able `wayfinder:research` tickets,
recording only to the GitHub ticket and map; build intake still begins at the
Build Issue, and no pre-issue workspace state returns.

### The operator console is read-only

The console performs no mutations. It deep-links actions to their authoritative
GitHub, chat, or CLI surface. The generic web command channel and workspace POST
endpoint are retired; GitHub issue management is not rebuilt in agentflow.

The console has two derived navigation levels:

- a **fleet home** for exceptions, live sessions, capacity/health, and bounded
  recent landed changes; and
- a **repository view** for active Decision Maps, their current frontier,
  workflow-relevant Build Issues, blockers, landed evidence, and contextual ADR
  links.

Repository views deliberately exclude the general backlog and arbitrary GitHub
history. GitHub remains the comprehensive browser.

### Decision Maps remain visible, but not editable

Map creation and resolution stay in chat and GitHub. The console derives map state
from GitHub's native child-issue and dependency relationships and links out for
action.

The map view in `mockups/workspace-combined.html` is preserved as an **information
model**, specifically:

- map status and progress;
- the current frontier;
- open and blocked tickets;
- decisions so far with ADR links;
- handed-off Build Issues and their pipeline state;
- landed evidence; and
- provenance/history.

Its shelf, chat composer, conversation affordances, proposal approval, synthetic
workspace data contract, and separate light/serif product identity are not
preserved.

### GitHub reads remain bounded daemon projections

The browser never queries GitHub. The daemon produces rate-budgeted, bounded,
disposable projections and the console serves the latest one with honest age.
Map data covers active maps; landed history remains bounded. There is no console
history database or fallback live query when the daemon is stale.

### The visual system is reopened

The existing terminal-native dashboard and separate workspace visual languages
are no longer binding constraints. The successor must be one coherent fleet →
repository → map experience, but palette, typography, density, layout, navigation,
tables, cards, and dark/light posture are open design questions.

The next UI must go through `/ui-mockups` as several genuinely different
whole-console concepts. The existing map viewer supplies information requirements,
not pixels. No implementation begins until a successor visual specification is
locked.

## What remains and what is retired

Retained foundations:

- daemon, coordinator, intake, build, review, merge, and recovery workflow;
- daemon-owned fleet/live projections and rate-budget discipline;
- FastAPI static/projection serving and the Svelte build unless implementation
  evidence later justifies changing the stack; and
- the proven semantics behind Inbox, Live, Fleet, History, and the map information
  model.

Retired vertically:

- Project/Conversation/Proposal workspace persistence and projections;
- the coordinated Conversation stage and interactive admission behavior;
- workspace command channel and `POST /api/command`;
- Proposal approval, Publication reconciliation, and workspace pipeline mirror;
- Workspace/Ask/approval UI, styles, synthetic capture, and superseded mockups; and
- ADR, glossary, product, tests, and open issues that exist only for that model.

## Alternatives considered

- **Keep the workspace as an artifact-only proposal shelf.** Rejected: it retains
  the second approval interaction and most of the reconciliation machinery after
  its chat purpose has disappeared.
- **Delete the entire web stack.** Rejected: cross-repository runtime, capacity,
  exception, and audit state has real value and no adequate single GitHub view.
- **Keep the existing console UI and add a map tab.** Rejected: the current visual
  system is itself under reconsideration; the map should help shape one successor,
  not be bolted onto a disliked shell.
- **Let each browser query GitHub.** Rejected by ADR 0026's observed quota failure.

## Consequences

- Agentflow becomes smaller: workflow engine plus derived operator instrument,
  not an agent client or issue manager.
- GitHub and git remain the only durable planning and execution authorities.
- Removing the workspace is a deliberate deletion project, not compatibility work;
  local workspace history is not migrated.
- The locked console/workspace mockups cease to bind successor implementation.
- A new Wayfinder map must audit stale issues and artifacts, establish the bounded
  map projection, lock the unified visual specification, and hand deletion and
  implementation off as independent Build Issues.
