# agentflow console (dashboard v2)

The Svelte SPA half of the operator dashboard re-platform ([ADR 0023](../../docs/adr/0023-dashboard-replatform-control-plane.md)).
This slice ships the console shell + the **Inbox** tab bound to the current
`/api/snapshot` contract; Live / Fleet / History are stubs.

Locked visual spec: [`mockups/dashboard-v2-combined.html`](../../mockups/dashboard-v2-combined.html)
(any UI change goes through `/ui-craft lock` first — charter gate).

## Develop

```sh
npm ci                 # install (Node 18+)
npm run dev            # Vite dev server; proxies /api to http://127.0.0.1:8788
npm test               # vitest — Inbox derivation (ordering + exclusion)
```

Run a snapshot server for `dev` to poll: `uv run agentflow-web` (from the repo root).

## Build (what FastAPI serves)

```sh
npm run build          # → dist/  (agentflow/webapp.py serves this)
```

Then from the repo root:

```sh
uv run agentflow-web   # http://localhost:8788
```

`dist/` is committed so the server runs from a clean checkout without Node; rebuild
it whenever `src/` changes.
