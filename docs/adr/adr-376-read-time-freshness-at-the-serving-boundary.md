# ADR 376 — The reader ages the projection: read-time freshness at the serving boundary

- Status: Accepted
- Date: 2026-08-04
- Amends: [0026](0026-daemon-owned-snapshot.md) (the endpoint is no longer the file
  verbatim), extends [0036](0036-bounded-repository-map-projection.md) (the two-heartbeat
  rule now applies on both sides of the file)

## Context

[ADR 0036](0036-bounded-repository-map-projection.md) stamps each repository's Decision Map
read as `fresh`, `stale` or `unavailable` at publish time. That stamp is correct when it is
written and never again: it is a fact about the moment of publication, sitting in a file that
outlives it.

So the moment the daemon stops publishing — crashed, paused, mid-deploy, or simply not yet
restarted onto merged code ([ADR 0051](0051-deploying-the-running-daemon.md)) — a previously
fresh body keeps being served, and the console keeps presenting a *verified decision frontier*
computed from it. Nothing decays. An operator can read a two-hour-old frontier as current and
act on it, which is the one failure this whole surface exists to prevent.

Three smaller things followed from the same root. The browser held its own copy of the
freshness rule, so the publish side and the read side could word the same state differently —
and had already drifted. A body from before this schema, or one perturbed on disk, was accepted
and rendered as an ordinary briefing. And a projection carrying no repositories at all was
drawn as an honest empty fleet, though the configuration rejects a fleet with no repositories,
so no daemon can publish one.

## Decision

**The serving boundary re-ages the body it was handed, and stamps how the briefing may
describe itself.** `GET /api/snapshot` is no longer the file verbatim. It remains file-only —
no GitHub read, no reconstruction, no live fallback — but before answering it recomputes two
things from the timestamps already in the body and the reader's own clock: each repository's
map-read status, and the briefing's display state, banner sentence, label prefix and verified
timestamp. Every other fact is passed through untouched.

**Aging is downgrade-only.** The published status is a ceiling. A read that *failed* keeps its
last verified timestamp on purpose, so age alone would promote a known failure back to fresh;
only `fresh` may move, and only to `stale`. A component that was never verified stays
unavailable. A timestamp that cannot be dated is infinitely old, never an error.

**The freshness window travels in the body.** Publication stamps the daemon's own heartbeat,
and the reader takes two of those. The daemon and the console are separate long-lived launch
agents installed together from one runtime checkout; each loads its code at start, and a deploy
defers the daemon's restart while fleet work is live, so for a while the two run different code.
A window inferred inside the server process would be a number the published body never agreed to.

**The rule has one home, and the server owns the words.** One stdlib-only module holds the
publish stamp, the read-time aging and the rollup; the daemon and the console server are both
callers. The server stamps the state, the banner sentence and the opening words of the masthead
label; the browser appends the age in the relative-time vocabulary it already ships. No relative
time formatter exists on the server, and no freshness rule exists in the browser.

**Everything it cannot vouch for fails closed to the same shape.** An absent body, an
unrecognised schema version, a body published before the window travelled in it, a
zero-repository body, and a body whose repositories are the wrong shape all serve their usable
v1 fields unchanged and replace the schema-v2 briefing with its unavailable shape — no map, no
frontier, no queue. The daemon's own carry-forward is deliberately narrower: it rejects only an
absent or unrecognised schema version, so the first publish after an upgrade still preserves
each repository's last verified read rather than republishing it as never-loaded.

## Consequences

- An overlong full pass now reads as honestly stale, with no damping. That is intended: a
  projection the daemon did not refresh inside two heartbeats *is* stale, and saying so costs
  an operator one glance at GitHub, where saying otherwise costs them a wrong decision.
- Server-new / daemon-old skew makes only the schema-v2 briefing unavailable. The Inbox, Live,
  Fleet and History tabs and the header's own freshness stamp keep working throughout, which is
  what makes deferring the daemon restart safe.
- The endpoint's response is no longer byte-identical to the state file, so a test that pins
  "the file, verbatim" pins the aged body instead.
- Rendering the briefing now depends on the clock the server answered with, so every fixture
  and every screenshot fixes both the publish stamp and the reading clock.
- A projection with no repositories is now unavailable rather than empty, which re-settles the
  locked `empty` capture (`mockups/operator-surface-finalist.lock.md`, term 6).
