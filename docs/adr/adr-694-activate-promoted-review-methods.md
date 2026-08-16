# ADR 694 — Activate promoted Review methods from the verified policy chain

Status: Accepted

Date: 2026-08-16

## Context

The Evaluation authority bootstrap occupies `fleet-policy/0-to-1`, while a mined Review lesson
was also nominated and approved as `0-to-1`. Even when promoted in isolation, that method receipt
was absent from the resolver's sealed Evaluation-only policy, so production Review could neither
consume nor attribute it.

## Decision

The Evaluation bootstrap is fleet-policy version 1. Every later promotion in that scope allocates
the immediate successor of the current verified version: the first learned method is `1-to-2`, the
next promotion is `2-to-3`, and so on. Promotion continues to verify the exact candidate digest,
authority revision, approval, scope, declared prior, and current durable winner; it additionally
rejects skipped versions.

`PromotionReceiptReader` exposes a query-only successor-chain read anchored at the pinned
Evaluation receipt. It revalidates that every verified fleet receipt is one consecutive transition
before returning any successors. `EffectivePolicyResolver` remains sealed around the pinned
Evaluation policy and folds that verified chain at briefing time. The global policy version follows
the chain's newest receipt, while the active advisory set keeps only the newest recognized method
per owning stage. A stage briefing cites the Evaluation receipt plus its active method receipt and
uses the newest version and scope that apply to that stage; unrelated stages therefore retain their
existing briefing identity. Repository overlays still only restrict the current global policy.

The resolver gains no promotion, Evidence-write, GitHub, or persistence authority. Production's
existing query-only receipt reader is the activation path, and Store continues to commit the exact
consumed method receipt and revision atomically with Review admission.

The amended effective-policy contract SHA-256 is
`f87266dddb953ee684958d8acef2f65b0aaa22cb812199adcd8d4cf912cbb01f`.

## Alternatives

- Give the bootstrap and learned methods independent `0-to-1` streams: rejected because the scope
  would no longer name one authoritative fleet-policy chain.
- Replace or renumber the bootstrap receipt: rejected because its immutable authority and deployed
  receipt are already pinned production history.
- Maintain a second checked-in or caller-injected receipt list: rejected because it recreates the
  inert test workaround and introduces another policy publication authority.

## Consequences

Bootstrap and subsequent learned methods coexist in one canonical Evidence store. A human-approved
Review lesson becomes active without daemon writes or a restart-specific mutation path, Review
receives the deployed method only when its locator and digest match, and admission attribution names
the same verified promotion receipt. A missing, malformed, forked, duplicated, or gapped durable
chain fails closed before a briefing is ready.
