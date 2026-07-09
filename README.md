# agentflow

A tool-agnostic, **autonomous issue → PR → review pipeline** that runs on either
Claude (Opus) or Codex (GPT-5.6 Sol), tuned per repo by a single **autonomy
profile** dial — from a vibe-code project that merges its own green PRs to a
medical-adjacent repo that waits for a human.

> **Status: design in progress.** The design is being derived from scratch (no
> prior art) via a grilling session. Decisions land in [`docs/adr/`](docs/adr/)
> and terms in [`CONTEXT.md`](CONTEXT.md) as they crystallize. Nothing is wired to
> run yet.

## Why this exists

Both coding agents are now full-loop autonomous (GPT-5.6 Sol shipped 2026-07-09).
The earlier `dotfiles` workflow assumed Codex was weak and confined it to a frozen
"hermetic middle" — an assumption the capability jump erased. `agentflow` starts
over: the tool is an interchangeable **runner**, and the only thing that varies
per repo is **domain risk**, expressed as an autonomy profile.

## Consumers

- `ciq-autotune` — medical-adjacent; the paranoid end of the dial.
- `work-kit` — the work (Jira/IaC) port.
- vibe-code projects — the autonomous end of the dial.
