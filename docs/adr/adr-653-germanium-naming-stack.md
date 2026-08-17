# ADR 653 — Germanium names the native product and Germanium Core names its engine

- Status: Accepted
- Date: 2026-08-14
- Ticket: [#653](https://github.com/ConnorGriffin/agentflow/issues/653)

## Context

The headless workflow engine is gaining a future native operator cockpit. `AgentFlow` is
crowded as a public software name, while the retired web console must not define the new
product's identity or shape. The native product needs a durable name before its operator
model, client boundary, visual language, packaging, and migration work can be specified.

The preferred name must also coexist with package registries. The unqualified `germanium`
name is already occupied by a dormant web-testing project on PyPI and npm, even though that
project does not present meaningful product confusion for a native agent operator app.

## Decision

The native operator product and app are named **Germanium**. Its headless engine is named
**Germanium Core**.

The naming stack is:

- Product and native app: `Germanium`
- Headless engine: `Germanium Core`
- Python distribution: `germanium-core`
- Python import package: `germanium_core`
- User-facing executable: `germanium`

The distribution and import names deliberately avoid the occupied unqualified package
namespace. The executable remains the product name because installed commands are independent
of Python distribution names.

This ruling settles identity; it does not authorize an immediate mechanical rename of the
existing repository, modules, configuration, or durable data. Migration scope and compatibility
must be planned after the headless-readiness and client-boundary decisions are settled.

No domain purchase is part of this decision.

## Alternatives

- Keep `AgentFlow` as the public product name — rejected because the name is crowded and carries
  the history of the retired web console rather than the intended native instrument.
- Use `Transist`, `Varistor`, `Chassis`, or `Aftertouch` — retained as explored alternatives but
  rejected in favor of Germanium's sound, visual identity, and authentic electronic-component
  and guitar-pedal associations.
- Publish the Python distribution as `germanium` — rejected because an existing web-testing
  package already owns that namespace on PyPI and npm.

## Consequences

Future product, UI, packaging, and migration decisions use **Germanium** and **Germanium Core**
as settled domain terms. Build work must preserve compatibility intentionally instead of treating
the rename as a global text replacement. Domain registration remains a separate reversible
commercial action, and the Wayfinder map remains the authority for when implementation becomes
eligible.
