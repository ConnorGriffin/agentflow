"""The offline Intake replay harness (Wayfinder #2, #228).

Drives a fixed, pinned corpus of real issues through three Intake arms and blind-scores the
results under the spend-per-success contract (ADR 0040, #227):

- **Arm A — deep** (baseline): one deep attempt per issue, exactly today's policy.
- **Arm B — standard-first**: one standard attempt per issue, no escalation.
- **Arm C — standard-first + typed escalation**: deterministic direct-deep triggers plus a
  standard attempt that can step up to a *second* deep attempt handed bounded prior evidence.

The harness never spawns a provider and never touches live GitHub. It drives the durable
Intake contract (``intake_prompt`` → ``parse_intake``) against a pluggable
:class:`Responder`, so a run is byte-stable for a fixed corpus + responder. When a responder
has no recording for an attempt, the harness records an explicit **gap** and drops that issue
from the arm's scored set — silent truncation would read as full coverage (#228 trap), so
coverage is always the honest count, never assumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from agentflow.coordinator.telemetry import AttemptUsage
from agentflow.experiments.escalation import (direct_deep_triggers, escalation_prompt,
                                              evidence_from_result)
from agentflow.experiments.spend import CLAUDE, headroom_weight, spend_summary
from agentflow.intake import IntakeResult, IntakeRoute, intake_prompt, parse_intake

DEEP = "deep"
STANDARD = "standard"


class Arm(str, Enum):
    A_DEEP = "A-deep"
    B_STANDARD = "B-standard-first"
    C_ESCALATE = "C-standard-first-escalate"


@dataclass(frozen=True, slots=True)
class CorpusEntry:
    """One pinned corpus item: the issue snapshot plus the repository SHA it was grounded
    against, so a rerun is reproducible, plus the recorded reference outcome used for scoring
    and the strata tags that make the sample auditable."""

    repo: str
    repo_ref: str                      # pinned commit SHA — the grounding is byte-stable
    number: int
    title: str
    body: str
    labels: tuple[str, ...] = ()
    reference_route: str = ""          # the route actually applied to this issue (label truth)
    reference_complexity: str = ""     # the complexity dial actually applied, if any
    reference_effort: str = ""
    strata: tuple[str, ...] = ()       # e.g. ("route:ready", "complexity:deep")

    def as_issue(self) -> dict:
        return {"number": self.number, "title": self.title, "body": self.body,
                "labels": [{"name": n} for n in self.labels]}


@dataclass(frozen=True, slots=True)
class Corpus:
    entries: tuple[CorpusEntry, ...]
    notes: tuple[str, ...] = ()        # explicit provenance / sampling notes

    def strata_mix(self) -> dict[str, int]:
        """The recorded strata histogram — how many issues carry each stratum tag. This is the
        auditable record of the sample's route/complexity mix (#228 acceptance)."""
        counts: dict[str, int] = {}
        for entry in self.entries:
            for tag in entry.strata:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items()))


def load_corpus(path: str | Path) -> Corpus:
    """Load a pinned corpus JSON. Fail loudly on a malformed entry rather than silently
    dropping it — a shrunk corpus that reads as full coverage is the #228 trap."""
    data = json.loads(Path(path).read_text())
    entries = tuple(
        CorpusEntry(
            repo=e["repo"], repo_ref=e["repo_ref"], number=int(e["number"]),
            title=e.get("title", ""), body=e.get("body", "") or "",
            labels=tuple(e.get("labels", ())),
            reference_route=e.get("reference_route", ""),
            reference_complexity=e.get("reference_complexity", ""),
            reference_effort=e.get("reference_effort", ""),
            strata=tuple(e.get("strata", ())))
        for e in data["entries"])
    return Corpus(entries=entries, notes=tuple(data.get("notes", ())))


# --- Responder seam ---------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AttemptReply:
    """One recorded provider reply: the structured decision payload the CLI would have emitted
    (fed straight to ``parse_intake``) and its measured usage."""

    payload: str
    usage: AttemptUsage


class MissingResponse(Exception):
    """No recording exists for this (issue, tier, kind) — the harness records a gap."""


