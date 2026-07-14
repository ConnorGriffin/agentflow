# AGENTS.md — agentflow

The tool-agnostic autonomous issue → PR → review pipeline. This repo **is** the
engine, enrolled into its own fleet to dogfood it. Python, uv-managed.

profile: reviewed
ui-surfaces: agentflow/static/ agentflow/webui/src/

## Repo facts
- **Python, uv.** Install: `uv sync --group dev`. **Test / CI gate:** `uv run pytest -q`.
- Source: `agentflow/`. Tests: `tests/`. Design: `docs/adr/` + `CONTEXT.md`. Mockups: `mockups/`.
- **Frontend:** the stdlib dashboard `agentflow/static/dashboard.html` (locked spec
  `mockups/dashboard-inbox.html`) still ships; the v2 console `agentflow/webui/`
  (Svelte SPA + `agentflow/webapp.py` FastAPI, locked spec
  `mockups/dashboard-v2-combined.html`, ADR 0023) is landing in slices. Build the
  console with `npm ci && npm run build` in `agentflow/webui/`; serve it with
  `uv run agentflow-web`. **Any UI change goes through `/ui-mockups` first** (charter
  gate), and must honor `PRODUCT.md`/`DESIGN.md` (no side-stripe accents, etc.).
- **No hazards:** no real data / PHI, no live credentials. (The daemon *spawns* agent
  sessions, but a build session working on this repo doesn't need to.)
- **Why `reviewed`:** changes to the merge machinery itself are correctness-sensitive —
  a human merges changes to the thing that decides merges.
