# Competitive landscape: project-oriented agentic development environments

**Research date:** 2026-07-16

**Primary question:** How competitive is the full future vision in [agentflow issue #123](https://github.com/ConnorGriffin/agentflow/issues/123)?

**Secondary question:** What is the smallest existing-repository slice that can validate a differentiated entry wedge?
**Evidence policy:** Primary sources only: official product documentation, first-party product and release posts, and official repositories. Product claims are not treated as independent proof.

## Executive conclusion

The full vision is a real, already-contested product category. It is not merely “a better coding agent.” It is a project-oriented environment where a person can discover what to build, turn decisions into durable work, dispatch agents, review evidence, and operate the resulting software-development loop. Five competitors cover much of that north star end to end:

- **Replit Agent** is the closest greenfield-and-design-shaped competitor. Its Project Editor is a home for describing and managing a project; Plan mode supports brainstorming, architecture, roadmaps, ordered tasks, and an explicit `Start building` approval; background tasks can explore alternative implementations; and Design Canvas can compare mockup directions and apply one back to the app. Checkpoints, live previews, and publishing close the build loop. It is weaker where agentflow is strongest: GitHub/repository authority, durable engineering-decision artifacts, independent review, and fleet governance. [Project Editor](https://docs.replit.com/learn/projects-and-artifacts/project-editor), [Plan mode](https://docs.replit.com/references/agent/plan-mode), [task system](https://docs.replit.com/core-concepts/agent/task-system), [Design Canvas](https://docs.replit.com/learn/design/canvas)
- **GitHub Copilot app + Agent HQ + Agentic Workflows** is the closest platform-shaped competitor. Its technical-preview desktop app already has a cross-repository work view, issue-started sessions, quick chats, Plan mode, parallel worktrees, canvases, integrated terminal/browser validation, PR review, CI repair, and Agent Merge. GitHub remains the ledger by construction. Agent HQ adds third-party agents, and Agentic Workflows adds repository-native automation with declared permissions and safe outputs. The missing pieces are agentflow's temporary decision-map semantics, its explicit candidate-artifact promotion model, and its opinionated methodology/evidence chain—not the general workspace shape. [GitHub Copilot app docs](https://docs.github.com/en/copilot/concepts/agents/github-copilot-app), [issue-to-merge workflow](https://docs.github.com/en/copilot/how-tos/github-copilot-app/managing-issues-and-pull-requests), [Agent HQ](https://github.blog/news-insights/company-news/pick-your-agent-use-claude-and-codex-on-agent-hq/), [Agentic Workflows](https://docs.github.com/en/copilot/concepts/agents/about-github-agentic-workflows)
- **Linear Agent + Coding Sessions + Linear Diffs** is the closest work-management-shaped competitor. Linear combines persistent project/issue/milestone context, freeform agent chat, reusable skills, automations, cloud coding through Claude Code or Codex, PR diffs, review, iteration, and merge. It is stronger than agentflow's north star on project control and weaker on repository/GitHub authority: Linear becomes a second durable system rather than a view over the repo and GitHub. [Linear Agent](https://linear.app/docs/linear-agent), [Coding Sessions](https://linear.app/docs/coding-sessions), [Reviews/Diffs](https://linear.app/docs/diffs)
- **Factory Missions** is the strongest long-horizon execution analogue. A user scopes a goal conversationally, approves the plan, then an orchestrator decomposes it into milestones and fresh worker contexts with independent validation. Factory explicitly says Git is the source of truth. It is stronger on multi-day mission execution, weaker on issue/milestone control as a durable product workspace, temporary decision maps, and explicit promotion of conversational outputs into repo/GitHub artifacts. Missions is also still described by Factory as early. [Missions](https://factory.ai/news/missions), [architecture](https://factory.ai/news/missions-architecture)
- **Kiro** is the strongest spec-driven IDE analogue. It spans greenfield and brownfield work, conversational exploration, requirements/design/tasks artifacts, approval checkpoints, autonomous execution, PRs, hooks, steering, and scheduled automations. Its repo-native specs are close to promoted decision outputs; its missing center is the fleet/project control plane and GitHub-native issue/milestone/activity loop. Kiro Web and Agent Focus are explicitly preview/experimental surfaces. [Kiro docs](https://kiro.dev/docs/), [CLI Specs](https://kiro.dev/docs/cli/v3/specs/), [Kiro Web autonomous mode](https://kiro.dev/docs/web/autonomous-mode/), [Agent Focus](https://kiro.dev/blog/introducing-agent-focus/)

The strongest substitute may be a **composable stack rather than one product**:

- **OpenSpec + Shep** supplies `explore → proposed repo artifacts → apply → archive` plus optional approved specs, agent worktrees, CI auto-fix, PRs, and human merge.
- **GitHub Copilot app + Spec Kit + Agentic Workflows** supplies the workspace, durable specs/method, GitHub controls, execution, and recurring automation.
- **Linear Agent + an integrated coding session** supplies project context, Ask, issue/milestone controls, dispatch, review, and merge.
- **BMAD Method + an agent orchestrator** supplies the broad methodology suite and greenfield lifecycle over execution infrastructure such as Shep, Agent Orchestrator, or Agent Canvas.

This means **Ask, plan approval, candidate artifacts, worktrees, agent dispatch, and PR review are individually non-moats**. OpenSpec is especially damaging to a novelty claim around Ask and promotion: its brownfield-first, model-agnostic flow already separates no-stakes exploration from a proposed change folder, implementation, and archive into current specs. [OpenSpec](https://github.com/Fission-AI/OpenSpec)

What still appears genuinely differentiated is the _combination_ of:

1. a fleet home plus repository project workspace;
2. temporary decision maps that exist only when unresolved decisions can invalidate downstream work;
3. a broad, selectable engineering-methodology suite rather than one fixed spec workflow;
4. explicit promotion of complete candidate artifacts into repo/GitHub truth;
5. local, provider-agnostic execution with risk-based autonomy and independent cross-tool review; and
6. locked visual specifications with implementation evidence tied back to the originating decision or issue.

No verified competitor was found that combines all six. But none of the six is individually defensible. The only plausible moat is a trustworthy, coherent transition system across them, proven by repeated use on real repositories.

The recommended validation remains the resolved existing-repo wedge, but it must be evaluated as the first vertical slice of the full north star, not as the product definition: **project workspace → Ask → staged issue or small decision map → explicit approval → one dispatched ticket → independent build/review → evidence returned to the same workspace**. Do not build greenfield creation first. Do ensure the underlying artifact and project model can later support blank-project discovery, milestone formation, and tracer-bullet planning without replacement.

## 1. The user request, precisely interpreted

### Horizon 1 — full future vision / north star (primary)

The original seed asks whether agentflow should become a **real project-oriented agentic software-development environment**:

- One fleet home, with projects that belong to repositories.
- An existing-repository project is a durable workspace for exploring, deciding, planning, designing, dispatching, reviewing, and monitoring.
- A blank/greenfield project begins with a Wayfinder-style conversation about what to build, discovers milestones and the first end-to-end tracer bullet, then dispatches implementation.
- An active project can chart temporary decision maps for features, bugs, or other questions where unresolved choices can invalidate downstream work.
- A freeform Ask surface explores the codebase or a decision tree. The conversation may mature into a bug, enhancement, PRD, issue, milestone, map, ADR, glossary change, mockup, or another explicit artifact.
- The environment composes the existing engineering-methodology skill suite: research, diagnosis/grilling, decision mapping, domain modeling, ADRs and ubiquitous language, interface design, UI mockups, PRDs/issues, implementation, review, and related methods.
- Each project has controls for maps, milestones, issues, activity, changelog/history, dispatch, and human-required actions.
- UI work includes an integrated mockup-design path. A selected mockup becomes a locked visual specification; later implementation evidence is attached to it.
- The loop runs continuously from idea and decision through a shipped change. It is not only issue-to-PR automation.

The resolved foundation adds a strict authority model: conversations may persist, but their outputs become truth only through explicit promotion to GitHub or the repository; Ask should stage a complete candidate rather than ask the user to author one; approval to publish or dispatch is separate from the repository's execution-autonomy profile. [Issue #123](https://github.com/ConnorGriffin/agentflow/issues/123)

Greenfield creation, teams, deployment, and broad release management are **later/foggy parts of the original future**, not irrelevant ideas. They are excluded from the first slice to keep validation coherent. The competitive analysis therefore scores them where the seed makes them relevant—especially greenfield discovery—while avoiding assumptions about an eventual team or deployment product.

### Horizon 2 — existing-repository validation wedge (secondary)

The resolved first slice is deliberately narrower:

> existing repository → Ask → staged direct issue or small map → explicit approval → one dispatched build ticket → build/review → evidence in the same workspace

The wedge tests five risky assumptions: whether repository-grounded Ask is useful; whether it stages a better artifact than a chat answer; whether explicit promotion feels safe rather than bureaucratic; whether the existing agentflow pipeline can be invoked without duplicating it; and whether returning execution/review evidence to the originating workspace closes the loop. [Issue #123](https://github.com/ConnorGriffin/agentflow/issues/123)

## 2. Current agentflow baseline

Current agentflow is already a substantial execution substrate, not a blank slate:

- The product is a solo operator console over a fleet of repositories, optimized for exception handling and graduated autonomy. [PRODUCT.md](../../PRODUCT.md)
- The domain model includes per-repository autonomy profiles, decisive intake, one Agent Brief as build input, cross-tool review, human hold/re-entry, pool-aware scheduling, and a daemon-owned snapshot. [CONTEXT.md](../../CONTEXT.md), [ADR 0001](../adr/0001-per-repo-autonomy-profile.md), [ADR 0022](../adr/0022-one-build-input-and-the-build-verb.md)
- The persistent daemon dispatches ephemeral agent sessions in isolated worktrees, recovers interrupted stages, and treats GitHub outcomes as authoritative while local records retain transient stage ownership. [ADR 0011](../adr/0011-persistent-orchestrator.md), [ADR 0028](../adr/0028-stage-scoped-continuations.md), [daemon.py](../../agentflow/daemon.py)
- Intake grounds an issue, rewrites it into an Agent Brief, and routes it to ready, grilling, or mockup. Wayfinder artifacts are explicitly upstream and excluded from intake until they produce ordinary build issues. [ADR 0016](../adr/0016-intake-stage.md), [ADR 0027](../adr/0027-wayfinder-planning-boundary.md), [intake.py](../../agentflow/intake.py)
- UI work already has a methodology gate: mockups precede implementation, and UI PRs must carry screenshot evidence against the locked visual spec. [ADR 0018](../adr/0018-two-dials-review-by-evidence.md), [standards/CHARTER.md](../../standards/CHARTER.md)
- The Svelte console has Inbox, Live, Fleet, and History views, but the FastAPI surface currently serves only the daemon-produced read-only snapshot. The controls envisioned in ADR 0023 are not present in the current server. [ADR 0023](../adr/0023-dashboard-replatform-control-plane.md), [ADR 0026](../adr/0026-daemon-owned-snapshot.md), [webapp.py](../../agentflow/webapp.py), [App.svelte](../../agentflow/webui/src/App.svelte)

The gap to the north star is therefore mostly **upstream and lateral**: no per-repository project workspace, no general Ask, no integrated decision-map editor, no candidate-artifact store and promotion gate, no integrated methodology-session router, no milestone/map/issue controls in the console, and no mockup designer inside the project workspace. The gap is not “make an agent write a PR”; agentflow already does the dispatch/review/merge half.

## 3. Comparison model

Scores are **analyst inference**, not vendor claims and not product-quality scores. They measure functional overlap with this specific vision. A high score can still be strategically unattractive because of lock-in, authority conflicts, preview maturity, or an unsuitable audience.

Scoring scale:

- **0 — absent:** no verified capability.
- **1 — adjacent:** possible through generic chat, manual work, or an external integration.
- **2 — substantial:** verified first-class support for much of the dimension, with an important boundary missing.
- **3 — strong:** verified, coherent support close to the target behavior.

### North-star dimensions (27 points)

| Code | Dimension | What earns a high score |
|---|---|---|
| **P** | Project/fleet workspace | Cross-repository home plus durable repository/project workspaces and resumable context. |
| **G** | Greenfield discovery | Blank-project conversation, product shaping, milestones, and a first tracer bullet—not merely generating an app from one prompt. |
| **A** | Ask and decision exploration | Codebase/project Q&A, exploratory branches, and a path from conversation toward concrete work. |
| **R** | Candidate artifacts and promotion | Complete staged artifacts, visible review, explicit promotion/approval, and separation from execution autonomy. |
| **M** | Methodology breadth | Reusable research, planning, domain, architecture, design, issue, implementation, and review methods—not one fixed plan template. |
| **V** | Visual design and evidence | Mockup generation/iteration, locked visual specs, previews, and implementation evidence tied to intent. |
| **C** | Project controls | Maps/specs, milestones, issues, activity, changelog/history, dispatch, and needs-human controls. |
| **E** | Execution and assurance | Isolated execution, parallelism, continuity, CI/review/fix loops, and merge policy. |
| **T** | Durable truth and escape hatch | Repository/Git/GitHub authority, portable artifacts, and no silent promotion of chat into truth. |

### Existing-repo wedge dimensions (15 points)

| Code | Dimension | Target behavior |
|---|---|---|
| **WA** | Ask | Repository-grounded conversation. |
| **WS** | Stage | A complete candidate issue or small decision structure appears before execution. |
| **WG** | Gate | Explicit publish/dispatch approval distinct from later execution autonomy. |
| **WL** | Closed loop | Dispatch, build, review, and evidence return to the same workspace. |
| **WT** | Truth | GitHub/repository remain authoritative and usable without the product. |

## 4. Comparative scoring

### Full north star (primary)

| Comparable | P | G | A | R | M | V | C | E | T | **/27** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Proposed agentflow north star** | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | **27** |
| GitHub Copilot app / Agent HQ / Agentic Workflows | 3 | 2 | 3 | 2 | 2 | 2 | 3 | 3 | 3 | **23** |
| Kiro | 2 | 3 | 3 | 3 | 3 | 1 | 2 | 3 | 3 | **23** |
| Linear Agent / Coding Sessions / Diffs | 3 | 2 | 3 | 3 | 3 | 1 | 3 | 3 | 1 | **22** |
| Factory Droid / Missions | 2 | 3 | 2 | 3 | 3 | 2 | 2 | 3 | 2 | **22** |
| Replit Agent / Project Editor / Design Canvas | 3 | 3 | 3 | 2 | 1 | 3 | 2 | 2 | 1 | **20** |
| OpenAI Codex | 2 | 2 | 3 | 1 | 3 | 2 | 1 | 3 | 2 | **19** |
| Devin | 2 | 2 | 3 | 2 | 2 | 1 | 2 | 3 | 2 | **19** |
| Cursor | 2 | 2 | 3 | 1 | 2 | 2 | 2 | 3 | 2 | **19** |
| OpenSpec | 1 | 3 | 3 | 3 | 2 | 0 | 2 | 2 | 3 | **19** |
| GitHub Spec Kit | 1 | 3 | 2 | 2 | 3 | 0 | 2 | 2 | 3 | **18** |
| BMAD Method | 1 | 3 | 2 | 2 | 3 | 1 | 2 | 2 | 2 | **18** |
| Shep | 2 | 1 | 1 | 3 | 1 | 1 | 2 | 3 | 3 | **17** |
| OpenHands Agent Canvas | 3 | 1 | 2 | 1 | 2 | 1 | 2 | 2 | 2 | **16** |
| AgentWrapper Agent Orchestrator | 3 | 0 | 1 | 1 | 1 | 2 | 2 | 3 | 3 | **16** |
| **Current agentflow** | 1 | 0 | 0 | 1 | 1 | 2 | 2 | 3 | 3 | **13** |

The current-to-proposed delta is visible: agentflow already scores strongly on execution, evidence discipline, and durable authority. Most competitors start from chat/spec/project management and add execution. Agentflow starts from trustworthy execution and must add the project/decision environment.

### Existing-repository wedge (secondary)

| Comparable | WA | WS | WG | WL | WT | **/15** |
|---|---:|---:|---:|---:|---:|---:|
| **Proposed validation slice** | 3 | 3 | 3 | 3 | 3 | **15** |
| GitHub Copilot app | 3 | 2 | 3 | 3 | 3 | **14** |
| Kiro | 3 | 2 | 3 | 3 | 3 | **14** |
| Shep | 1 | 3 | 3 | 3 | 3 | **13** |
| OpenSpec | 3 | 3 | 3 | 1 | 3 | **13** |
| Linear | 3 | 3 | 2 | 3 | 1 | **12** |
| Factory Missions | 2 | 2 | 3 | 3 | 2 | **12** |
| Replit Agent | 3 | 2 | 3 | 3 | 1 | **12** |
| Devin | 3 | 2 | 2 | 3 | 2 | **12** |
| OpenAI Codex | 3 | 1 | 2 | 3 | 2 | **11** |
| Cursor | 3 | 1 | 1 | 3 | 2 | **10** |
| GitHub Spec Kit | 2 | 2 | 2 | 1 | 3 | **10** |
| AgentWrapper Agent Orchestrator | 1 | 1 | 1 | 3 | 3 | **9** |
| OpenHands Agent Canvas | 2 | 1 | 1 | 2 | 2 | **8** |
| **Current agentflow** | 0 | 1 | 0 | 2 | 3 | **6** |

The wedge is not uncontested either. Its validation burden is not “can this workflow exist?”—GitHub, Kiro, Shep, and an OpenSpec-plus-runner stack show that it can. The burden is whether agentflow's version is materially better for this operator because candidate promotion, method choice, risk-aware dispatch, independent review, and evidence are one trustworthy flow.

## 5. Market map

| Category | Strongest examples | What they compete for |
|---|---|---|
| **Greenfield project and visual app environments** | Replit Agent, Lovable, v0, Bolt | Blank-project discovery, visual iteration, implementation, preview, and deployment. Replit reaches furthest into structured planning and project continuity. |
| **Project/work platforms becoming development environments** | GitHub Copilot app/Agent HQ, Linear Agent | The durable home for issues, context, agent sessions, reviews, and human action. |
| **Long-horizon software factories** | Factory Missions, Devin, Kiro Web autonomous mode | Goal/plan approval, decomposition, remote execution, validation, and PR delivery. |
| **Agent command centers / IDEs** | Codex app, Cursor Agents Window, OpenHands Agent Canvas, AgentWrapper AO | Multi-repository session management, worktree isolation, live steering, previews, and agent portability. |
| **Local open-source execution orchestrators** | Shep, AO, Agent Canvas; emerging Shepherd, Spec Kitty, Agent Kanban, Tekton | Agent-agnostic dispatch, local custody, CI/review loops, dashboards, and human merge gates. |
| **Spec and methodology layers** | OpenSpec, GitHub Spec Kit, BMAD Method, Taskmaster | Exploration, requirements, architecture, task decomposition, approval, and repo-native durable artifacts. |
| **Coding-agent substrates** | Claude Code/Agent SDK, Cline, Aider, SWE-agent | The executor that a higher-level environment can embed or orchestrate. |
| **Review/quality layers** | Greptile, Qodo, Ellipsis | Independent validation and PR feedback; substitutes for only agentflow's assurance half. |

## 6. Detailed competitor profiles

### 6.1 Replit Agent — closest to the greenfield and mockup half

**Verified shape.** Replit's Project Editor is a persistent home for a project and its artifacts. Plan mode is read-only brainstorming and architecture work that can produce an ordered roadmap with dependencies; implementation starts only after the user selects `Start building`. The task system can run background work and explicitly recommends separate tasks for comparing design directions. Design Canvas is an infinite visual board for generating and comparing mockups, and a selected direction can be applied back to the app. Agent creates checkpoints while building, supports rollback, previews the running result, and can publish it. [Project Editor](https://docs.replit.com/learn/projects-and-artifacts/project-editor), [Plan vs Build](https://docs.replit.com/learn/plan-vs-build-mode), [tasks](https://docs.replit.com/core-concepts/agent/task-system), [Canvas](https://docs.replit.com/learn/design/canvas), [checkpoints](https://docs.replit.com/references/version-control/checkpoints-and-rollbacks)

**Against the full seed.** Replit already demonstrates the proposed blank-project experience: describe an idea, explore it, form a roadmap, approve work, compare visual directions, build, inspect, and publish without leaving the project. That is a more direct north-star competitor than an issue-to-PR agent. What it does not establish is agentflow's engineering authority model. Replit's project state and checkpoints are primary; GitHub issues, independently reviewed PRs, temporary decision maps, ADR/glossary promotion, and a fleet-wide risk dial are not the center.

**Implication.** Do not position blank-project chat, plan approval, mockup comparison, or “one project home” as novel. Agentflow must win on the rigor and portability of the decisions behind the resulting software, then connect that rigor to its existing execution pipeline.

### 6.2 GitHub Copilot app, Agent HQ, and Agentic Workflows — closest repo-native platform

**Verified shape.** The Copilot app can start a session from an issue in Plan mode, wait for approval, create a branch, implement, open a PR, show checks and review activity, ask an agent to fix review comments or CI, and use Agent Merge to keep working in the background until GitHub permits the merge. GitHub's Agents surface can also run concurrent research and coding sessions. Agentic Workflows are Markdown-defined GitHub Actions automations with explicit triggers, permissions, safe outputs, and support for Copilot, Claude, Codex, and Gemini engines. [Issue and PR workflow](https://docs.github.com/en/copilot/how-tos/github-copilot-app/managing-issues-and-pull-requests), [Copilot agents](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/overview), [Agentic Workflows](https://docs.github.com/en/copilot/concepts/agents/about-github-agentic-workflows)

**Maturity.** The Copilot app is technical preview; Agentic Workflows are public preview. Fleet mode and SDK sub-agent orchestration are documented as experimental surfaces, so they are evidence of direction rather than settled end-user contracts. [Copilot app status](https://docs.github.com/en/copilot/how-tos/github-copilot-app/managing-issues-and-pull-requests), [Fleet mode](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/fleet-mode)

**Against agentflow.** GitHub owns the strongest escape hatch because it is the ledger. It can copy the workspace and execution shape more easily than agentflow can copy GitHub's native integration. Agentflow's remaining advantage is policy: map-vs-issue semantics, methodology routing, cross-provider independent review, per-repo autonomy, and evidence tied back to the original decision. GitHub is the largest strategic threat because its platform can absorb those ideas incrementally.

### 6.3 Linear Agent, Coding Sessions, and Diffs — closest project-management environment

**Verified shape.** Linear Agent converses against teams, initiatives, projects, milestones, cycles, issues, relationships, documents, comments, and history. Its chats persist; it can create and update work artifacts; skills capture reusable methods; MCP adds external context; and automations act on incoming work. Coding Sessions use Claude Code and Codex to implement work in the cloud. Linear Diffs brings review, iteration, and shipping into the issue surface while syncing review state to GitHub. [Linear Agent](https://linear.app/docs/linear-agent), [Coding Sessions announcement](https://linear.app/changelog/2026-06-11-coding-sessions-in-linear), [Linear Diffs](https://linear.app/changelog/2026-05-28-linear-diffs), [agents in Linear](https://linear.app/docs/agents-in-linear)

**Against the full seed.** Linear is already the envisioned project control panel: Ask, project context, milestones, issues, activity, coding sessions, diffs, and shipping. It is weaker on greenfield product shaping, integrated visual mockups, local provider operation, and independent cross-tool assurance. The deeper conflict is authority: adopting Linear makes Linear a durable source alongside GitHub and the repository, while issue #123 explicitly wants conversations to promote into GitHub/repo truth.

**Implication.** Agentflow should copy Linear's contextual project navigation and chat-to-artifact ergonomics, not its second-ledger data model. Every resolved output should remain exportable and legible without the workspace.

### 6.4 Factory Missions — closest long-horizon software factory

**Verified shape.** Factory Missions begins with a conversational planning phase, asks for approval, decomposes work into milestones and features, uses fresh worker contexts, validates each milestone, repairs failures, and can use different model families for planning, implementation, validation, and research. Factory says Missions can run for days, supports computer-use validation of user interfaces, carries skills/hooks/custom droids into the mission, and treats Git as source of truth. [Missions](https://factory.ai/news/missions), [Missions architecture](https://factory.ai/news/missions-architecture), [Specification Mode](https://docs.factory.ai/cli/user-guides/specification-mode), [automated review](https://docs.factory.ai/guides/droid-exec/code-review)

**Against the full seed.** Factory is ahead on decomposition, multi-day recovery, multi-model role assignment, and application-level validation. It covers greenfield work better than the validation wedge suggests. It is weaker on an ongoing project workspace whose durable control objects are issues/maps/milestones, and its planning artifacts and orchestration are largely proprietary. The user cannot inspect its transition guarantees the way agentflow's ADRs, state records, and tests can be inspected.

**Implication.** “Different models for different roles,” milestone validation, and recovery are not unique. Agentflow's claim must be narrower: a transparent, GitHub-native, risk-aware loop whose promotion and completion rules are inspectable.

### 6.5 Kiro — closest structured-spec environment

**Verified shape.** Kiro's IDE, CLI, and web surfaces combine agent chat with specs, steering, hooks, and MCP. Specs create requirements, design, and task artifacts before implementation; Kiro Web adds isolated cloud sessions connected to GitHub or GitLab; and autonomous/spec/default modes can coexist. [Kiro overview](https://kiro.dev/docs/), [Specs](https://kiro.dev/docs/specs/), [Kiro Web](https://kiro.dev/docs/web/)

**Against the full seed.** Kiro is stronger than agentflow today on greenfield-to-implementation continuity and structured repo-native planning. It is weaker on fleet operations, issue/milestone control, temporary decision graphs, mockup exploration, and independent provider review. Its fixed spec grammar also differs from issue #123's method threshold: agentflow should use a map only when multiple slices or unresolved decisions justify one, not force every change through the same document chain.

### 6.6 Devin — close on Ask-to-Agent transition and managed sessions

**Verified shape.** Devin's first-run flow has separate Ask and Agent modes; Ask explores and constructs a scoped Devin prompt before execution. Managed Devins can decompose work into isolated parallel sessions, propose those sessions for approval, track them, and compile results. Devin also persists knowledge, playbooks, schedules, session events, and prior-session references. [First session](https://docs.devin.ai/get-started/first-run), [advanced capabilities](https://docs.devin.ai/work-with-devin/advanced-capabilities), [introduction and stated limits](https://docs.devin.ai/get-started/devin-intro)

**Against agentflow.** Devin validates Ask→plan→execution and persistent agent knowledge, but its promotion boundary is a prompt/session transition, not a typed candidate artifact promoted into GitHub or repo truth. It is a proprietary executor and workspace, not a transparent fleet policy layer. Its own docs still recommend small, isolated tasks for reliable execution.

### 6.7 OpenAI Codex and Claude Code — powerful substrates converging upward

**Codex.** Codex projects group tasks around local folders and shared project context; tasks retain transcripts; worktrees isolate concurrent code changes; `/plan` and `/goal` cover shaping and long-running outcomes; skills/plugins encode reusable methods; and subagents, cloud work, review, and automations cover execution. [Projects and tasks](https://learn.chatgpt.com/docs/projects), [long-running work](https://learn.chatgpt.com/docs/long-running-work), [skills and plugins](https://learn.chatgpt.com/docs/skills-and-plugins)

**Claude Code.** Claude Code on the web runs persistent GitHub-backed sessions with Plan approval, parallel branches, diff review, and PR creation. `/batch` decomposes approved work into isolated worktrees and PRs; routines trigger cloud work from schedules, APIs, or GitHub events; and its review service uses multiple specialized agents. [Web sessions](https://code.claude.com/docs/en/web-quickstart), [commands](https://code.claude.com/docs/en/commands), [routines](https://code.claude.com/docs/en/web-scheduled-tasks), [code review](https://code.claude.com/docs/en/code-review)

**Against agentflow.** Both can host the methodology conversations imagined by the seed, and both are rapidly adding project, background, and review surfaces. Neither currently supplies agentflow's durable GitHub issue/map state machine or risk-based cross-provider merge policy. They are better treated as replaceable runners and skill hosts than products agentflow should imitate feature-for-feature.

### 6.8 OpenSpec, Spec Kit, and BMAD — the methodology competition

**OpenSpec** is the closest candidate-artifact model. `/opsx:explore` is explicitly no-stakes; `/opsx:propose` writes proposal/spec/design/task artifacts; `/opsx:apply` implements; and `/opsx:archive` updates current specs. It is brownfield-first, fluid rather than phase-locked, model-agnostic, and supports many agent tools. Its Stores feature moves shared planning into a dedicated repository but is beta. [OpenSpec repository and workflow](https://github.com/Fission-AI/OpenSpec)

**GitHub Spec Kit** formalizes constitution → specify → plan → tasks → implement, with clarify/analyze/checklist steps, extensions, presets, workflows, and a command that converts tasks to GitHub issues. It supports 30+ coding-agent integrations. [Spec Kit](https://github.com/github/spec-kit), [workflow overview](https://github.github.com/spec-kit/)

**BMAD Method** supplies the broadest role/method catalog: analysis and research, PRD planning, architecture and UX, epics/stories, implementation, review, and retrospectives across multiple planning tracks. Its own tutorial requires separate fresh chats for workflows and stores artifacts in `_bmad-output`, exposing the orchestration/context seam that issue #127 intends to solve. [BMAD getting started](https://github.com/bmad-code-org/BMAD-METHOD/blob/main/docs/tutorials/getting-started.md)

**Against agentflow.** These projects prove that method libraries and repo artifacts are popular and portable. Agentflow's advantage is not having skills; it is selecting the smallest appropriate method, carrying context across methods without one giant prompt, promoting only resolved artifacts, and feeding them into a durable execution/review loop.

### 6.9 Shep — closest small open-source end-to-end execution product

**Verified shape.** Shep advertises an idea → optional approved spec/research/plan YAML → isolated agent worktrees → CI auto-fix → draft PR → review/merge loop. It is local-first, stores state in SQLite, accepts terminal agents, and keeps GitHub PRs as the delivery boundary. [Shep product and workflow](https://shep.bot/), [source](https://github.com/shep-ai/shep)

**Against agentflow.** Shep overlaps heavily with both current agentflow and the first slice. It proves that local, agent-agnostic execution, worktrees, spec gates, a web dashboard, and subscription reuse are not differentiators. Agentflow remains deeper on intake grounding, risk profiles, cross-tool review, human re-entry, evidence policy, and GitHub issue dependencies; the proposed north star is broader on Ask, maps, methodology, and project memory.

### 6.10 AgentWrapper Agent Orchestrator — strongest open-source command center

**Verified shape.** Agent Orchestrator manages projects and parallel sessions for 23 terminal agent harnesses, each in an isolated worktree. Its daemon observes runtime and GitHub facts, persists session/PR/check/comment state in SQLite, derives display state, routes CI failures/review comments/merge conflicts back to workers, and exposes terminal, review, and browser-preview surfaces. [README](https://github.com/AgentWrapper/agent-orchestrator), [architecture](https://github.com/AgentWrapper/agent-orchestrator/blob/main/docs/architecture.md)

**Against agentflow.** AO is ahead on adapter breadth, terminal supervision, and generic agent-IDE ergonomics. It is not the upstream decision environment in the seed, and its durable facts describe sessions and PR feedback rather than agentflow's typed stage outcomes, claims, autonomy, and evidence gates. It is both competitor and architectural prior art; agentflow should not spend its wedge recreating generic terminal/session management.

### 6.11 OpenHands Agent Canvas — strongest open multi-backend control plane

**Verified shape.** Agent Canvas calls itself a self-hosted developer control center for coding agents and automations. It can run OpenHands, Claude Code, Codex, Gemini, or ACP-compatible agents across local, remote, Docker, VM, cloud, or enterprise backends; connect multiple agent servers; and create scheduled/webhook automations with GitHub, Linear, Slack, and other tools. [Agent Canvas](https://github.com/OpenHands/agent-canvas), [OpenHands platform](https://github.com/OpenHands/OpenHands)

**Against agentflow.** Agent Canvas is broader infrastructure but thinner product methodology. It competes directly with the fleet home and provider abstraction, while leaving issue→review policy, decision maps, artifact promotion, and evidence semantics to the user. Its ACP support is worth watching as a possible standard adapter boundary; agentflow should not assume custom Claude/Codex runners remain the best interoperability seam forever.

## 7. Wider field: adjacent products and partial substitutes

| Product/project | Verified overlap | Why it is not a full direct match |
|---|---|---|
| **Lovable** | Plan mode for deciding; Agent mode for implementation/verification; visual edits; project history; GitHub sync. [Docs](https://docs.lovable.dev/features/agent-mode), [GitHub integration](https://docs.lovable.dev/integrations/github) | Strong prompt-to-app product, but the platform is primary and existing-code import remains constrained; no fleet, issue map, or independent review policy. |
| **v0** | Project chats, generated full-stack code/UI, GitHub/Vercel integration, deployment. [Docs](https://v0.app/docs/projects) | Visual app creation rather than a general repository project/methodology/assurance environment. |
| **Bolt** | Browser-based prompt, run, edit, and deploy for full-stack apps; open repository. [Source](https://github.com/stackblitz/bolt.new) | Greenfield app builder, not durable multi-repo engineering control plane. |
| **Cursor** | Ask/Agent modes, structured plans and dependent todos, remote background agents, web/mobile handoff, GitHub branches/PRs, Bugbot. [Planning](https://docs.cursor.com/en/agent/planning), [background agents](https://docs.cursor.com/background-agent), [web/mobile](https://docs.cursor.com/en/background-agent/web-and-mobile) | Strong executor and session workspace, but planning artifacts, project truth, and review are split across product surfaces. |
| **Cline / Roo Code** | Plan→Act separation, task history/checkpoints, and Roo Orchestrator's specialized subtasks. [Cline](https://docs.cline.bot/core-workflows/plan-and-act), [Roo](https://roocodeinc.github.io/Roo-Code/features/boomerang-tasks/) | IDE-local agent methods rather than fleet/project ledger or end-to-end governance. |
| **Taskmaster** | PRD parsing, generated dependent tasks, status tracking, and many agent integrations. [Source](https://github.com/eyaltoledano/claude-task-master) | Task decomposition layer; no execution assurance or project workspace. License includes Commons Clause restrictions. |
| **Tekton** | Self-hosted background agents, dashboard, isolated Nix containers, PR previews, subtask spawning; roadmap names durable history, plan approval, policy, multi-model routing, and CI/review loops. [Source](https://github.com/lambdaclass/tekton) | Much of the closest overlap is explicitly roadmap rather than shipped behavior; current scope is execution infrastructure. |
| **Quester / Shepherd** | Self-hosted worktree fleets, plan/review/QA roles, approval gates, dashboards, and issue/PR integration. [Quester](https://quester.dev/), [Shepherd](https://www.shepherd.run/) | Early/private or pre-release products; evidence is primarily first-party claims rather than inspectable adoption. |
| **Aider / SWE-agent** | Model-agnostic code editing or issue-resolution harnesses with Git workflows. [Aider](https://github.com/Aider-AI/aider), [SWE-agent](https://github.com/SWE-agent/SWE-agent) | Execution substrates, not the project/decision environment. |
| **Greptile / Qodo / Ellipsis** | Automated PR review, repository context, comments, and repair suggestions. [Greptile](https://docs.greptile.com/), [Qodo](https://docs.qodo.ai/qodo-documentation/qodo-merge), [Ellipsis](https://docs.ellipsis.dev/) | Compete with the assurance stage only; none supplies the full idea→artifact→dispatch loop. |

## 8. Open-source traction snapshot

Observed directly on GitHub on 2026-07-16. Stars and forks are adoption signals, not quality, reliability, or product-fit scores.

| Repository | Stars | Forks | License / recent signal | Relevance |
|---|---:|---:|---|---|
| [github/spec-kit](https://github.com/github/spec-kit) | 122k | 10.8k | MIT; 1,431 commits observed | Large demand for portable, agent-neutral structured development. |
| [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | 80.9k | 10.3k | Core MIT; enterprise directory separately licensed | Mature open agent substrate and local/cloud GUI ecosystem. |
| [Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec) | 61.1k | 4.2k | MIT; v1.6.0 released 2026-07-10 | Strong evidence for lightweight explore/propose/apply/archive semantics. |
| [bmad-code-org/BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD) | 50.7k | 5.8k | MIT; v6.10.0 released 2026-07-03 | Strong demand for broad, role-based methodology workflows. |
| [eyaltoledano/claude-task-master](https://github.com/eyaltoledano/claude-task-master) | 27.9k | 2.6k | MIT + Commons Clause; latest observed release 2026-03-31 | Strong demand for PRD→dependent-task orchestration. |
| [AgentWrapper/agent-orchestrator](https://github.com/AgentWrapper/agent-orchestrator) | 8.3k | 1.2k | Apache-2.0 | Closest established open-source multi-agent command center. |
| [shep-ai/shep](https://github.com/shep-ai/shep) | 231 | 51 | MIT; v1.218.1 released 2026-07-11 | Very close workflow overlap but still small/emerging. |
| [OpenHands/agent-canvas](https://github.com/OpenHands/agent-canvas) | 192 | 83 | MIT; v1.4.0 released 2026-07-14 | Very new control plane with fast release activity and broad adapter ambition. |
| [lambdaclass/tekton](https://github.com/lambdaclass/tekton) | 24 | 0 | 196 commits observed; roadmap-heavy | Useful convergent design evidence, not established competition yet. |

## 9. Strategic interpretation

### The category is converging from four directions

1. **Code hosts and trackers are adding agents.** GitHub and Linear already own durable work objects, so adding chat, plans, coding sessions, and review lets them approach the north star without inventing a second control plane.
2. **Coding agents are adding projects and orchestration.** Codex, Claude Code, Cursor, Devin, Factory, and Kiro are accumulating project memory, parallel sessions, plans, skills, automations, and review.
3. **App builders are adding real planning and design.** Replit now covers plan approval, background tasks, visual mockup comparison, checkpoints, and publishing—not only one-shot generation.
4. **Open-source layers are becoming composable.** OpenSpec/Spec Kit/BMAD provide methods and artifacts; Shep/AO/Agent Canvas provide execution and supervision. A user can assemble most of the proposed experience without buying one monolith.

The competitive risk is therefore not only “which company has the same product?” It is **how cheaply can the user assemble an adequate substitute from products they already have?** For Connor, the real benchmark is likely an OpenSpec-or-skills planning flow feeding GitHub plus an existing coding-agent control surface.

### What is commodity already

- A repo-scoped chat or Ask mode.
- Read-only Plan followed by approved execution.
- Generated requirements, designs, plans, and task lists.
- Git worktree isolation and parallel sessions.
- Background/cloud agents, resumable conversations, and mobile/web monitoring.
- CI auto-fix and PR-comment feedback loops.
- Generic skills, subagents, plugins, MCP, and model selection.
- A dashboard of running/waiting/failed agent sessions.
- Basic previews, checkpoints, and visual editing.

Building these well is still necessary; treating them as differentiation is wrong.

### What may be a defensible wedge

**1. Method selection rather than one methodology.** Issue #123 does not prescribe a spec ceremony for every change. A crisp bug should become an issue; a consequential unknown should invoke research or grilling; a tangled domain should update the glossary/ADR; a UI change should explore mockups; only a dependency-bearing uncertainty should become a map. Competitors generally provide one plan/spec flow or a bag of manually invoked skills. A reliable router that chooses the smallest method and preserves context between methods is more distinctive.

**2. Promotion as a typed boundary.** Conversation output should be a complete, inspectable candidate with `edit / approve / discard`; nothing becomes GitHub/repo truth or dispatchable work before approval. Execution autonomy starts only after promotion. OpenSpec has strong proposal/archive semantics, but agentflow can apply the same discipline across heterogeneous artifacts—issue, map, milestone, ADR, glossary change, mockup, PRD—while keeping the external ledger authoritative.

**3. Decision-to-evidence lineage.** A map node or candidate issue owns its selected mockup; the implementation returns exact PR/SHA, CI, independent review, and screenshot evidence to that same lineage. Replit has visual design, Factory has UI validation, and agentflow already has screenshot policy, but no verified competitor ties the entire chain together with the same authority semantics.

**4. Risk-aware, inspectable autonomy.** Agentflow's per-repo autonomy profile, fail-closed holds, cross-tool review, bounded continuation, claim ownership, and outcome-first completion are more than an “agent mode” toggle. The system can explain why a change advanced, parked, or merged. That matters more than raw agent breadth for the intended operator.

**5. One operator's fleet economics.** Balancing prepaid Claude/Codex capacity, yielding to interactive use, and surfacing only exceptions are highly specific to Connor. That specificity is a weak commercial moat but a strong build-vs-buy reason: general products optimize seats, tokens, or enterprise policy instead.

### What is not a moat

The six-way feature combination identified in the executive conclusion is differentiated today, but easy to describe and increasingly easy to copy. The UI is not the moat; the skills are not the moat; even the state vocabulary is not the moat. The plausible moat is accumulated trust in the transitions: repeated evidence that Ask promotes the right artifact, dispatch never outruns authority, review catches mistakes, restarts do not duplicate work, and the operator can reconstruct why something shipped.

## 10. What agentflow should learn or reuse

| Source | Adopt the idea | Avoid copying |
|---|---|---|
| **OpenSpec** | Make candidate vs current truth visibly different; show artifact diffs; allow fluid revision before promotion. | Treating specs as mandatory for every small change or adopting a second source of truth wholesale. |
| **Linear** | Keep project chat, maps, issues, activity, and review in one contextual workspace; make agent status legible inside the work object. | Making an internal database the final authority for milestones/issues already represented in GitHub/repo. |
| **Replit** | Let blank-project discovery flow directly into a reviewed task roadmap; compare mockup directions spatially; preserve checkpoints. | Expanding the first slice into hosting, databases, deployment, or a generic prompt-to-app builder. |
| **Factory Missions** | Define validation before decomposition; use fresh contexts per slice; re-plan at milestone boundaries; specialize models by role when evidence supports it. | Hiding critical transition policy inside one opaque orchestrator prompt. |
| **GitHub** | Use native issues, PRs, checks, review, and safe write boundaries wherever possible. | Rebuilding GitHub screens or mirroring all GitHub state into mutable local state. |
| **AO / Agent Canvas / Shep** | Treat agent/runtime/workspace integration as adapters or protocols; derive UI state from durable facts. | Rebuilding a generic terminal multiplexer or supporting dozens of agents before a second real provider seam requires it. |
| **BMAD / Spec Kit** | Package methods as focused workflows with explicit artifacts and handoffs. | A heavyweight universal lifecycle or role-play bureaucracy for ordinary changes. |

Two integration questions deserve later investigation, not inclusion in the first build:

- Whether ACP or another typed agent protocol can replace some custom runner integration without weakening agentflow's provider-start, outcome, and recovery facts.
- Whether OpenSpec-compatible proposed-change folders could be one candidate-artifact representation while maps, GitHub issues, ADRs, and mockups remain agentflow-native types. One representation should not be forced across all artifacts.

## 11. Recommendation

### Product decision

**Proceed with the experiment, not with a generic “agentic IDE” build-out.** The full seed is feasible and competitors validate demand, but its broad feature description is crowded. Building it is rational for this first user because agentflow already owns the expensive, idiosyncratic half—risk-aware execution across Connor's repos and subscriptions—and because the desired methodology/evidence policy is unusually specific. It is not yet a credible general-market product thesis.

Frame the product internally as:

> **The decision-to-evidence environment for a self-running repository fleet.**

That is narrower and more defensible than “a place to talk to coding agents.” Replit owns idea-to-app ergonomics; Linear/GitHub own general work tracking; Factory/Devin own hosted long-running agents; AO/Agent Canvas own generic fleet supervision; OpenSpec/Spec Kit/BMAD own portable methodology artifacts. Agentflow should connect judgment to trustworthy execution.

### First validation slice

Build exactly one existing-repository loop:

1. Open one repository project workspace.
2. Start or resume one Ask conversation grounded in the repository, current GitHub state, and relevant ADR/glossary context.
3. Ask stages exactly one typed candidate:
   - an ordinary issue when the work is crisp; or
   - a small decision map when multiple slices or unresolved decisions justify it.
4. The workspace shows the complete proposed GitHub/repo writes, their rationale/sources, and `edit / approve / discard`. No external truth changes before approval.
5. Approval publishes the artifact. Dispatch is a separate explicit action unless approval clearly included dispatch.
6. One promoted build issue enters the existing intake/build/review machinery rather than a new execution path.
7. The workspace projects the returned issue, PR, exact head SHA, CI result, independent review, merge/park state, and any required screenshot evidence back onto the originating candidate/map node.
8. Browser refresh and daemon restart preserve the conversation, candidate status, promotion proof, and evidence links without duplicate GitHub writes or duplicate dispatch.

### Observable validation criteria

The slice validates the thesis only if all of these hold on real work:

- **Artifact quality:** in at least three materially different requests, Ask's staged artifact is publishable with at most one substantive user correction.
- **Boundary clarity:** the user can always tell whether something is conversational, staged, promoted, dispatched, or shipped; no test participant mistakes a candidate for truth.
- **Authority safety:** zero GitHub/repo writes occur before approval, and replay/restart creates zero duplicate issues, comments, maps, or sessions.
- **Loop closure:** every dispatched ticket returns enough evidence to answer “what shipped, against which decision, and what proved it?” without opening provider transcripts.
- **Method threshold:** a crisp request stays an issue; a genuinely branching request becomes a map. The product does not create ceremony merely because it can.
- **Operator leverage:** compared with today's manual chat→GitHub→agentflow handoff, the end-to-end loop removes meaningful copying/coordination steps or catches a scope/evidence problem the manual flow misses.
- **North-star continuity:** the data model can later admit a blank project's Wayfinder conversation, coarse milestone, and first tracer bullet without replacing Project, Conversation, Candidate Artifact, Promotion, Map, Issue, Mockup, or Evidence semantics.

### Competitive benchmark for the slice

Run the same two brownfield requests through:

1. the proposed agentflow workspace;
2. GitHub Copilot app or the closest available GitHub-native agent flow; and
3. OpenSpec plus a normal coding-agent/PR workflow.

Compare correction count, manual handoffs, time-to-approved artifact, time-to-reviewable evidence, authority mistakes, and whether the final ledger explains the original decision. This is more informative than comparing model coding quality, because agentflow uses the same underlying model class as its competitors.

### Stop conditions

Do not continue toward blank-project creation if the slice shows that:

- Ask mostly restates what existing coding agents already return;
- candidate promotion feels like duplicate GitHub issue editing;
- evidence cannot be projected without introducing a conflicting second ledger;
- method routing creates more ceremony than judgment; or
- GitHub Copilot app, OpenSpec + runner, or another existing stack is equally trustworthy with materially less maintenance.

## 12. Methodology and limitations

### Method

- Read the complete original seed, resolved foundation, decision map, and child questions in [issue #123](https://github.com/ConnorGriffin/agentflow/issues/123); verified that the issue had no additional comments as of the research date.
- Grounded current agentflow against `PRODUCT.md`, `CONTEXT.md`, accepted ADRs, source files, and the indexed code architecture.
- Searched product documentation and GitHub for three kinds of competition: end-to-end project environments, execution/control planes, and methodology/artifact layers.
- Preferred official documentation, first-party release posts, and official repositories. Secondary comparison articles, social-media claims, and Reddit reports were excluded from conclusions.
- Distinguished verified current behavior from preview, beta, experimental, roadmap, or vendor-stated behavior where the source made that distinction.
- Scored products twice: against the original full north star and against the resolved existing-repository validation wedge.

### Limits

- This is a source review, not hands-on product testing. A documented workflow may be awkward, unreliable, plan-gated, or unavailable to this account in practice.
- Commercial systems expose less architecture than open-source projects. Claims about Factory, Devin, Linear, Replit, Kiro, and similar products describe their first-party contracts, not independently verified reliability.
- The market is moving weekly. Preview status, feature names, pricing, model support, and agent limits may change after 2026-07-16.
- GitHub stars/forks are point-in-time popularity signals and are easy to misread; they do not prove quality, active use, safety, or fit.
- The numerical scores are explicit analyst judgments used to make tradeoffs visible. A one-point difference is not statistically meaningful.
- “Proposed agentflow north star = 27” is a statement of intended scope, not evidence that the design is coherent or achievable. The validation slice exists to test that.
- Current agentflow is evolving rapidly. Accepted ADRs may describe a migration target that is only partly implemented; the baseline section cites source boundaries where that distinction matters.
- No verified competitor was found with the exact six-part combination, but absence from public docs is not proof a private/internal system lacks it.

## Bottom line

The original seed is neither absurd nor novel. It describes where several large categories are converging. Replit is already close to the greenfield/design experience; Linear and GitHub are close to the persistent project/ledger experience; Factory is close to the long-horizon execution experience; OpenSpec, Spec Kit, and BMAD are close to the methodology/artifact experience; and Shep, AO, and Agent Canvas are close to the local fleet-control experience.

Agentflow's opportunity is to make those pieces behave as one trustworthy system for this operator: **the right method, the right promoted artifact, the right level of autonomy, and durable evidence back to the decision that authorized the work.** If the first brownfield slice proves that transition better than an assembled stack, the full seed is worth continuing. If it does not, the competition already offers enough parts to avoid building the rest.