class Responder(Protocol):
    def respond(self, *, issue_number: int, tier: str, kind: str,
                prompt: str) -> AttemptReply: ...


@dataclass
class RecordedResponder:
    """A responder backed by recorded replies keyed by ``(issue_number, tier, kind)``. Any
    missing key raises :class:`MissingResponse` so the harness logs a gap rather than inventing
    a run. ``kind`` is one of ``deep`` / ``standard`` / ``escalate`` so a two-attempt arm-C
    issue keeps its two replies distinct."""

    replies: dict[tuple[int, str, str], AttemptReply]
    cache_hits: int = 0

    def respond(self, *, issue_number: int, tier: str, kind: str,
                prompt: str) -> AttemptReply:
        key = (issue_number, tier, kind)
        if key not in self.replies:
            raise MissingResponse(str(key))
        self.cache_hits += 1
        return self.replies[key]


# --- Blind scoring ----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BlindBrief:
    """What a scorer sees — deliberately arm-free. The arm label never reaches the scorer, so
    scoring stays blind under the #227 protocol."""

    issue_number: int
    route: str
    complexity: str
    effort: str
    body: str


@dataclass(frozen=True, slots=True)
class AxisScores:
    """The six #227 axes. Route/complexity/effort are scored deterministically against the
    corpus reference labels (real applied outcomes). The three judgment axes — verified
    evidence quality, acceptance-criteria completeness, downstream hold/review/revise outcome —
    are ``None`` from the deterministic scorer: they require a blind human/LLM scorer under the
    protocol, and reporting them as ``None`` keeps the harness from faking a judgment it did not
    make."""

    route_correct: float | None = None
    complexity_correct: float | None = None
    effort_correct: float | None = None
    verified_evidence: float | None = None
    acceptance_completeness: float | None = None
    downstream_outcome: float | None = None


Scorer = Callable[[BlindBrief, CorpusEntry], AxisScores]


def reference_scorer(brief: BlindBrief, entry: CorpusEntry) -> AxisScores:
    """A deterministic, blind scorer: it compares the brief's structured decision to the
    issue's recorded reference labels. It receives no arm label. The three judgment axes are
    left ``None`` for a downstream blind scorer to fill."""
    def match(got: str, want: str) -> float | None:
        return None if not want else (1.0 if got == want else 0.0)
    return AxisScores(
        route_correct=match(brief.route, entry.reference_route),
        complexity_correct=match(brief.complexity, entry.reference_complexity),
        effort_correct=match(brief.effort, entry.reference_effort))


# --- Per-issue run ----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AttemptRecord:
    tier: str
    kind: str
    usage: AttemptUsage
    reasons: tuple[str, ...] = ()      # escalation reasons attached to this attempt, if any


@dataclass(frozen=True, slots=True)
class IssueRun:
    number: int
    result: IntakeResult
    attempts: tuple[AttemptRecord, ...]
    direct_deep: bool
    escalated: bool
    headroom: float
    cost_usd: float | None
    score: AxisScores


@dataclass
class ArmRun:
    arm: Arm
    pool: str
    issues: list[IssueRun] = field(default_factory=list)
    gaps: list[tuple[int, str]] = field(default_factory=list)   # (issue_number, kind) unrun
    notes: list[str] = field(default_factory=list)

    def spend(self) -> dict:
        return spend_summary((i.headroom for i in self.issues),
                             (i.cost_usd for i in self.issues))

    def escalation_rate(self) -> float | None:
        if not self.issues:
            return None
        stepped = sum(1 for i in self.issues if i.escalated or i.direct_deep)
        return stepped / len(self.issues)


def _brief(number: int, result: IntakeResult) -> BlindBrief:
    return BlindBrief(
        issue_number=number, route=result.route.value,
        complexity=result.complexity.value if result.complexity else "",
        effort=result.effort.value if result.effort else "",
        body=result.body)


def _attempt(responder: Responder, *, number: int, tier: str, kind: str,
             prompt: str, reasons: tuple[str, ...] = ()) -> tuple[IntakeResult, AttemptRecord]:
    reply = responder.respond(issue_number=number, tier=tier, kind=kind, prompt=prompt)
    result = parse_intake(reply.payload)
    return result, AttemptRecord(tier=tier, kind=kind, usage=reply.usage, reasons=reasons)


