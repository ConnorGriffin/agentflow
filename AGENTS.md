# AGENTS.md — agentflow

The tool-agnostic autonomous issue → PR → review pipeline. This repo **is** the
engine, enrolled into its own fleet to dogfood it. Python, uv-managed.

profile: reviewed

## Repo facts
- **Python, uv.** Install: `uv sync --group dev`. **Test / CI gate:** `uv run pytest -q`.
- Source: `agentflow/`. Tests: `tests/`. Design: `docs/adr/` + `CONTEXT.md`. Mockups: `mockups/`.
- **Frontend:** `agentflow/static/dashboard.html` — the locked visual spec is
  `mockups/dashboard-inbox.html`; **any UI change goes through `/ui-mockups` first**
  (charter gate), and must honor `PRODUCT.md`/`DESIGN.md` (no side-stripe accents, etc.).
- **No hazards:** no real data / PHI, no live credentials. (The daemon *spawns* agent
  sessions, but a build session working on this repo doesn't need to.)
- **Why `reviewed`:** changes to the merge machinery itself are correctness-sensitive —
  a human merges changes to the thing that decides merges.
