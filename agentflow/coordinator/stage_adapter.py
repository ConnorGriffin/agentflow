"""The skeleton every logical stage adapter extends (ADR 0030).

ADR 0030 gives each logical stage a small adapter that prepares its source, observes the ended
provider family, verifies its own required outcome, and produces its native human handoff. Most
of that is identical from stage to stage: reuse the retained worktree before admission, rebuild
the observation from the attempt's durable artifacts, continue within the attempt budget behind a
bounded recovery envelope, and hand off with the kind ADR 0028's exhaustion table names for the
stage. That shared skeleton lives here once.

Each stage's own module keeps only what is genuinely its own — the outcome it proves, an owned
worktree with different preparation rules, a completion projection, or a recovery classification
that is not "continue within budget".
"""

from __future__ import annotations

from agentflow.coordinator.admission import native_handoff_proof
from agentflow.coordinator.providers import ProviderObserver
from agentflow.coordinator.recovery import durable_progress


class StageAdapter:
    """The parts of ADR 0030's adapter jobs that every stage shares.

    Collaborators are injected so a stage is exercised without a real worktree, GitHub, or
    provider: ``outcome_ready`` answers whether the stage's required outcome is durable,
    ``worktree_ready`` proves the branch/worktree the record already owns is present and checked
    out, ``observer`` reconstructs the provider observation from the attempt's durable artifacts,
    and ``handoff`` creates the stage-native human hold. Production defaults reuse the real
    provider observer, treat a record with no source as unprepared, and fall back to the same
    durable hold proof the coordinator itself would write.
    """

    #: What a fresh attempt is missing, named in the bounded envelope handed to it (issue #225).
    required_outcome = ""

    def __init__(self, *, outcome_ready=None, worktree_ready=None, observer=None,
                 handoff=None) -> None:
        self._outcome_ready = outcome_ready
        self._worktree_ready = worktree_ready or (lambda record: bool(record.source))
        self._observer = observer or ProviderObserver()
        self._handoff = handoff

    def prepare(self, record):
        """Reuse the retained branch and worktree before admission (ADR 0030). Answers whether the
        owned worktree is present and checked out; a miss consumes no permit and no attempt, and
        the record simply waits and retries — its claim, lineage, branch, worktree, and local
        changes are untouched, so an interrupted stage resumes exactly where it left off.

        Returns the collaborator's answer as-is, exactly as ``verify`` does: a typed
        :class:`~agentflow.coordinator.verification.Verification` names the check that refused and
        the live values behind it, so the coordinator can persist and publish why the record is
        still waiting; a legacy bool stays valid and simply carries no detail (#405)."""
        return self._worktree_ready(record)

    def observe(self, record):
        """Reconstruct the provider observation from the attempt's durable session artifacts.
        Extraction only — whether the stage is done is ``verify``'s call, never the provider's."""
        return self._observer.observe(record)

    def verify(self, record, obs):
        """Whether the stage's own required outcome is durable (ADR 0028 outcome-first).
        Independent of how the provider exited: a bad exit that still produced the outcome
        completes the stage, and a clean exit that did not produce it does not. Returns the
        collaborator's answer as-is — a typed :class:`~agentflow.coordinator.verification.
        Verification` names the exact failed conjunct; a legacy bool stays valid and simply
        carries no miss. Callers branch on truthiness either way."""
        return self._outcome_ready(record, obs)

    def recover(self, record, obs):
        """A worktree-owning stage carries its partial work forward across a continuation, so a
        continuation is genuinely new state rather than an identical replay. Continue within the
        attempt budget behind a bounded envelope that points the fresh session at the retained
        worktree instead of re-running the same prompt from scratch (issue #225)."""
        return durable_progress(record, self.required_outcome)

    def finalize_hold(self, record) -> str | None:
        """Create the stage-native human handoff and return its durable proof (ADR 0028's
        exhaustion table). Production wires the real park/hold and notifies once; tests may omit
        the collaborator and use the same proof the coordinator would write for that handoff."""
        if self._handoff is not None:
            return self._handoff(record)
        return native_handoff_proof(record)