def _run_issue_A(entry: CorpusEntry, responder: Responder,
                 base_prompt: str) -> tuple[IntakeResult, list[AttemptRecord], bool, bool]:
    result, rec = _attempt(responder, number=entry.number, tier=DEEP, kind=DEEP,
                           prompt=base_prompt)
    return result, [rec], False, False


def _run_issue_B(entry: CorpusEntry, responder: Responder,
                 base_prompt: str) -> tuple[IntakeResult, list[AttemptRecord], bool, bool]:
    result, rec = _attempt(responder, number=entry.number, tier=STANDARD, kind=STANDARD,
                           prompt=base_prompt)
    return result, [rec], False, False


def _run_issue_C(entry: CorpusEntry, responder: Responder,
                 base_prompt: str) -> tuple[IntakeResult, list[AttemptRecord], bool, bool]:
    triggers = direct_deep_triggers(entry.as_issue())
    if triggers:
        # Direct-deep: skip the standard attempt entirely, spend a single deep attempt.
        reasons = tuple(r.value for r in triggers)
        result, rec = _attempt(responder, number=entry.number, tier=DEEP, kind=DEEP,
                               prompt=base_prompt, reasons=reasons)
        return result, [rec], True, False
    std_result, std_rec = _attempt(responder, number=entry.number, tier=STANDARD,
                                    kind=STANDARD, prompt=base_prompt)
    if std_result.route is not IntakeRoute.ESCALATE:
        return std_result, [std_rec], False, False
    # Step up: hand the deep attempt bounded prior evidence — never the original prompt blind.
    evidence = evidence_from_result(std_result)
    deep_prompt = escalation_prompt(base_prompt, evidence)
    reasons = tuple(r.value for r in std_result.escalation_reasons)
    deep_result, deep_rec = _attempt(responder, number=entry.number, tier=DEEP,
                                     kind="escalate", prompt=deep_prompt, reasons=reasons)
    return deep_result, [std_rec, deep_rec], False, True


_RUNNERS = {Arm.A_DEEP: _run_issue_A, Arm.B_STANDARD: _run_issue_B, Arm.C_ESCALATE: _run_issue_C}


def run_arm(arm: Arm, corpus: Corpus, responder: Responder, *,
            scorer: Scorer = reference_scorer, pool: str = CLAUDE) -> ArmRun:
    """Drive every corpus issue through one arm, accumulating spend (arm C sums both attempts
    when it escalates) and a blind score. An issue whose responder has no recording is dropped
    and logged as a gap — never counted as covered."""
    run = ArmRun(arm=arm, pool=pool)
    runner = _RUNNERS[arm]
    for entry in corpus.entries:
        base_prompt = intake_prompt(entry.repo, entry.as_issue())
        try:
            result, attempts, direct_deep, escalated = runner(entry, responder, base_prompt)
        except MissingResponse as gap:
            run.gaps.append((entry.number, str(gap)))
            continue
        headroom = sum(headroom_weight(a.usage, pool) for a in attempts)
        costs = [a.usage.cost_usd for a in attempts]
        cost = sum(c for c in costs if c is not None) if any(c is not None for c in costs) else None
        run.issues.append(IssueRun(
            number=entry.number, result=result, attempts=tuple(attempts),
            direct_deep=direct_deep, escalated=escalated, headroom=headroom, cost_usd=cost,
            score=scorer(_brief(entry.number, result), entry)))
    if run.gaps:
        run.notes.append(
            f"{len(run.gaps)} of {len(corpus.entries)} issues had no recorded reply for this "
            f"arm and were dropped from the scored set (explicit gaps, not full coverage)")
    return run


def run_experiment(corpus: Corpus, responder: Responder, *,
                   scorer: Scorer = reference_scorer, pool: str = CLAUDE) -> dict[Arm, ArmRun]:
    """Run all three arms over the identical corpus from the same pinned snapshots."""
    return {arm: run_arm(arm, corpus, responder, scorer=scorer, pool=pool) for arm in Arm}
