# ADR 649 — Owned vendored skills may be repaired to their pin

Status: Accepted
Date: 2026-08-16

## Context

Enrollment materializes vendored skills into provider-local directories
(`.agents/skills/<name>`, `.claude/skills/<name>`). A path and its current digest alone cannot
establish whether AgentFlow or an operator created the directory, and convergence needs a safe
way to recover an AgentFlow materialization that has drifted from its pin.

An earlier version of this decision bound marker validity to both the destination identity *and*
byte-equality with the pinned tree at read time. That made the marker circular: valid only when
the tree already matched the pin, i.e. only when status was already `ok`. `drifted AND owned` was
then unsatisfiable — repair could never fire on the one state it exists to handle. Binding validity
to the *current* manifest digest compounded this: any pin bump invalidated every marker fleet-wide,
so the marker offered no continuity across a skill release.

## Decision

Provenance means "AgentFlow materialized this destination from pin X," recorded once, at
materialization time — not "this destination still equals pin X." Each materialized
provider-local destination has a separate schema-1 marker under `.agentflow/skill-ownership`. The
marker names the exact destination and records the pinned release identity that was materialized
(the `connor_skills`/`methodology_skills` commit, or a capability's pinned `version`, from
`capabilities.toml`) as provenance data — it is never read back as a validity precondition.

A marker is valid when its payload is well-formed, its recorded destination matches the path being
checked, and the destination is currently a regular directory (not a symlink). Later drift of the
tree does not invalidate it — drift is exactly the state repair exists to fix — and a pin bump does
not either, so a marker written under an old release still proves ownership after the fleet's pin
moves on.

During convergence, AgentFlow may replace only a regular destination that is `drifted` and still
has a valid marker. It builds the pinned replacement beside the destination under the repository's
existing `_capability_repair_lock` (the same lock `repair_capability_refusal` uses to serialize
concurrent coordinators on one root — taking it here closes a race where the stale-`.<name>-*`
sweep could `rmtree` a concurrent converge's in-flight temp directory), revalidates status and
ownership immediately before the rename, then swaps the complete tree and refreshes the marker
with the pin just installed. Unowned, incompatible, and symlinked content is reported and never
touched.

Enrollment rollback journals only `.agentflow/skill-ownership`; it neither snapshots nor restores
the daemon's broader runtime root. Rollback that removes a materialized destination also removes
its marker. Rollback removes the `.agentflow` directory itself only when this enrollment run
observed it absent at the start — a pre-existing `.agentflow` may belong to a concurrent repair
that is mid-`mkdir`+open of its own lock file, and an unconditional `rmdir` on "now empty" would
race that.

## Consequences

Residual risk, accepted: if an operator overwrites a marker-owned directory in place — same path,
still a regular directory — it is indistinguishable from drift and convergence will repair it back
to the pin. This is bounded by two things: rollback's marker cleanup keeps orphaned markers from
outliving the destinations they name, and the refusal to touch symlinked or `incompatible`
destinations means the residual risk is scoped to "operator edits files in place," not "operator
replaces the destination with something structurally different."

Durable daemon state remains outside enrollment rollback. The marker is outside the digest-checked
skill tree, so it does not affect `skill_destination_status`. A pin bump no longer invalidates
existing markers — that is the point: it is what lets repair recover every enrolled repo's
destination to the new pin instead of losing provenance on every release.
