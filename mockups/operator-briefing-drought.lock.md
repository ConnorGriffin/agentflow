# Lock manifest — operator-briefing-drought (issue #430)

Locked: 2026-08-04 by the /agentflow pickup session (headless round)
Mocks: `operator-briefing-drought.html`   Supersedes: — (local addition; extends
`operator-surface-finalist.lock.md`, which continues to bind in full except terms 12–13
as amended below)

## Precedence

The base briefing lock (`operator-surface-finalist.lock.md`) wins for everything this
manifest does not state; this manifest wins only for the stage-drought rows it adds. For
component styling the mock wins where explicit; the shipped `Briefing.svelte` scoped
tokens win for anything it doesn't state.

## Terms

| # | Term | Kind | Evidence expected |
|---|------|------|-------------------|
| 1 | A stage with zero stage outcomes across its last 10 finished attempts renders exactly one Attention row of kind `stage drought`; the window is 10 and lives in one named constant | gate | assertion |
| 2 | Drought rows rank first in Attention, before all five base conditions (amends base term 13) | gate | assertion |
| 3 | Row title is the stage's own domain name (e.g. `pre-publish attack`); detail reads `0 of its last 10 finished attempts <produced its stage outcome, named concretely> · <n> sessions spent` — the outcome clause names the artifact (`published a hardened brief`, `recorded a review verdict`), never a generic "output" | gate | assertion + text diff |
| 4 | The third column is plain non-link text `What to check: <where the missing output pools>`, unique per stage — never an `Open in GitHub ↗` action, never a repeated string across two drought rows | gate | assertion |
| 5 | A stage with fewer than 10 finished attempts, or ≥1 stage outcome in its window, renders nothing: Attention, its count, and the tab badge are byte-identical to the shipped path (under-window and all-healthy fixtures) | gate | assertion on rendered DOM equality |
| 6 | Two simultaneous droughts render as two stacked rows in the same ruled-row treatment — no coalescing, no banner (multi-drought fixture) | gate | assertion |
| 7 | Drought rows inherit the base row grid (104px kind column, ruled 1px `--line` separators, `--warn` kind label) with no new container, card, or accent | eye | paired render vs base |
| 8 | Drought rows count toward the Attention section count and the fleet-wide 25-row bound exactly like any other row (base term 17 unamended) | gate | assertion |
| 9 | The signal never appears in Fleet health, as a banner, or as color-only state — words first, everywhere | gate | assertion |

## Fixture obligations

- **drought** — one stage (pre-publish attack) at 0 outcomes across its last 10 finished
  attempts, 37 sessions spent, alongside otherwise-healthy repositories.
- **under-window** — a stage with fewer than 10 finished attempts must render nothing
  (the quiet-fleet case is provably not a false alarm).
- **all-healthy** — the addition absent entirely; shipped reading path byte-identical.
- **multi-drought** — two stages in drought at once (pre-publish attack + a review stage
  recording no verdicts), so the layout is proven for more than one row.
- A fixture that cannot show a term cannot prove it; the four states above are the
  minimum evidence set for terms 1–6.

## Verbatim strings

- `stage drought`
- `pre-publish attack`
- `0 of its last 10 finished attempts published a hardened brief · 37 sessions spent`
- `0 of its last 10 finished attempts recorded a review verdict · 41 sessions spent`
- `What to check: held drafts and ready-for-agent holdbacks`
- `What to check: open PRs waiting on a verdict`
