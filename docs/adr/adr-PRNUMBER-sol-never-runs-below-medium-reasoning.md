# ADR PRNUMBER — Sol never runs below medium reasoning

Status: Accepted

Date: 2026-08-17

## Context

ADR 498 pins the Build/Revise *session lead* to `low` reasoning: the parent's job is to route and
supervise delegated work, and the work-effort dial lands on the worker rung inside the rendered
lead brief rather than on the parent's own provider flag. That pin was reasoned about and measured
on Fable, the Claude session lead.

Build and Revise fall back to the Codex pool with `sol` as the parent, and Sol inherits the same
`low` rung. The operator's judgment from watching those sessions is that Sol underperforms at the
low rung — low enough that the parent stops being a competent supervisor of its own delegated work.

Nothing about ADR 498's reasoning transfers automatically: it is a decision about what a session
lead needs, evaluated on one model, not a fleet-wide claim that every lead is adequate at `low`.

## Decision

Sol never launches below `medium` reasoning effort.

The floor is enforced in one place: `_lead_reasoning()` in `agentflow/coordinator/profiles.py`,
which `profile_for_facts()` calls to resolve the Build/Revise reasoning rung. That module is the
single point every session's rung is resolved through — the durable `LaunchConfigV1` artifact
routing freezes at `select_route()`, the direct `profile_for(record)` path both provider adapters
fall back to when there is no admitted launch, and the coordinator's telemetry row all read the
rung from it. Applying the floor at the launch flags instead would mean two provider adapters
(`ClaudeRunner`, `CodexRunner`) each carrying their own copy, and would leave the frozen launch
artifact recording a rung the session did not run at.

`profile_for_facts()` therefore takes the session lead's `model`. Both of its callers already hold
that identity, so nothing new is threaded through the pipeline. `profile_for()` is also read for
its ceilings by the pre-admission `Submission`, which carries a pool but no model yet; that read
has no lead identity and never launches anything, so it resolves to the ADR 498 rung.

The floor keys off Sol by name and accepts either spelling of it — the internal routing name `sol`
that records are stamped with, and the provider CLI id an admitted launch carries. Both are named
in `profiles.py` as a module-level constant rather than read from the routing table: `routing`
already imports the profile module, and the repo's import-cycle gate tolerates exactly one ring, so
reaching back into routing — even through a deferred import — is the defect that gate exists to
catch.

The routing table stays the source of truth for the pair, so the constant is a copy, and the copy
is kept honest by a test rather than by an import. `test_capability_routing.py` — which imports
`routing` legitimately — asserts the constant equals `{"sol", routing.cli_identifier("codex",
"sol")}`. Renaming Sol's CLI id in the table without a matching edit in `profiles.py` fails that
test by name, which is the drift signal; the profile tests independently pin that both spellings
resolve to `medium`.

## What this does not change

- **ADR 498's Fable pin stands.** A Claude-pool Build or Revise parent still launches at `low`.
- **Terra, Luna, and every other model are untouched.** The floor is keyed on Sol specifically,
  not on the Codex provider, so a Codex session that is not Sol-led resolves exactly as before.
- **The work-effort dial is unchanged.** Complexity and effort still size ceilings and leases, and
  still map to the worker rung in the lead brief; this decision moves only the parent's own
  provider reasoning flag.
- **Non-lead stages keep the provider default.** Intake, Research, Attack, Review, Respond,
  Converse, and Mockup set no reasoning flag at all, before or after this change.

## Alternatives

- **Drop ADR 498's low pin for every lead.** Rejected: it would re-litigate a settled decision on
  evidence that only concerns Sol, and would move Fable's rung for no observed reason.
- **Floor the whole Codex pool at `medium`.** Rejected: Terra and Luna are worker rungs whose
  reasoning is set by the worker launcher, and a pool-wide floor would claim evidence about models
  the operator has not judged.
- **Apply the floor in the runner's launch-flag construction.** Rejected: two provider adapters
  build argv, so the floor would be duplicated, and the frozen `LaunchConfigV1` would disagree with
  what actually launched.

## Consequences

A Sol-led Build or Revise launches with `model_reasoning_effort=medium`, and its frozen launch
artifact and telemetry row record `medium`. Sol-parent sessions will spend more reasoning tokens
per attempt than they did at `low`; the ceilings in `profiles.py` are set from recorded tool-call
and wall distributions and are unchanged, so if the higher rung shifts Sol's recorded pace, the
ceiling tables are ratcheted on their own rule rather than by relaxing this floor.
