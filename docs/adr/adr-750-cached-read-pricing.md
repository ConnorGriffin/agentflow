# ADR 750 — Price cached reads from a sourced rate card

Status: Accepted

Date: 2026-08-17

## Context

ADR-era decision #516 put estimated dollars on Codex work for the first time, priced from the
routing table's rate card. The card carried a fresh-input rate and an output rate and nothing
else, so cached reads were left out of every estimate. #531 considered pricing them, and
deliberately declined: no cached rate with verified provenance existed, and inventing one was
out of bounds. It shipped a row-level disclosure of the omission instead.

The omission is not small. Reading the fleet's own telemetry on 2026-08-17, cached reads are
528M of Codex's 560M tokens — 94% of its input volume — and 2.0B of Opus's 2.0B. Pricing the
Codex share alone adds about $240 against the $191 the estimator produced from everything else,
so any Codex-versus-Claude comparison drawn from the estimate was biased toward Codex by
roughly a factor of two. That is the same direction of error #516 set out to remove.

The blocker was provenance, not arithmetic. Both vendors publish cached-read rates:
Anthropic prices a cache hit at 0.1x base input (Opus 5 $0.50/MTok against $5 base), and
OpenAI publishes a cached-input rate per model (gpt-5.6-sol $0.50/MTok short context). Checking
those pages also showed the card's fresh rates had drifted: Sonnet is $2/$10 rather than the
recorded $3/$15, Terra is $2/$12 rather than $2.50/$15, and Luna is $0.20/$1.20 rather than
$1/$6.

## Decision

The rate card carries an optional `cached_input` rate per model, and the estimator charges
cached reads at it. `cached_input` is optional on purpose: a model the card gives no cached
rate keeps #531's behavior exactly — its cached reads stay out of the estimate and the spend
report discloses the unpriced quantity on the row — so the fail-closed path survives for any
model added before its price is known. A caller asks `prices_cached_reads(model)` to decide
whether to disclose.

Prices are stamped with the date they were verified and the vendor pages they came from.
OpenAI tiers its prices by context length; agentflow's telemetry records no context length, so
the card carries the **short-context** tier and the provenance says so. An attempt that ran in
the long-context tier is therefore understated (2x input and cached-read, 1.5x output). That is
a known, disclosed floor, consistent with this repo's rule that missingness stays visible
rather than being coerced.

Provider-billed dollars are untouched by all of this. Claude's harness reports a real total that
already includes its cache reads; the rate card is only ever consulted for an attempt with no
billed figure, which today means Codex.

## Alternatives

Charging cached reads at the fresh-input rate was rejected: it overstates by 10x and is a guess,
which is what #531 refused. Storing a per-attempt context tier to pick the right tier was
rejected as premature — no telemetry carries it, and the short-context floor is disclosed.
Repricing historical stored telemetry was rejected: entries are read, never rewritten.

## Consequences

Estimated Codex spend more than doubles, from $191.05 to $428.89 across the fleet's telemetry on
2026-08-17 — 13.3% of total fleet spend rather than 6.4%, which is the correct direction: it was
never that cheap. Sixteen attempts stay unpriceable for want of any token fact. The `unpriced cached input`
note on spend rows disappears for every model now priced, and remains for any that is not.
The learning report is unaffected — it reads provider-billed cost only and still reports Codex
attempts as cost-unknown; wiring estimates into it is separate work against the #629 contract.
Rates need re-verifying when either vendor moves; `price_verified_date` is the stamp to check.
