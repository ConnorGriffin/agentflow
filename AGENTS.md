# AGENTS.md — agentflow

The tool-agnostic autonomous issue → PR → review pipeline. This repo **is** the
engine, enrolled into its own fleet to dogfood it. Python, uv-managed.

profile: reviewed
ui-surfaces: agentflow/webui/src/

## Repo facts
- **Python, uv.** Install: `uv sync --group dev`. **Test / CI gate:** `uv run pytest -q`.
- **DCO is required.** Every commit carries a `Signed-off-by` trailer (`git commit -s`);
  the `dco` check blocks any PR with an unsigned commit.
- Source: `agentflow/`. Tests: `tests/`. Design: `docs/adr/` + `CONTEXT.md`. Mockups: `mockups/`.
- **Frontend:** the v2 console `agentflow/webui/` (Svelte SPA + `agentflow/webapp.py`
  FastAPI, locked spec `mockups/dashboard-v2-combined.html`, ADR 0023) — the retired
  stdlib dashboard is gone (ADR 0026). Build the console with `npm ci && npm run build`
  in `agentflow/webui/`; serve it with `uv run agentflow-web`. The server only reads
  the snapshot the daemon publishes — it never queries GitHub (ADR 0026). **Any UI
  change goes through `/ui-craft lock` first** (charter gate), and must honor
  `PRODUCT.md`/`DESIGN.md` (no side-stripe accents, etc.).
- **Never `git stash` in a fleet worktree.** The stash is a single repo-wide stack shared by
  every worktree of this repo, so a `stash push` here is visible to — and poppable by — a
  session working somewhere else entirely. A concurrent `pop` silently moves another issue's
  uncommitted work into your tree, which then contaminates whatever you test and can strand the
  owning session's only copy. This has happened (2026-08-17: issue #736's liveness work was
  pulled into an unrelated worktree by a stash race, and the two copies diverged).
  To set changes aside, commit them on your own branch — a WIP commit you amend or drop later
  is worktree-local and costs nothing. If you find work in your tree you did not write, do not
  discard it: save it (`git diff > <patch>`) and say so.
- **Driving the pipeline by hand needs the daemon's env.** `AGENTFLOW_PERMIT_BUDGET=25` lives
  only in the launchd plist, so a by-hand `build_issue` / `review_pr` reads the packaged default
  of 5 and reports `no pool has headroom` against a budget the daemon is not using. Prefix
  `AGENTFLOW_PERMIT_BUDGET=25` on by-hand invocations. A by-hand verb reporting Codex as
  `capacity helper not configured` is the same launchd-only blind spot (#727), not a real
  misconfiguration — never diagnose Codex capacity from it.
- **No hazards:** no real data / PHI, no live credentials. (The daemon *spawns* agent
  sessions, but a build session working on this repo doesn't need to.)
- **Why `reviewed`:** changes to the merge machinery itself are correctness-sensitive —
  a human merges changes to the thing that decides merges.
