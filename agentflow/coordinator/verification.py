"""Typed verification results — an unverified outcome names the exact check that failed.

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

Legacy verifiers (and test fakes) that still return plain bools stay valid: they simply carry no
miss, and :func:`miss_summary` reads them as untyped.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verification:
    """One verify answer: truthy when the outcome is durable, else the first failed check.

    ``check`` is a stable kebab-case id of the conjunct that failed (``"fix-push"``,
    ``"targeted-reply"``); ``detail`` is one sentence of live values — what was actually read
    from GitHub, git, or the payload — so the miss is diagnosable without the transcript.
    """

    ok: bool
    check: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok

    def summary(self) -> str:
        """The persisted one-line miss (``""`` when verified or untyped)."""
        if self.ok or not self.check:
            return ""
        return f"{self.check}: {self.detail}" if self.detail else self.check


VERIFIED = Verification(True)


def unverified(check: str, detail: str = "") -> Verification:
    """The falsy answer for one named failed conjunct."""
    return Verification(False, check, detail)


def miss_summary(result) -> str:
    """The miss of any verify result — typed or legacy bool (``""`` when verified/untyped)."""
    return result.summary() if isinstance(result, Verification) else ""
