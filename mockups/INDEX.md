# Mockups library

One line per surface. `locked` = binding visual spec for its implementation;
`shipped` = now in the app (harvest from the shipping frontend, never from here —
archived markup is stale by definition).

Grounding for this repo: theme tokens are lifted from `agentflow/static/dashboard.html`
(`:root` block — GitHub-dark-derived). Real data shape is
`agentflow.dashboard_data.snapshot()`, captured to `dashboard.capture.json`. The
re-platform (`dashboard-v2`) binds a **proposed v2 snapshot contract** that extends
`snapshot()` with daemon live-state (running sessions, held, parked, daemon status),
captured to `dashboard-v2.capture.json`. Both captures are gitignored.

| surface | concept | status | implementation | file |
|---|---|---|---|---|
| dashboard | **inbox** (needs-you-first) + **stream**'s trust-ratchet bar/glow | `shipped` | `server.py` + `static/dashboard.html` — superseded by `dashboard-v2` | `dashboard-inbox.html` |
| dashboard-v2 | **console shell** + queue **Inbox** / live **Live** kanban / grid **Fleet** / **History** + shared drill-down | `locked` | re-platform → Svelte SPA + FastAPI, polling liveness, interactive controls (issue TBD) | `dashboard-v2-combined.html` |

Losing variants — round 1 (`dashboard`): mission-control, pipeline, stream.
Round 2 (`dashboard-v2`): queue, live, console, grid — each contributed its
strongest surface to the locked combined synthesis, then was deleted. Git history
holds them all if ever needed.
