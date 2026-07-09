# Engineering charter

Standards every app built in the agentflow flow must meet. Read by **both** tools
(Claude via `CLAUDE.md`, Codex via `AGENTS.md` — the *same canonical bytes*) and
**enforced at cross-review**: a violation below is a blocking finding
([ADR 0004](docs/adr/0004-auto-merge-gate.md)).

> Seed — refine it. This is the bar, not holy writ.

## Architecture — deep modules

- Every module's **interface is far simpler than its implementation.** A module whose
  interface is nearly as complex as its body is *shallow* — deepen it or delete it.
- Apply the **deletion test:** if removing a module just moves complexity around
  rather than concentrating it, it shouldn't exist.
- **The interface is the test surface.** Don't extract pure functions "for
  testability" when the real bugs live in how they're called — preserve **locality**.
- Use the vocabulary exactly — module / interface / depth / seam / adapter / leverage
  / locality (`/codebase-design`). **One adapter = a hypothetical seam; two = a real
  one** — don't build the seam before the second caller exists.

## UI — never invented at build time

- Any user-facing surface goes through **`/ui-mockups` to a *locked* visual spec**
  before it is implemented. No ad-hoc UI.

## Testing — through the interface

- New behavior ships with a test that **exercises it through the public interface**,
  and — where it fits — one that failed first for the right reason.
- A green suite that doesn't exercise the behavior is not coverage.

## Maintainability

- **Match the surrounding code** — idiom, naming, comment density.
- **No dead code, no speculative abstraction.** Build the seam when the second caller
  is real, not before.
- Domain terms come from **`CONTEXT.md`**; record load-bearing, hard-to-reverse
  decisions as **ADRs** — and don't re-litigate settled ones.
