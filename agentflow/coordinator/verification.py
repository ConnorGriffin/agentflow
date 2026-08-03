"""Typed answers — a refusal names the exact check that said no, on either side of the provider.

A stage's ``outcome_ready`` collaborator answers whether the required durable outcome exists
(ADR 0028 outcome-first). Historically that answer was a bare bool, and every ``False`` was
indistinguishable: a verifier with eight conjuncts had eight silent ways to park a PR, and each
park read as a brand-new edge case that had to be re-diagnosed by hand from session transcripts
(the #346-class parks). A :class:`Verification` keeps the same truthiness contract — adapters
and the coordinator still branch on ``bool(result)`` — while carrying the first failed check's
stable id and the live values that failed it. The coordinator persists that miss on the record,
stamps it into the attempt's telemetry, names it in recovery envelopes and hold reasons, and the
park comment prints it, so a parked PR states which conjunct stopped it instead of "budget
exhausted".

The same type now answers the *preparation* question a stage's ``prepare`` asks before the
provider ever runs (#405). A preparation refusal costs no permit and no attempt, so it used to be
invisible: a review checkout refused every cycle for half an hour over one untracked scratch file
and the daemon could only say admission was stuck (#397/#399). The typed answer carries the same
check id and live detail, plus two preparation-only facts — its **disposition**.
:attr:`Verification.expected` marks ordinary contention (a live sibling still holding a checkout)
rather than something a human should chase: it is still published, it simply does not count
toward the repeat-breadcrumb cadence. :attr:`Verification.stall` marks the opposite extreme — a
refusal the fleet has locally *proved* it cannot clear itself, so waiting longer can only waste
wall time (#406). Only that disposition starts the coordinator's preparation-refusal clock, and
only a check standing beside the evidence may set it: a network failure, a busy checkout, or an
unclassified error stays undisposed and escalates to nobody, however long it lasts.

Legacy collaborators (and test fakes) that still return plain bools stay valid: they simply carry
no miss, and :func:`miss_summary` reads them as untyped.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How much of a corrupt durable payload a refusal detail may quote (see :func:`payload_preview`).
PREVIEW_LIMIT = 160


@dataclass(frozen=True)
class Verification:
    """One answer: truthy when the check passes, else the first check that refused.

    ``check`` is a stable kebab-case id of the conjunct that failed (``"fix-push"``,
    ``"branch-mismatch"``); ``detail`` is one sentence of live values — what was actually read
    from GitHub, git, or the payload — so the refusal is diagnosable without the transcript.
    ``expected`` marks a refusal the fleet is *supposed* to produce sometimes, so the repeat
    breadcrumb stays quiet for it. ``stall`` marks one the fleet has proved it cannot clear
    itself, so it is worth a human's attention once it persists (#406).
    """

    ok: bool
    check: str = ""
    detail: str = ""
    expected: bool = False
    stall: bool = False

    def __bool__(self) -> bool:
        return self.ok

    def summary(self) -> str:
        """The persisted one-line miss (``""`` when verified or untyped)."""
        if self.ok or not self.check:
            return ""
        return f"{self.check}: {self.detail}" if self.detail else self.check


VERIFIED = Verification(True)
#: The truthy preparation answer. Same value as :data:`VERIFIED`, named for the question it answers.
PREPARED = VERIFIED


def unverified(check: str, detail: str = "") -> Verification:
    """The falsy answer for one named failed conjunct."""
    return Verification(False, check, detail)


def unprepared(check: str, detail: str = "", *, expected: bool = False,
               stall: bool = False) -> Verification:
    """The falsy preparation answer for one named refusing check (#405).

    ``stall`` is the caller's assertion that it read local evidence proving no amount of
    retrying will clear this — only a check standing beside that evidence may set it (#406)."""
    return Verification(False, check, detail, expected, stall)


def miss_summary(result) -> str:
    """The miss of any result — typed or legacy bool (``""`` when it passed, or is untyped)."""
    return result.summary() if isinstance(result, Verification) else ""


def refusal_expected(result) -> bool:
    """Whether a refusal is ordinary contention rather than something to chase. Untyped and
    passing answers are never expected, so a legacy bool keeps the loud cadence it has today."""
    return isinstance(result, Verification) and not result.ok and result.expected


def refusal_stalls(result) -> bool:
    """Whether a refusal proved a human has to clear it (#406). Untyped and passing answers never
    do, so nothing escalates on an answer that stated no disposition at all."""
    return isinstance(result, Verification) and not result.ok and result.stall


def payload_preview(field: str, value) -> str:
    """Name a corrupt durable payload and quote a bounded, single-line, escaped prefix of it.

    ``input_ptr`` is external text a crash or a hand edit can leave arbitrarily long and
    multi-line. The refusal it produces travels into the durable record, the daemon log, and the
    published snapshot, so the payload itself is never copied: ``repr`` escapes every newline and
    the quote is capped at :data:`PREVIEW_LIMIT` characters with an explicit truncation marker.
    """
    quoted = repr(value)
    if len(quoted) > PREVIEW_LIMIT:
        quoted = f"{quoted[:PREVIEW_LIMIT]}… (truncated)"
    return f"{field} is not a readable payload: {quoted}"
