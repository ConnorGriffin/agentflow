# Mockups library

One line per surface. `locked` = binding visual spec for its implementation;
`shipped` = now in the app (harvest from the shipping frontend, never from here —
archived markup is stale by definition).

Grounding for this repo: theme tokens are lifted from `agentflow/static/dashboard.html`
(`:root` block — GitHub-dark-derived). Real data shape is
`agentflow.dashboard_data.snapshot()`, captured to `dashboard.capture.json` (gitignored).

| surface | concept | status | implementation | file |
|---|---|---|---|---|
| dashboard | **inbox** (needs-you-first) + **stream**'s trust-ratchet bar/glow | `locked` | lift → `server.py` + `static/dashboard.html` (repoint `fetch` at `/api/snapshot`) | `dashboard-inbox.html` |

Losing variants (mission-control, pipeline, stream) were deleted after the lock —
git history holds them if ever needed.
