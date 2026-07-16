# Public project-name viability: `agentflow`

**Research date:** 2026-07-16

**Issue:** [#139](https://github.com/ConnorGriffin/agentflow/issues/139)

**Question:** Is `agentflow` sufficiently distinctive and usable as this project's public name, or should it be renamed before launch?

## Decision

**Recommendation — rename before public launch.** This is a product and distribution recommendation, not a legal conclusion.

`agentflow` fails the practical no-go threshold on non-legal grounds alone:

1. The exact PyPI project name is already owned and cannot be used for this repository's currently declared Python distribution without a transfer. A separated distribution spelling is a technical workaround—`agent-flow`, `agent_flow` and `agent.flow` normalize to the same `agent-flow`, which is distinct from unseparated `agentflow`; the official JSON lookup for `agent-flow` returned 404 on the research date. That workaround would not recover the exact public/package identity. [PyPI `agentflow` record](https://pypi.org/project/agentflow/), [`agent-flow` JSON lookup](https://pypi.org/pypi/agent-flow/json), [Python package-name normalization](https://packaging.python.org/en/latest/specifications/name-normalization/)
2. Several active tools already expose an `agentflow` command or product in the same coding-agent / multi-agent orchestration category. The closest, `berabuddies/agentflow`, is a Python/JavaScript project that orchestrates Codex and Claude, installs an `agentflow` CLI and skill, and includes a local web UI. [`berabuddies/agentflow`](https://github.com/berabuddies/agentflow)
3. The exact U.S. standard-character mark **AGENTFLOW** is live and registered on the Supplemental Register for Class 42 AI-agent platform/SaaS services. A second Class 42 application for **RELTIO AGENTFLOW** is live/pending in the United States, with a parallel published EU application. These facts require trademark counsel before any public use in overlapping software services. [USPTO serial 98568900](https://tsdr.uspto.gov/#caseNumber=98568900&caseSearchType=US_APPLICATION&caseType=DEFAULT&searchType=statusSearch), [USPTO serial 99421153](https://tsdr.uspto.gov/#caseNumber=99421153&caseSearchType=US_APPLICATION&caseType=DEFAULT&searchType=statusSearch), [EUIPO application 019339451](https://euipo.europa.eu/eSearch/#details/trademarks/019339451)
4. The obvious identity set is fragmented: the exact GitHub organization slug is held by an unrelated organization, all seven exact-match domains checked are registered, and the name is already used by public software, research, extensions, and enterprise products. [GitHub `@AgentFlow`](https://github.com/AgentFlow), [`agentflow.io`](https://agentflow.io/), [Reltio AgentFlow](https://docs.reltio.com/en/products/agentflow/reltio-agentflow-at-a-glance), [Stanford AgentFlow](https://agentflow.stanford.edu/)

**Qualifying the name is not the recommended fallback.** A house-mark form such as “X AgentFlow” could be legally different in some contexts, but it would not recover the PyPI name, remove CLI collisions, or make search results distinctive. Keeping `agentflow` only as an internal codename is operationally possible; shipping it as the public product/package/CLI name is not.

## Evidence labels

- **Documented fact** — stated by a primary source or official registry record.
- **Repository observation** — read directly from this repository or another project's first-party repository.
- **Inference** — an assessment from documented facts; it is not a registry or legal determination.
- **Recommendation** — a proposed decision or threshold.
- **Uncertainty** — an evidence gap that a negative search must not conceal.

## 1. What this repository actually is

**Repository observation.** The current project declares itself as a tool-agnostic autonomous `issue → PR → review` pipeline that runs Claude or Codex across a fleet of GitHub repositories. Its Python distribution is named `agentflow`; its public script is currently `agentflow-web`; and its Svelte package is private under the separate internal name `agentflow-console`. [README](../../README.md), [`pyproject.toml`](../../pyproject.toml), [`agentflow/webui/package.json`](../../agentflow/webui/package.json)

The product is presently a solo operator console rather than a customer-facing SaaS product, but it is still software for operating AI coding agents: it surfaces fleet state, autonomous work, reviews, merges, held work and trust policy. [PRODUCT.md](../../PRODUCT.md), [CONTEXT.md](../../CONTEXT.md) The accepted architecture uses a Svelte/FastAPI control plane, while the daemon owns the GitHub-backed snapshot and the web server serves that state. [ADR 0023](../adr/0023-dashboard-replatform-control-plane.md), [ADR 0026](../adr/0026-daemon-owned-snapshot.md)

**Inference.** The relevant public category is therefore not generic workflow software. It is developer tooling for coordinating AI/coding agents, with a Python package, shell-visible commands and skills, GitHub issue/PR automation, and an operator UI. Collisions in that exact category matter more than unrelated uses in real estate or sales.

## 2. GitHub repositories and organizations

### Method

On 2026-07-16, authenticated GitHub REST searches were run with:

```text
gh api --method GET search/repositories \
  -f 'q=agentflow in:name' -f per_page=100
gh api --method GET search/users \
  -f 'q=agentflow in:login type:org' -f per_page=100
```

The repository response reported `total_count: 1176` and `incomplete_results: false`; the organization response reported 22 matches and `incomplete_results: false`. [`agentflow in:name` API result](https://api.github.com/search/repositories?q=agentflow%20in%3Aname&per_page=100), [`agentflow in:login type:org` API result](https://api.github.com/search/users?q=agentflow%20in%3Alogin%20type%3Aorg&per_page=100)

**Limitation.** `in:name` is a case-insensitive substring search, not exact equality; the count includes names such as `agentflow-*`. Only the first 100 relevance-sorted repository results were inspected, forks were not requested, private repositories outside the authenticated account were invisible, and GitHub search exposes at most 1,000 results. Exact-name matches therefore had to be inspected manually; the count is a collision-density signal, not an exhaustive exact-name count. [GitHub repository-search documentation](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories), [GitHub Search REST documentation](https://docs.github.com/en/rest/search/search#search-repositories)

The exact, case-insensitive organization slug is already occupied: [`github.com/agentflow`](https://github.com/agentflow) existed on the research date and exposed two unrelated public repositories. [GitHub organization API](https://api.github.com/orgs/agentflow)

### Material exact-name results

| Project | Documented fact | Relevance to this repository |
|---|---|---|
| [`berabuddies/agentflow`](https://github.com/berabuddies/agentflow) | Public Python/JavaScript project; observed at about 1.3k stars. It orchestrates Codex, Claude and Kimi in dependency graphs, imports `agentflow`, installs an `agentflow` CLI/skill, and serves a local web UI. | **Direct category and interface collision.** Same public name, import, CLI, agent runners, orchestration concept and local control surface. |
| [`harun-yardimci/agentflow`](https://github.com/harun-yardimci/agentflow) | Pipeline-based orchestration for Claude Code, Codex, Gemini, and Antigravity; it provides the `agentflow` command, isolated Git worktrees, a visual dashboard, SQLite state, and an MCP server. | **Near-total product and interface collision.** |
| [`Tweakzx/agentflow`](https://github.com/Tweakzx/agentflow) | A stage-first control layer for coding agents with issue intake, task lifecycle, gates, evidence history, multi-repo operation, an `agentflow` CLI, and a web console. | **Direct category, vocabulary, CLI, and control-plane collision.** |
| [`aaronrussell/agentflow`](https://github.com/aaronrussell/agentflow) | JavaScript framework for AI workflows using Markdown and natural language; it publishes the `@agentflow/core`, `@agentflow/cli`, and `@agentflow/tools` npm packages. | **Exact framework-name and npm-namespace collision.** |
| [`lupantech/AgentFlow`](https://github.com/lupantech/AgentFlow) / [project site](https://agentflow.stanford.edu/) | Public Python research framework observed at about 1.9k stars; an ICLR 2026 oral project for a planner/executor/verifier/generator agentic system. | Large, current exact-name search/discoverability collision in AI agents, even though it is not coding-fleet software. |
| [`OpenDCAI/AgentFlow`](https://github.com/OpenDCAI/AgentFlow) | Public Python framework for agent data synthesis and evaluation. | Additional current exact-name AI-framework collision. |
| [GitHub `@AgentFlow`](https://github.com/AgentFlow) | The case-insensitive `agentflow` organization slug resolves to an unrelated organization branded “pascal” with two public repositories. | The obvious public organization handle is unavailable. |

**Inference.** Search attribution would be poor even if no trademark existed. Multiple projects already occupy the same semantic sentence—“AgentFlow orchestrates Codex and Claude”—and expose the same terminal command.

## 3. Package registries

### PyPI

**Method.** Checked the exact project page and official JSON record, then direct close-form project pages (`agentsflow`, `langgraph-agentflow`). PyPI's search-result page was not usable in this environment, so there is no claim that these are every close form.

| Name | Official record | Finding |
|---|---|---|
| `agentflow` | [PyPI page](https://pypi.org/project/agentflow/), [JSON API](https://pypi.org/pypi/agentflow/json) | Version 0.0.2, released 2023-05-29, owner/maintainer `spstoyanov`, described as “A library for creating agents.” The release is not yanked. **The exact distribution name is occupied.** |
| `agent-flow` / `agent_flow` / `agent.flow` | [`agent-flow` JSON lookup](https://pypi.org/pypi/agent-flow/json) | The official API returned 404 on 2026-07-16. These punctuation variants normalize to `agent-flow` and are equivalent to one another, but not to unseparated `agentflow`. This is a possible distribution-name workaround, not evidence that the public name is distinctive. |
| `agentsflow` | [PyPI page](https://pypi.org/project/agentsflow/) | A production-grade “control plane for coding agents” using Claude, Codex or Gemini, GitHub/Jira issues and Git worktrees. This is a close-form, same-category collision. |
| `langgraph-agentflow` | [PyPI page](https://pypi.org/project/langgraph-agentflow/) | Python library for planning, routing and executing multi-step agent workflows. This is a close-form category collision. |

**Documented fact.** Python project names are compared after lowercasing and replacing runs of `.`, `-` and `_` with `-`. Thus `AgentFlow` collides with `agentflow`, while inserting a separator produces the distinct normalized name `agent-flow`. [PyPA normalization specification](https://packaging.python.org/en/latest/specifications/name-normalization/)

**Uncertainty.** A PyPI transfer is possible only under the formal name-retention policy. The policy requires attempts to contact the owner and, for reuse by a different project, abandonment, notability, low use, and a showing that a different name is not an acceptable workaround; a reachable owner cannot be overridden. No transfer request was made. [PEP 541](https://peps.python.org/pep-0541/)

### npm

**Method.** Ran `npm view agentflow --json`, `npm view agentflow-console --json`, and `npm search agentflow --json` against the configured official npm registry on 2026-07-16.

Both exact unscoped lookups returned registry `E404` responses. This means no public package was returned at either exact name at that instant; it does not reserve the names, prove that npm would accept a publication, or confer trademark rights. [`agentflow` registry endpoint](https://registry.npmjs.org/agentflow), [`agentflow-console` registry endpoint](https://registry.npmjs.org/agentflow-console)

The nearby namespace is already crowded in the same category:

- [`@agentflow/core`](https://www.npmjs.com/package/%40agentflow/core), [`@agentflow/cli`](https://www.npmjs.com/package/%40agentflow/cli), and [`@agentflow/tools`](https://www.npmjs.com/package/%40agentflow/tools) occupy the obvious `@agentflow` scope for an existing AI-workflow framework.
- [`@argustech/agentflow`](https://www.npmjs.com/package/%40argustech/agentflow) installs an `agentflow` command for orchestrating Claude, Codex, and Gemini through a visual dashboard and MCP server.
- [`@fieldwangai/agentflow`](https://www.npmjs.com/package/%40fieldwangai/agentflow) orchestrates long-running work through Cursor, OpenCode, Claude Code, or Codex.
- [`@flowiseai/agentflow`](https://www.npmjs.com/package/%40flowiseai/agentflow) is the SDK for Flowise's established Agentflow product surface. [Flowise Agentflow V2 documentation](https://docs.flowiseai.com/using-flowise/agentflowv2)

**Repository observation.** The current Svelte console package is `private: true`, so npm is not an immediate publishing blocker for today's console. It is still strong evidence that the name and terminal vocabulary are crowded in the exact developer audience. [`package.json`](../../agentflow/webui/package.json)

**Scope decision.** No other language registry was treated as launch-critical: this repository currently distributes Python and has a private JavaScript console. Container, Rust, Ruby and Java registries were not searched.

## 4. Domains

### Method and limitations

Registry endpoints were selected from IANA's RDAP bootstrap data, then each exact domain was queried through its registry-operated RDAP service; `.io` was also checked through the official `whois.nic.io` service. [IANA RDAP bootstrap](https://data.iana.org/rdap/dns.json), [IANA `.io` delegation](https://www.iana.org/domains/root/db/io.html)

**Documented fact.** Every exact domain checked was registered on 2026-07-16:

| Domain | Registry result | Separate HTTP/product observation |
|---|---|---|
| `agentflow.com` | Registered 2003-11-10; current record expires 2026-11-10. [Verisign RDAP](https://rdap.verisign.com/com/v1/domain/agentflow.com) | Root HTTPS timed out; this says nothing about registration. |
| `agentflow.dev` | Registered 2024-07-09; current record expires 2027-07-09. [Google Registry RDAP](https://pubapi.registry.google/rdap/domain/agentflow.dev) | Root fetch failed; this says nothing about registration. |
| `agentflow.io` | Registered 2022-06-02; current record expires 2027-06-02. [Identity Digital RDAP](https://rdap.identitydigital.services/rdap/domain/agentflow.io) | Root serves a live AgentFlow real-estate operations product. [`agentflow.io`](https://agentflow.io/) |
| `agentflow.org` | Registered 2024-10-14; current record expires 2026-10-14. [Public Interest Registry RDAP](https://rdap.publicinterestregistry.org/rdap/domain/agentflow.org) | Not needed to determine availability. |
| `agentflow.ai` | Registered 2023-04-14; current record expires 2027-04-14. [Identity Digital RDAP](https://rdap.identitydigital.services/rdap/domain/agentflow.ai) | Root HTTPS timed out; this says nothing about registration. |
| `agentflow.net` | Registered 2023-03-28; current record expires 2028-03-28. [Verisign RDAP](https://rdap.verisign.com/net/v1/domain/agentflow.net) | Not needed to determine availability. |
| `agentflow.app` | Registered 2025-06-02; current record expires 2027-06-02. [Google Registry RDAP](https://pubapi.registry.google/rdap/domain/agentflow.app) | Registry nameservers indicate a marketplace landing configuration; no acquisition terms were investigated. |

**Limitations.** Public RDAP/WHOIS is the highest-trust public registration-data surface, but registry notices disclaim accuracy and legal authority. Expiry dates are not availability dates, registrants are often redacted, and no purchase or transfer outreach was made. DNS and HTTP behavior were treated separately from registration.

**Inference.** Domain availability does not decide trademark clearance, but this set provides no clean exact-name identity.

## 5. Existing products, apps and companies

These are first-party product/repository/official-store observations, not search-result snippets:

| Use | Documented fact | Confusion relevance |
|---|---|---|
| [Reltio AgentFlow](https://docs.reltio.com/en/products/agentflow/reltio-agentflow-at-a-glance) | Current enterprise product with prebuilt/custom agents, governed APIs, an AgentFlow workspace, and long-running autonomous tasks. [Reltio launch release](https://www.reltio.com/resources/press-releases/reltio-agentflow/) | Same broad AI-agent software/services field; backed by active U.S. and EU applications. |
| [Flowise Agentflow V2](https://docs.flowiseai.com/using-flowise/agentflowv2) | Core Flowise surface for explicit multi-agent workflow orchestration, state, checkpoints, human input, MCP tools, and long-running agents. | Established use of “Agentflow” as a product/category term in the same AI-orchestration market. |
| [Stanford AgentFlow](https://agentflow.stanford.edu/) | Trainable agentic framework presented as an ICLR 2026 oral project. | Exact AI framework name with substantial public visibility. |
| [AgentFlow VS Code extension](https://marketplace.visualstudio.com/items?itemName=AgentFlow.agentflow) | “AI Workflow Visualizer” for Claude Code and other coding-agent session transcripts. | Same developer audience and coding-agent category. |
| [`agentflow.io`](https://agentflow.io/) | Real-estate operations platform for agents. | Exact product/domain use, but a different meaning of “agent.” |
| [AgentFlow business platform](https://www.agentflow-ai.com/) | First-party site markets finance, sales and marketing AI agents under AgentFlow. | Exact AI-agent product use outside developer tooling. |

**Inference.** The name is descriptive-looking in this market: many unrelated builders independently combine “agent” and “flow” for agent workflows. That explains the collision density but does not determine legal distinctiveness. It does mean public search, word-of-mouth attribution and support queries would need a permanent owner qualifier.

## 6. Trademark registers

### Important boundary

This section records official register facts and search limitations. It does **not** decide infringement, priority, validity, geographic scope, common-law rights, fair use, abandonment/non-use, or likelihood of confusion. Those are counsel-required judgments.

USPTO itself explains that likelihood of confusion depends on both similarity of the marks and relatedness of the goods/services; identical classes are not required. [USPTO guidance](https://www.uspto.gov/trademarks/search/likelihood-confusion)

### USPTO — United States

**Method.** In the official Trademark Search system, ran a Basic `Wordmark` search for `AgentFlow` with both Live and Dead status filters enabled, then opened the exact record. Also ran the close-form Basic search `Agent Flow` and opened the identified Reltio serial directly. [USPTO Trademark Search](https://tmsearch.uspto.gov/), [USPTO search guidance](https://www.uspto.gov/trademarks/search/federal-trademark-searching)

Queries and observed result counts:

```text
Wordmark: AgentFlow  -> 2 results; 2 live, 0 dead
Wordmark: Agent Flow -> 9,224 token matches; 3,096 live, 6,128 dead
Serial: 98568900
Serial: 99421153
```

The spaced query was tokenized broadly and is not a usable close-form clearance by itself. No claim is made that Basic mode found every phonetic, spelling, design, or meaning-near mark.

Material official records:

| Mark / record | Status and owner | Class / goods and services | Why counsel must review it |
|---|---|---|---|
| **AGENTFLOW**, standard characters; serial **98568900**, registration **7656187** | **Live; registered 2025-01-14 on the Supplemental Register. Owner: Elevasis LLC.** [Official TSDR record](https://tsdr.uspto.gov/#caseNumber=98568900&caseSearchType=US_APPLICATION&caseType=DEFAULT&searchType=statusSearch) | Nice **42**. PaaS/SaaS for creating, managing and deploying AI agents; visual-graph tools for building AI agents/AI systems; interfaces for interacting with and monitoring agents; software for managing agent flows and integrations. | This is the exact word mark and its recited services directly overlap the broader AI-agent software category. Counsel must assess the Supplemental Register, scope, priority/first use, actual use and the distinction between downloadable developer tooling and hosted services. |
| **RELTIO AGENTFLOW**, standard characters; serial **99421153** | **Live/pending. Owner: Reltio, Inc.** [Official USPTO record](https://tmsearch.uspto.gov/search/search-results/99421153) | Nice **42**. AI-as-a-service for enterprise governance, integration, master-data management, data unification/warehousing and software/network analytics; online software connecting AI agents to databases and data platforms. | The additional house mark reduces literal identity but `AGENTFLOW` remains the shared element in overlapping AI software/services. Outcome and any amended identification remain unresolved. |

USPTO explains that the Supplemental Register is for marks not yet eligible for the Principal Register and gives fewer presumptions, but such registrations are still protected against conflicting later-filed applications. That nuance does not resolve whether this use would infringe or whether the registration is enforceable; counsel must review it. [USPTO Supplemental Register guidance](https://www.uspto.gov/trademarks/laws/how-amend-principal-supplemental-register)

**Uncertainty.** This was a targeted registry sweep, not a comprehensive U.S. clearance search. Expert-mode fuzzy searches, designs, coordinated classes, state registrations, company names, and unregistered/common-law uses remain unswept.

### EUIPO — European Union

**Method.** Ran official eSearch Basic searches for `AgentFlow` and the close form `Agent Flow`, then opened the returned record.

`AgentFlow` returned one trademark: **RELTIO AGENTFLOW**, EUTM application **019339451**. It was filed 2026-03-30 by Reltio, Inc., was **Application published** on the research date, and covers Class 42 AIaaS and cloud software connecting AI agents to databases and data platforms. `Agent Flow` returned zero Basic-search results. [Official `AgentFlow` query](https://euipo.europa.eu/eSearch/#basic/1+1+1+1/100+100+100+100/AgentFlow), [official EUIPO record](https://euipo.europa.eu/eSearch/#details/trademarks/019339451)

**Uncertainty.** A zero Basic-search result for the spaced form is not legal clearance. eSearch does not replace fuzzy searches, TMview/national-register searches, Madrid/WIPO searches, or counsel's assessment of related goods and services.

### WIPO Global Brand Database

**Method.** Attempted to open the official Global Brand Database for the same exact and close forms.

**Uncertainty — no result set obtained.** The search application was blocked by the client, and WIPO's terms/FAQ prohibit automatic querying, so no automated fallback was used and no WIPO negative result is claimed. WIPO says its database combines Madrid marks with participating national/regional collections, may lag national updates, and should be supplemented with national/regional office searches. [WIPO Global Brand Database](https://www.wipo.int/en/web/global-brand-database), [WIPO FAQ and automation/coverage limits](https://www.wipo.int/en/web/global-brand-database/faqs_branddb)

## 7. Confusion and usability risk in this project's real category

| Axis | Repository-grounded assessment | Risk |
|---|---|---|
| Product purpose | This project coordinates Claude/Codex work from issue through PR, independent review and merge; it also presents a fleet control plane. Existing `berabuddies/agentflow`, `harun-yardimci/agentflow`, `Tweakzx/agentflow`, and the VS Code AgentFlow all operate coding or developer agents. [README](../../README.md), [`berabuddies/agentflow`](https://github.com/berabuddies/agentflow), [`harun-yardimci/agentflow`](https://github.com/harun-yardimci/agentflow), [`Tweakzx/agentflow`](https://github.com/Tweakzx/agentflow), [VS Code extension](https://marketplace.visualstudio.com/items?itemName=AgentFlow.agentflow) | **Very high practical confusion.** |
| Installation | This repo declares the exact PyPI distribution `agentflow`; PyPI already serves a different package at that name. A separated distribution name appears technically available but would differ from the declared/public name. [Local metadata](../../pyproject.toml), [PyPI record](https://pypi.org/project/agentflow/), [`agent-flow` lookup](https://pypi.org/pypi/agent-flow/json) | **Blocking for publication under the exact declared name; workaround available with identity cost.** |
| CLI and skill vocabulary | This repo uses `/agentflow`, `agentflow-web`, `.agentflow/`, `agentflow:*` labels and `AGENTFLOW_*` variables. Other same-category npm/GitHub projects already document an `agentflow` executable, state directory, MCP server, and skill. [CONTEXT.md](../../CONTEXT.md), [`berabuddies/agentflow`](https://github.com/berabuddies/agentflow), [`harun-yardimci/agentflow`](https://github.com/harun-yardimci/agentflow) | **High support and documentation ambiguity.** |
| Search/discovery | Two visible exact-name AI research frameworks and a same-category 1.3k-star coding-agent orchestrator outrank a new launch; the obvious GitHub organization slug is also occupied. [`lupantech/AgentFlow`](https://github.com/lupantech/AgentFlow), [`OpenDCAI/AgentFlow`](https://github.com/OpenDCAI/AgentFlow), [`berabuddies/agentflow`](https://github.com/berabuddies/agentflow), [`@AgentFlow`](https://github.com/AgentFlow) | **High discoverability cost.** |
| Trademark proximity | Exact live U.S. registration for AI-agent SaaS plus pending U.S./EU Reltio applications in Class 42. [USPTO exact record](https://tsdr.uspto.gov/#caseNumber=98568900&caseSearchType=US_APPLICATION&caseType=DEFAULT&searchType=statusSearch), [USPTO Reltio record](https://tsdr.uspto.gov/#caseNumber=99421153&caseSearchType=US_APPLICATION&caseType=DEFAULT&searchType=statusSearch), [EUIPO Reltio record](https://euipo.europa.eu/eSearch/#details/trademarks/019339451) | **Counsel-required; unacceptable default uncertainty before launch.** |
| Domains/products | The seven exact domains checked were registered ([`.com`](https://rdap.verisign.com/com/v1/domain/agentflow.com), [`.dev`](https://pubapi.registry.google/rdap/domain/agentflow.dev), [`.io`](https://rdap.identitydigital.services/rdap/domain/agentflow.io), [`.org`](https://rdap.publicinterestregistry.org/rdap/domain/agentflow.org), [`.ai`](https://rdap.identitydigital.services/rdap/domain/agentflow.ai), [`.net`](https://rdap.verisign.com/net/v1/domain/agentflow.net), [`.app`](https://pubapi.registry.google/rdap/domain/agentflow.app)); public AgentFlow products/apps span [AI orchestration](https://docs.flowiseai.com/using-flowise/agentflowv2), [enterprise data](https://docs.reltio.com/en/products/agentflow/reltio-agentflow-at-a-glance), and [real estate](https://agentflow.io/). | **High identity fragmentation; lower legal relevance for unrelated goods.** |

**Inference.** Users are likely to assume affiliation with, confuse installation instructions for, or search into a different AgentFlow. The closest open-source collision is not hypothetical: it shares Python, JavaScript, Codex, Claude, an `agentflow` import/CLI/skill, orchestration graphs and a local web UI.

## 8. Practical go/no-go rule

### Current name

**No-go.** Do not announce, publish to package registries, create public marketing, or expand public use under `agentflow` until either:

- the project is renamed; or
- trademark counsel gives a written clearance for a specific qualified mark and exact goods/services, the PyPI/CLI distribution is renamed or lawfully transferred, and a coherent public identifier set is secured.

The second path costs most of the value of keeping the name and still leaves poor search attribution. It is not recommended.

### Threshold for a replacement candidate

A replacement is launchable only when all of these are true at the same dated checkpoint:

1. **Marks:** counsel clears exact, phonetic and meaning-near forms in the United States and likely launch markets, including USPTO/EUIPO/WIPO plus relevant national/common-law checks, with specific attention to Classes 009 and 042 and coordinated Class 035.
2. **Developer identity:** no active same-category project owns the exact project/CLI name; authenticated GitHub repository and organization searches have been manually exact-filtered.
3. **Distribution:** the normalized PyPI project name is available; the intended npm unscoped or scoped name is available; the installed executable does not collide with a material current coding-agent tool.
4. **Web identity:** at least one strong product domain and sensible defensive variants are available at normal registration or an explicitly accepted acquisition price. RDAP registration, DNS and HTTP are checked separately.
5. **Attribution test:** an exact web/GitHub/package search points predominantly to the candidate or to clearly unrelated uses, not another AI developer tool.

## 9. Follow-up checks before choosing the replacement

1. Have trademark counsel review the complete prosecution history and current use of **AGENTFLOW** registration 7656187, including its Supplemental Register status, and the pending Reltio U.S./EU files. The judgment needed is not “same Nice class?” but whether this project's actual downloadable software, hosted console and future services are related enough to create risk.
2. Run a manual WIPO Global Brand Database search and manual EUIPO eSearch/TMview search for every candidate. Export or screenshot the dated result sets; do not convert no results into a legal availability claim.
3. Repeat the authenticated GitHub API sweep for every candidate, include forks, page within GitHub's limits, and post-filter exact repository names and organization logins. Record `total_count`, `incomplete_results`, query, UTC timestamp, and the 1,000-result ceiling.
4. Re-run official registry RDAP/WHOIS checks immediately before reservation and save the raw responses for the chosen exact domains. Check DNS records and HTTP redirects as distinct observations.
5. Check PyPI normalized names and npm exact/scoped names immediately before reservation and again immediately before launch. Registry availability is volatile.
6. Sweep state business-name/trademark registers, app stores, extension marketplaces, major Linux package repositories, social handles and unregistered commercial uses for the final candidate. These are outside this note's official federal/regional register sweep.
7. Only after the name decision, reserve identifiers in one coordinated pass and update the Python distribution, imports, script names, `/agentflow` skill, labels, environment variables, state directories, GitHub repository/organization, console title and docs. This research made no reservations and changed no external state.

## 10. Residual uncertainty

- Trademark rights and likelihood of confusion are legal questions. This note identifies records and operational collisions; it does not opine on enforceability, infringement or the effect of Supplemental Register status.
- WIPO results were inaccessible under the available tools. No WIPO absence claim is made.
- GitHub's 1,176 count is a substring count; only the first 100 results were inspected and the API's 1,000-result ceiling prevents exhaustive paging of that query.
- RDAP/WHOIS records establish the dated public registration state, not ownership rights, future renewal, willingness to sell, or trademark clearance.
- Registry, mark, package, domain and app status can change after 2026-07-16.
- This was not a comprehensive common-law, company-name, state-register or every-country clearance search.

## Bottom line

**Recommendation.** Rename now, while the project is pre-launch. `agentflow` is not merely popular vocabulary with tolerable unrelated uses; it is already an exact Python package, an exact live U.S. mark for AI-agent software services, a pending enterprise AI mark in the U.S. and EU, and the public CLI/project name of multiple coding-agent tools. Qualification cannot repair the distribution and terminal collisions. A fresh distinctive name is cheaper than launching into this identity debt.
