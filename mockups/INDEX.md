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
| dashboard | **inbox** (needs-you-first) + **stream**'s trust-ratchet bar/glow | `retired` | was `server.py` + `static/dashboard.html`; superseded by `dashboard-v2`, deleted (ADR 0026) | `dashboard-inbox.html` |
| dashboard-v2 | **console shell** + queue **Inbox** / live **Live** kanban / grid **Fleet** / **History** + shared drill-down | `locked` | re-platform → Svelte SPA + FastAPI, polling liveness, interactive controls (issue TBD) | `dashboard-v2-combined.html` |
| workspace | **shelf** (efforts in 3 visual weights) + **anchored Ask composer** + **queue** prioritization | `locked` | project-workspace surface (Wayfinder #128 → PRD; build issue TBD) | `workspace-combined.html` |

**`workspace` grounding is its own language, not the console's.** The operator's
direction: this surface deliberately departs from the terminal-native console
(mono/GitHub-dark). Light **and** dark, OKLCH tokens, daylight-white bg /
instrument-teal, **copper reserved exclusively for "awaiting your decision"**;
Newsreader serif (thinking voice) + Inter sans (chrome). Data shape is the ADR
0033 daemon Project-workspace projection, captured **synthetically** to
`workspace.capture.json` (no real endpoint exists yet; gitignored, regenerate to
render). Honors ADR 0034 (coordinated-turn model) and the #128 crafting: three
visual weights, hash-routed views (Back + direct links), WCAG AA both themes.

Losing variants — round 1 (`dashboard`): mission-control, pipeline, stream.
Round 2 (`dashboard-v2`): queue, live, console, grid — each contributed its
strongest surface to the locked combined synthesis, then was deleted.
`workspace` round 1 (rejected wholesale — terminal-styled): loop, ledger, split.
Round 2 (new light/serif language): shelf, anchor, queue — synthesized into the
locked `workspace-combined`, then deleted. Git history holds them all if needed.
