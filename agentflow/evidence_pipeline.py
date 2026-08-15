"""Typed, content-free Evidence producers for pipeline artifacts.

This adapter retains opaque GitHub and pipeline identities only.  It never stores
issue prose, prompts, provider messages, or reconstructed review text.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable, Protocol
import re

from agentflow.evidence import (AuthorityPointer, EvidenceEnvelopeV2, EvidenceLink,
                                EvidenceRecord, EvidenceStore, FailureFacts, LessonCandidate, ProducerEvent,
                                ProducerFacts, SubjectRevision)

_UPSTREAM_CONTRACTS = frozenset({"build-tdd", "plan-review", "code-review",
                                 "wayfinder-coordinated-slicing"})
_REVIEW_ACTIONS = {"fix": "fix_before_completion", **{
    action: action for action in ("ask_maintainer", "discard_preference",
                                  "fix_before_completion", "necessary_follow_up")}}


def _digest(*parts: str) -> str:
    return sha256("\0".join(parts).encode()).hexdigest()


def _id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{_digest(*parts)[:32]}"


def _valid_digest(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{64}", value))


@dataclass(frozen=True)
class GitHubComment:
    node_id: str
    locator: str
    updated_at: str
    captured_at: str
    body_digest: str


@dataclass(frozen=True)
class GitHubRequest:
    issue_number: int
    node_id: str
    locator: str
    updated_at: str
    captured_at: str
    body_digest: str
    selected_replies: tuple[GitHubComment, ...] = ()


@dataclass(frozen=True)
class RequestRevision:
    subject: SubjectRevision
    authority: AuthorityPointer
    revision: ProducerEvent
    claim: ProducerEvent
    criteria: tuple[ProducerEvent, ...]

    @property
    def claim_id(self) -> str:
        return self.claim.event_id

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(item.event_id for item in self.criteria)


@dataclass(frozen=True)
class ReviewFinding:
    review_id: str
    reviewed_sha: str
    axis: str
    pass_name: str
    sequence: int
    action: str
    upstream_contract: str
    source: RequestRevision
    signature_digest: str
    failure_class: str
    validation_state: str
    observed_at: int


@dataclass(frozen=True)
class StageFact:
    stage_id: str
    stage: str
    source: RequestRevision
    signature_digest: str
    validation_state: str
    observed_at: int
    criterion_ids: tuple[str, ...] = ()
    objection_ref: str = ""


@dataclass(frozen=True)
class FixFact:
    review_id: str
    reviewed_sha: str
    pushed_sha: str
    actionable_finding_ids: tuple[str, ...]
    source: RequestRevision
    signature_digest: str
    validation_state: str = "observed"
    observed_at: int = 0
    failure_class: str = ""


@dataclass(frozen=True)
class AttemptFact:
    identity: str
    stage: str
    outcome: str
    observed_at: int
    source: RequestRevision
    cost: int | None = None
    round: int | None = None


@dataclass(frozen=True)
class AttemptJoin:
    identity: str
    stage: str
    outcome: str
    observed_at: int
    governing_event_ids: tuple[str, ...]
    cost: int | None = None
    round: int | None = None


@dataclass(frozen=True)
class SettlementFact:
    kind: str
    review_id: str
    evaluated_sha: str
    source: RequestRevision
    signature_digest: str
    observed_at: int
    fix_event_id: str
    finding_event_ids: tuple[str, ...] = ()
    validation_state: str = "observed"
    attempt_join: AttemptJoin | None = None


@dataclass(frozen=True)
class PipelineEvidence:
    event: ProducerEvent
    finding_id: str = ""
    actionable_finding_ids: tuple[str, ...] = ()
    failure_event_id: str = ""
    review_action_event_id: str = ""
    objection_id: str = ""
    attempt_join: AttemptJoin | None = None


@dataclass(frozen=True)
class Provenance:
    status: str
    authority: AuthorityPointer | None = None


class EvidenceProducer:
    """Create typed producer facts; only a real fix defect gets a failure observation."""
    NORMALIZER = "pipeline-v2"

    def __init__(self, store: EvidenceStore, *, repository: str) -> None:
        self._store = store
        self._repository = repository

    def _producer(self, observation_id: str, subject: SubjectRevision,
                  authority: AuthorityPointer, observed_at: int, kind: str,
                  fact_digest: str, validation_state: str,
                  links: tuple[EvidenceLink, ...] = (), review_action: str | None = None) -> ProducerEvent:
        event = self._store.observe(EvidenceEnvelopeV2(
            "producer_fact", observation_id, subject, authority, observed_at, links,
            producer=ProducerFacts(kind, fact_digest, self.NORMALIZER, validation_state,
                                  review_action),
        ))
        assert isinstance(event, ProducerEvent)
        return event

    def request(self, request: GitHubRequest, *, criterion_count: int = 1,
                observed_at: int = 0) -> RequestRevision:
        if (request.issue_number < 1 or not request.node_id or not request.locator
                or not request.updated_at or not request.captured_at
                or criterion_count < 0 or not _valid_digest(request.body_digest)):
            raise ValueError("request lacks durable identity")
        replies = tuple(sorted(request.selected_replies, key=lambda item: item.node_id))
        if (len({item.node_id for item in replies}) != len(replies)
                or any(not item.node_id or not item.locator or not item.updated_at or not item.captured_at
                       or not _valid_digest(item.body_digest)
                       for item in replies)):
            raise ValueError("selected reply lacks durable identity")
        source_set = _digest(request.node_id, request.locator, request.updated_at, request.captured_at,
            request.body_digest, *(f"{item.node_id}:{item.locator}:{item.updated_at}:{item.captured_at}:"
                                   f"{item.body_digest}" for item in replies))
        authority = AuthorityPointer("github", self._repository, request.locator,
            f"sha256:{source_set}", "sha256", source_set, "issue-source-set")
        subject = SubjectRevision("issue", f"issue/{request.issue_number}",
            f"issue-{source_set[:32]}", request.locator, source_set)
        revision = self._producer(_id("observation", "revision", source_set), subject, authority,
            observed_at, "revision", _digest("request-revision", source_set), "observed")
        claim = self._producer(_id("observation", "claim", source_set), subject, authority, observed_at,
            "claim", _digest("claim", source_set, "0"), "observed",
            (EvidenceLink("derives_from", revision.event_id, 0),))
        criteria = tuple(self._producer(_id("observation", "criterion", source_set, str(index)),
            subject, authority, observed_at, "criterion", _digest("criterion", source_set, str(index)),
            "observed", (EvidenceLink("derives_from", revision.event_id, 0),))
            for index in range(criterion_count))
        return RequestRevision(subject, authority, revision, claim, criteria)

    def review_source(self, review_id: str, reviewed_sha: str, *, locator: str,
                      observed_at: int = 0) -> RequestRevision:
        """Bind a review decision to its immutable reviewed commit without source prose."""
        if not review_id or not re.fullmatch(r"[a-f0-9]{40}", reviewed_sha) or not locator:
            raise ValueError("review source lacks durable identity")
        source_digest = _digest("review-source-v1", self._repository, locator, reviewed_sha)
        authority = AuthorityPointer(
            "github", self._repository, locator, reviewed_sha, "sha256", source_digest,
            "review-source")
        subject = SubjectRevision("review", f"review/{review_id}", reviewed_sha)
        observed = str(observed_at)
        revision = self._producer(
            _id("observation", "review-revision", review_id, reviewed_sha, observed), subject, authority,
            observed_at, "revision", source_digest, "observed")
        claim = self._producer(
            _id("observation", "review-claim", review_id, reviewed_sha, observed), subject, authority,
            observed_at, "claim", _digest("review-claim-v1", review_id, reviewed_sha),
            "observed", (EvidenceLink("derives_from", revision.event_id, 0),))
        criterion = self._producer(
            _id("observation", "review-criterion", review_id, reviewed_sha, observed), subject, authority,
            observed_at, "criterion", _digest("review-criterion-v1", review_id, reviewed_sha),
            "observed", (EvidenceLink("derives_from", revision.event_id, 0),))
        return RequestRevision(subject, authority, revision, claim, (criterion,))

    def stage_source(self, stage_id: str, stage: str, issue: str, source_revision: str,
                     input_digest: str, *, observed_at: int = 0) -> RequestRevision:
        """Bind a terminal stage fact to its frozen content-free input and checkout revision."""
        if (not stage_id or stage not in {"intake", "attack", "research"} or not issue
                or not re.fullmatch(r"[a-f0-9]{40}", source_revision)
                or not _valid_digest(input_digest)):
            raise ValueError("stage source lacks durable identity")
        locator = f"issues/{issue}"
        source_digest = _digest(
            "stage-source-v1", self._repository, stage_id, stage, issue,
            source_revision, input_digest)
        authority = AuthorityPointer(
            "github", self._repository, locator, source_revision, "sha256", input_digest,
            "stage-source")
        subject = SubjectRevision(
            "issue", f"issue/{issue}", f"issue-{source_digest[:32]}", locator, source_digest)
        revision = self._producer(
            _id("observation", "stage-revision", source_digest, str(observed_at)),
            subject, authority, observed_at, "revision", source_digest, "observed")
        claim = self._producer(
            _id("observation", "stage-claim", source_digest, str(observed_at)),
            subject, authority, observed_at, "claim",
            _digest("stage-claim-v1", source_digest), "observed",
            (EvidenceLink("derives_from", revision.event_id, 0),))
        criterion = self._producer(
            _id("observation", "stage-criterion", source_digest, str(observed_at)),
            subject, authority, observed_at, "criterion",
            _digest("stage-criterion-v1", source_digest), "observed",
            (EvidenceLink("derives_from", revision.event_id, 0),))
        return RequestRevision(subject, authority, revision, claim, (criterion,))

    def provenance(self, captured: RequestRevision, current: GitHubRequest | None) -> Provenance:
        if current is None:
            return Provenance("unavailable")
        try:
            reread = self.request(current, criterion_count=len(captured.criteria))
        except (TypeError, ValueError):
            return Provenance("unavailable")
        return (Provenance("available", captured.authority)
                if reread.authority == captured.authority else Provenance("unavailable"))

    def review(self, finding: ReviewFinding) -> PipelineEvidence:
        if finding.upstream_contract not in _UPSTREAM_CONTRACTS:
            raise ValueError("unknown upstream contract")
        review_action = _REVIEW_ACTIONS.get(finding.action)
        if review_action is None:
            raise ValueError("unknown review action")
        identity = _digest(finding.review_id, finding.reviewed_sha, finding.axis, finding.pass_name,
            str(finding.sequence), review_action, finding.upstream_contract, finding.signature_digest,
            finding.failure_class)
        subject = SubjectRevision("review", f"review/{finding.review_id}", finding.reviewed_sha)
        failure = self._store.observe(EvidenceEnvelopeV2(
            "failure_observation", _id("observation", "review-failure", identity,
                finding.source.subject.revision, str(finding.observed_at)), subject,
            finding.source.authority, finding.observed_at,
            failure=FailureFacts(finding.failure_class, finding.validation_state,
                finding.signature_digest, self.NORMALIZER),
        ))
        contract_digest = _digest("upstream-contract-v1", finding.upstream_contract)
        contract = self._producer(_id("observation", "upstream-contract",
            finding.upstream_contract, finding.source.subject.revision, str(finding.observed_at)),
            SubjectRevision("document", f"contract/{finding.upstream_contract}", "v1",
                f"contracts/{finding.upstream_contract}", contract_digest), finding.source.authority,
            finding.observed_at, "decision", contract_digest, finding.validation_state)
        action_identity = _digest(finding.review_id, finding.reviewed_sha, finding.axis,
                                  finding.pass_name, str(finding.sequence), review_action)
        action = self._producer(_id("observation", "review-action", action_identity,
            finding.source.subject.revision, str(finding.observed_at)),
            SubjectRevision("review", f"review-action/{action_identity[:32]}", finding.reviewed_sha),
            finding.source.authority, finding.observed_at, "review_action", action_identity,
            finding.validation_state, (EvidenceLink("addresses", failure.event_id, 0),), review_action)
        upstream = (finding.source.claim_id, *finding.source.criterion_ids)
        links = (*tuple(EvidenceLink("derives_from", target, index)
                        for index, target in enumerate(upstream)),
                 EvidenceLink("derives_from", failure.event_id, len(upstream)),
                 EvidenceLink("derives_from", contract.event_id, len(upstream) + 1),
                 EvidenceLink("derives_from", action.event_id, len(upstream) + 2))
        event = self._producer(_id("observation", "finding", identity, finding.source.subject.revision,
            str(finding.observed_at)),
            subject, finding.source.authority, finding.observed_at, "finding", identity,
            finding.validation_state, links)
        return PipelineEvidence(event, event.event_id, failure_event_id=failure.event_id,
                                review_action_event_id=action.event_id)

    def stage(self, fact: StageFact) -> PipelineEvidence:
        criteria = fact.criterion_ids or fact.source.criterion_ids
        objection_id = ""
        objection = None
        if fact.stage in {"attack", "redraft"}:
            if not fact.objection_ref:
                raise ValueError(f"{fact.stage} needs a stable objection reference")
            objection_id = _id("objection", fact.objection_ref)
            objection = self._producer(_id("observation", "objection-reference", objection_id,
                str(fact.observed_at)),
                SubjectRevision("document", objection_id, f"objection-{objection_id[-32:]}",
                    f"objections/{objection_id[-32:]}", _digest("objection", fact.objection_ref)),
                fact.source.authority, fact.observed_at, "objection",
                _digest("objection-reference", fact.objection_ref), fact.validation_state,
                (EvidenceLink("derives_from", fact.source.revision.event_id, 0),))
        if fact.stage == "attack":
            kind, digest = "objection", _digest("attack", fact.stage_id, fact.objection_ref,
                fact.signature_digest, *criteria)
        elif fact.stage == "redraft":
            kind, digest = "revision", _digest("redraft", fact.stage_id, fact.signature_digest, *criteria)
        else:
            kind, digest = "verification", _digest("stage", fact.stage_id, fact.stage,
                fact.signature_digest, *criteria)
        links = (EvidenceLink("derives_from", fact.source.revision.event_id, 0),
                 *(EvidenceLink("verifies" if kind == "verification" else "derives_from", target, index + 1)
                   for index, target in enumerate(criteria)),
                 *((EvidenceLink("derives_from", objection.event_id, len(criteria) + 1),)
                   if objection is not None else ()))
        subject = (SubjectRevision("document", objection_id, f"objection-{objection_id[-32:]}",
                   f"objections/{objection_id[-32:]}", _digest("objection", fact.objection_ref))
                   if fact.stage == "attack" else fact.source.subject)
        event = self._producer(_id("observation", "stage", fact.stage_id, fact.source.subject.revision,
            str(fact.observed_at)), subject, fact.source.authority, fact.observed_at, kind, digest,
            fact.validation_state, links)
        return PipelineEvidence(event, objection_id=objection_id)

    def fix(self, fix: FixFact) -> PipelineEvidence:
        ids = tuple(sorted(set(fix.actionable_finding_ids)))
        if not ids:
            raise ValueError("fix needs its complete actionable-finding set")
        subject = SubjectRevision("review", f"review/{fix.review_id}", fix.pushed_sha)
        parent = self._producer(_id("observation", "reviewed-parent", fix.review_id,
            fix.reviewed_sha, fix.source.subject.revision, str(fix.observed_at)),
            SubjectRevision("review", f"review/{fix.review_id}", fix.reviewed_sha), fix.source.authority,
            fix.observed_at, "revision", _digest("reviewed-parent", fix.review_id, fix.reviewed_sha),
            fix.validation_state)
        failure_event_id = ""
        if fix.failure_class == "fix_introduced_defect":
            failure = self._store.observe(EvidenceEnvelopeV2(
                "failure_observation", _id("observation", "fix-defect", fix.review_id,
                    fix.reviewed_sha, fix.pushed_sha, fix.signature_digest, str(fix.observed_at)),
                subject, fix.source.authority, fix.observed_at,
                failure=FailureFacts("fix_introduced_defect", fix.validation_state,
                    fix.signature_digest, self.NORMALIZER, fix.reviewed_sha, fix.pushed_sha),
            ))
            failure_event_id = failure.event_id
        elif fix.failure_class:
            raise ValueError("only fix-introduced defects create failure observations")
        links = (*tuple(EvidenceLink("addresses", target, index) for index, target in enumerate(ids)),
                 EvidenceLink("revises", parent.event_id, len(ids)),
                 *((EvidenceLink("derives_from", failure_event_id, len(ids) + 1),)
                   if failure_event_id else ()))
        event = self._producer(_id("observation", "fix", fix.review_id, fix.reviewed_sha,
            fix.pushed_sha, str(fix.observed_at), *ids), subject, fix.source.authority, fix.observed_at, "fix",
            _digest("fix", fix.review_id, fix.reviewed_sha, fix.pushed_sha, fix.signature_digest, *ids),
            fix.validation_state, links)
        return PipelineEvidence(event, actionable_finding_ids=ids, failure_event_id=failure_event_id)

    def settlement(self, settlement: SettlementFact) -> PipelineEvidence:
        if settlement.kind not in {"merge", "park"}:
            raise ValueError("unknown settlement")
        subject = SubjectRevision("review", f"review/{settlement.review_id}", settlement.evaluated_sha)
        disposition = self._producer(_id("observation", "disposition", settlement.kind,
            settlement.review_id, settlement.evaluated_sha, str(settlement.observed_at)),
            SubjectRevision("review", f"disposition/{settlement.kind}/{settlement.review_id}",
                settlement.evaluated_sha),
            settlement.source.authority, settlement.observed_at, "disposition",
            _digest("disposition", settlement.kind, settlement.review_id, settlement.evaluated_sha),
            settlement.validation_state, (EvidenceLink("derives_from", settlement.fix_event_id, 0),))
        if (settlement.attempt_join is not None
                and settlement.source.revision.event_id not in settlement.attempt_join.governing_event_ids):
            raise ValueError("attempt join does not govern this settlement")
        upstream = settlement.finding_event_ids
        links = (EvidenceLink("settles", settlement.fix_event_id, 0),
                 EvidenceLink("derives_from", disposition.event_id, 1),
                 *(EvidenceLink("derives_from", target, index + 2)
                   for index, target in enumerate(upstream)))
        event = self._producer(_id("observation", "settlement", settlement.kind,
            settlement.review_id, settlement.evaluated_sha, settlement.fix_event_id,
            str(settlement.observed_at)), subject,
            settlement.source.authority, settlement.observed_at, "settlement",
            _digest("settlement", settlement.kind, settlement.review_id, settlement.evaluated_sha,
                settlement.signature_digest), settlement.validation_state, links)
        return PipelineEvidence(event, attempt_join=settlement.attempt_join)

    def attempt(self, attempt: AttemptFact) -> AttemptJoin:
        """Return operational telemetry joined to immutable Evidence identity, not Evidence."""
        if not attempt.identity or not attempt.stage or not attempt.outcome:
            raise ValueError("attempt lacks durable identity")
        if (attempt.cost is not None and (isinstance(attempt.cost, bool) or attempt.cost < 0)
                or (attempt.round is not None and (isinstance(attempt.round, bool) or attempt.round < 0))):
            raise ValueError("attempt metrics must be non-negative")
        return AttemptJoin(attempt.identity, attempt.stage, attempt.outcome, attempt.observed_at,
                           (attempt.source.revision.event_id,), attempt.cost, attempt.round)


@dataclass(frozen=True)
class LessonInput:
    event_id: str
    proposal_digest: str


class EvidenceReceiptQuery(Protocol):
    """Injected read-only access to immutable, content-free Evidence event receipts."""
    def read(self, event_id: str) -> EvidenceRecord: ...


class EvidenceMiner:
    """Read-only miner that returns candidates without evaluating or mutating Evidence."""
    SAFE_STATES = frozenset({"reproduced", "model_judged", "human_validated"})

    def __init__(self, receipts: EvidenceReceiptQuery) -> None:
        self._receipts = receipts

    def candidates(self, inputs: Iterable[LessonInput], *, policy_version: int,
                   nominated_at: int) -> tuple[LessonCandidate, ...]:
        groups: dict[tuple[str, str, str, str], set[str]] = {}
        for item in inputs:
            if not _valid_digest(item.proposal_digest):
                continue
            try:
                event = self._receipts.read(item.event_id)
                linked = tuple(self._receipts.read(link.target_event_id) for link in event.links)
            except ValueError:
                continue
            failure = next((candidate for candidate in linked
                            if candidate.event_kind == "failure_observation"), None)
            finding = (event if event.producer_kind == "finding" else next(
                (candidate for candidate in linked if candidate.producer_kind == "finding"), None))
            if (event.producer_kind not in {"finding", "fix"} or finding is None or failure is None
                    or not set(failure.validation_states) & self.SAFE_STATES):
                continue
            try:
                method = next(candidate.subject.removeprefix("contract/") for candidate in
                    (self._receipts.read(link.target_event_id) for link in finding.links)
                    if candidate.producer_kind == "decision" and candidate.revision == "v1"
                    and candidate.subject.startswith("contract/"))
            except (StopIteration, ValueError):
                continue
            if method not in _UPSTREAM_CONTRACTS:
                continue
            groups.setdefault((method, item.proposal_digest, failure.failure_class,
                               failure.normalized_signature), set()).add(event.event_id)
        return tuple(LessonCandidate(_id("lesson", method, digest, failure_class, signature,
                                         *sorted(events)), tuple(sorted(events)),
            digest, policy_version, nominated_at)
            for (method, digest, failure_class, signature), events in sorted(groups.items())
            if len(events) >= 2)
