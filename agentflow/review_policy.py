"""Depth-aware review policy and durable handoff values (ADR 0047).

This module is the policy boundary. Coordinator code supplies repository facts; this module
normalizes the author's proposal, enforces sensitive-area minimums, validates a reviewer's
structured result, and decides whether another independent pass is required.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ReviewDepth(StrEnum):
    FOCUSED = "focused"
    TARGETED = "targeted"
    FULL = "full"


class ReviewAxis(StrEnum):
    COMBINED = "combined"
    PRODUCT = "product"
    STANDARDS = "standards"
    FIX = "fix"
    DECISION = "decision"


class UIVerification(StrEnum):
    """The Review's execution result for user-facing behavior."""

    NOT_REQUIRED = "not_required"
    PASSED = "passed"
    UNAVAILABLE = "unavailable"


class ReviewAction(StrEnum):
    FIX = "fix_before_completion"
    FOLLOW_UP = "necessary_follow_up"
    ASK = "ask_maintainer"
    DISCARD = "discard_preference"


_DEPTH_ORDER = {
    ReviewDepth.FOCUSED: 0,
    ReviewDepth.TARGETED: 1,
    ReviewDepth.FULL: 2,
}

_FULL_PATH_TERMS = (
    "auth", "permission", "security", "secret", "credential", "safety", "privacy",
    "medical", "billing", "payment", "merge", "gate.py", "coordinator",
)
_FULL_CONTEXT_RE = re.compile(
    r"\b(permission|authorization|authentication|credential|secret|privacy|medical|billing|"
    r"payment|safety|destructive|irreversible|delete|destroy|revoke|shared (?:behavior|decision|"
    r"policy|rule)|global (?:behavior|decision|policy|rule)|competing product|incompatible "
    r"(?:behavior|intent))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DepthAssignment:
    depth: ReviewDepth
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewAssignment:
    """One durable review scope. Axis is part of the assignment, not orchestration trivia."""

    depth: ReviewDepth = ReviewDepth.TARGETED
    reason: str = "one contained change"
    axis: ReviewAxis = ReviewAxis.COMBINED


_PROPOSAL_RE = re.compile(
    r"^Review depth:\s*(Focused|Targeted|Full)\s+[—-]\s+(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE)


def proposed_depth(pr_body: str) -> DepthAssignment:
    """Read the author's one-line proposal; absence safely defaults to Targeted."""
    match = _PROPOSAL_RE.search(pr_body or "")
    if match is None:
        return DepthAssignment(ReviewDepth.TARGETED, "author supplied no review-depth proposal")
    return DepthAssignment(ReviewDepth(match.group(1).lower()), match.group(2).strip())


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    action: ReviewAction
    summary: str
    grounding: str
    file: str = ""
    line: int = 0
    failure_class: str = ""


def encode_findings(findings: tuple[ReviewFinding, ...]) -> str:
    """Encode the durable Full-axis finding ledger."""
    return json.dumps([
        {"action": item.action.value, "summary": item.summary, "grounding": item.grounding,
         "file": item.file, "line": item.line, "failure_class": item.failure_class}
        for item in findings
    ], sort_keys=True)


def decode_findings(payload: str) -> tuple[ReviewFinding, ...] | None:
    """Decode a durable Full-axis finding ledger, failing closed on malformed state."""
    try:
        raw_items = json.loads(payload or "[]")
        if not isinstance(raw_items, list):
            return None
        result = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                return None
            failure_class = str(raw.get("failure_class", ""))
            if failure_class not in {"", "fix_introduced_defect", "original_defect", "plan_gap",
                                     "reviewer_false_claim", "slice_scope_error",
                                     "speculative_preference"}:
                return None
            result.append(ReviewFinding(
                ReviewAction(str(raw["action"])), str(raw["summary"]),
                str(raw["grounding"]), str(raw.get("file", "")), int(raw.get("line", 0)),
                failure_class))
        return tuple(result)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def merge_findings(*groups: tuple[ReviewFinding, ...]) -> tuple[ReviewFinding, ...]:
    """Stable union for independently-produced Full product and standards findings."""
    return tuple(dict.fromkeys(item for group in groups for item in group))


@dataclass(frozen=True, slots=True)
class FollowUp:
    evidence: str
    desired_outcome: str
    historic_url: str = ""


def merge_follow_ups(prior: tuple[FollowUp, ...], current: tuple[FollowUp, ...]) -> tuple[FollowUp, ...]:
    """Keep durable historical references and the newest single live proposal.

    A successor review may carry older, already-filed references while replacing the one
    proposal that describes current follow-up work. Keeping the proposal separate from history
    prevents a normal continuation from becoming unpersistable.
    """
    all_items = prior + current
    historic = tuple(dict.fromkeys(item for item in all_items if item.historic_url))
    proposals = tuple(item for item in current if not item.historic_url)
    return historic + proposals[-1:]


def _park_field(value: str, limit: int) -> str:
    """One deterministic public-envelope field; no later whole-body trimming is permitted."""
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[:limit - 1] + "…"


def format_park_comment(context, verdict=None, *, proof_marker: str = "",
                        park_mark: str = "> *agentflow: parked for human review.*") -> str:
    """Render the fixed, operator-sized public review park envelope.

    This is deliberately pure and applies every dynamic-field budget before interpolation. The
    terminal cap is only a regression tripwire: it never chooses fields or silently removes text.
    """
    marker = f"\n<!-- {proof_marker} -->" if proof_marker else ""
    heading = "Maintainer decision needed" if context.decision_needed else "Action needed"
    options = tuple(context.options[:2])
    option_line = " | ".join(_park_field(item, 120) for item in options) or "None."
    locations = tuple(context.locations[:2])
    location_line = " | ".join(_park_field(item, 80) for item in locations) or "None."
    proposal = (() if verdict is None else tuple(
        item for item in getattr(verdict, "follow_ups", ()) if not item.historic_url)[:1])
    proposal_block = ""
    if proposal:
        proposal_block = (
            "\nProposed follow-up: "
            f"{_park_field(proposal[0].desired_outcome, 100)}; evidence: "
            f"{_park_field(proposal[0].evidence, 100)}")
    facts = ""
    if verdict is not None:
        depth = getattr(getattr(verdict, "depth", None), "value", "")
        facts = f"\nReview: {str(depth).title() or 'Legacy'}"
        if getattr(verdict, "reviewer_tool", ""):
            facts += (" / cross-tool" if verdict.reviewer_tool != verdict.change_author_tool
                      else " / same-tool; merge required")
    body = (
        f"{park_mark}{marker}\n## {heading}\n"
        f"Affected behavior: {_park_field(context.behavior, 200)}\n"
        f"Options: {option_line}\n"
        f"Consequences: {_park_field(context.consequences, 160)}\n"
        f"Recommendation: {_park_field(context.recommendation, 160)}\n"
        "## Agent handoff\n"
        f"Code locations: {location_line}\n"
        f"Unresolved fact: {_park_field(context.conflicts, 180)}\n"
        f"Completed check: {_park_field(context.checks[0] if context.checks else '', 100)}\n"
        f"Retained work: {_park_field(context.retained_work, 80)}\n"
        f"Exact next action: {_park_field(context.next_action, 160)}"
        f"{proposal_block}{facts}")
    if len(body) > 2_000:
        raise ValueError("park formatter exceeded its 2,000-character budget")
    return body


@dataclass(frozen=True, slots=True)
class Uncertainty:
    options: tuple[str, ...]
    missing_guidance: str
    recommendation: str


@dataclass(frozen=True, slots=True)
class ReviewState:
    """Cohesive in-memory review ledger.

    Coordinator persistence remains the compatibility boundary: only ``from_record`` and
    ``record_fields`` know that the current SQLite schema stores this value as flat columns and
    JSON strings.
    """

    assignment: ReviewAssignment = field(default_factory=ReviewAssignment)
    change_author_tool: str | None = None
    reviewed_from_sha: str | None = None
    passes: int = 0
    sequence: int = 0
    cross_tool_covered: bool = False
    tainted: bool = False
    taint_cleared: bool = False
    handoff: str | None = None
    findings: tuple[ReviewFinding, ...] = ()
    fixes: tuple[str, ...] = ()
    follow_ups: tuple[FollowUp, ...] = ()
    checks: tuple[str, ...] = ()
    uncertainty: Uncertainty | None = None
    uncertainty_handoffs: int = 0

    @classmethod
    def from_record(cls, record: Any) -> ReviewState | None:
        """Decode a legacy flat Record, failing closed on malformed durable state."""
        findings = decode_findings(getattr(record, "review_findings", "[]"))
        if findings is None:
            return None
        try:
            fixes = _json_strings(getattr(record, "review_fixes", "[]"))
            checks = _json_strings(getattr(record, "review_checks", "[]"))
            follow_ups = _decode_follow_ups(getattr(record, "review_follow_ups", "[]"))
            uncertainty = _decode_uncertainty(getattr(record, "review_uncertainty", None))
            if fixes is None or checks is None or follow_ups is None:
                return None
            return cls(
                assignment=ReviewAssignment(
                    ReviewDepth(getattr(record, "review_depth", "targeted")),
                    getattr(record, "depth_reason", "") or "one contained change",
                    ReviewAxis(getattr(record, "review_axis", "combined"))),
                change_author_tool=getattr(record, "change_author_tool", None),
                reviewed_from_sha=getattr(record, "reviewed_from_sha", None),
                passes=int(getattr(record, "review_passes", 0)),
                sequence=int(getattr(record, "review_sequence", 0)),
                cross_tool_covered=bool(getattr(record, "cross_tool_covered", False)),
                tainted=bool(getattr(record, "review_tainted", False)),
                taint_cleared=bool(getattr(record, "review_taint_cleared", False)),
                handoff=getattr(record, "review_handoff", None),
                findings=findings, fixes=fixes, follow_ups=follow_ups, checks=checks,
                uncertainty=uncertainty,
                uncertainty_handoffs=int(getattr(record, "uncertainty_handoffs", 0)))
        except (TypeError, ValueError):
            return None

    def record_fields(self) -> dict[str, Any]:
        """Encode this value into the existing flat Record/store schema."""
        if sum(not item.historic_url for item in self.follow_ups) > 1:
            raise ValueError("new review records allow at most one follow-up proposal")
        return {
            "review_depth": self.assignment.depth.value,
            "depth_reason": self.assignment.reason,
            "review_axis": self.assignment.axis.value,
            "change_author_tool": self.change_author_tool,
            "reviewed_from_sha": self.reviewed_from_sha,
            "review_passes": self.passes,
            "review_sequence": self.sequence,
            "cross_tool_covered": self.cross_tool_covered,
            "review_tainted": self.tainted,
            "review_taint_cleared": self.taint_cleared,
            "review_handoff": self.handoff,
            "review_findings": encode_findings(self.findings),
            "review_fixes": json.dumps(list(self.fixes)),
            "review_follow_ups": json.dumps([
                {"evidence": item.evidence, "desired_outcome": item.desired_outcome,
                 **({"url": item.historic_url} if item.historic_url else {})}
                for item in self.follow_ups
            ], sort_keys=True),
            "review_checks": json.dumps(list(self.checks)),
            "review_uncertainty": _encode_uncertainty(self.uncertainty),
            "uncertainty_handoffs": self.uncertainty_handoffs,
        }


def _json_strings(payload: str) -> tuple[str, ...] | None:
    try:
        value = json.loads(payload or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _decode_follow_ups(payload: str) -> tuple[FollowUp, ...] | None:
    try:
        values = json.loads(payload or "[]")
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            return None
        result = tuple(FollowUp(
            value["evidence"], value["desired_outcome"], value.get("url", ""))
            for value in values)
        if not all(
                isinstance(part, str) and part.strip()
                for item in result
                for part in (item.evidence, item.desired_outcome)):
            return None
        if sum(not item.historic_url for item in result) > 1:
            return None
        return result
    except (KeyError, TypeError, json.JSONDecodeError):
        return None


def _encode_uncertainty(value: Uncertainty | None) -> str | None:
    if value is None:
        return None
    return json.dumps({
        "options": list(value.options),
        "missing_guidance": value.missing_guidance,
        "recommendation": value.recommendation,
    }, sort_keys=True)


def _decode_uncertainty(payload: str | None) -> Uncertainty | None:
    if not payload:
        return None
    try:
        value = json.loads(payload)
        options = value["options"]
        if (not isinstance(options, list) or len(options) != 2
                or not all(isinstance(item, str) for item in options)):
            raise ValueError
        return Uncertainty(
            tuple(options), str(value["missing_guidance"]), str(value["recommendation"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("malformed review uncertainty")


# One durable park/resume contract owns a PR exact head's whole review chain (#344). A recorded
# product decision belongs to that chain, not to whichever record happened to stop last: only the
# maintainer's own answer retires it, and that answer is bound to the comment it came from.
_DECISION_ANSWER_PREFIX = "Maintainer decision answer for "
_DECISION_ANSWER_SEPARATOR = " — "


def decision_answer_handoff(target: str, answer: str) -> str:
    """The private next-agent context for a review resumed by the maintainer's own answer.

    It carries the answered PR comment as its durable binding, so the exact-head chain can tell a
    settled decision from an unanswered one without a second ownership or comment protocol.
    """
    return f"{_DECISION_ANSWER_PREFIX}{target}{_DECISION_ANSWER_SEPARATOR}{answer.strip()}"


def decision_answer_target(handoff: str | None) -> str:
    """The maintainer comment a resumed review answers, or ``""`` for any other handoff."""
    if not handoff or not handoff.startswith(_DECISION_ANSWER_PREFIX):
        return ""
    rest = handoff[len(_DECISION_ANSWER_PREFIX):]
    return rest.split(_DECISION_ANSWER_SEPARATOR)[0].strip()


def unresolved_uncertainty(chain: Any) -> Uncertainty | None:
    """The latest structured product decision in one exact head's review chain that no maintainer
    has answered yet. Pure over durable records — the test surface (ADR 0020).

    Records are read in durable chain order (creation, then same-head sequence) because a Standards
    or Fix pass that recorded no decision of its own must not erase the one an earlier Product pass
    recorded (#344). Malformed durable state contributes nothing rather than inventing a decision.
    """
    pending = None
    for record in sorted(chain, key=lambda item: (
            int(getattr(item, "created_at", 0) or 0),
            int(getattr(item, "review_sequence", 0) or 0),
            str(getattr(item, "identity", "")))):
        if decision_answer_target(getattr(record, "review_handoff", None)):
            pending = None
        try:
            found = _decode_uncertainty(getattr(record, "review_uncertainty", None))
        except ValueError:
            continue
        if found is not None:
            pending = found
    return pending


_CONFLICT_UNCERTAINTY_RE = re.compile(r"^CONFLICT-UNCERTAINTY:\s*(\{.*\})\s*$", re.MULTILINE)

# How a Revise stage records that private ambiguity as its own durable outcome, so the decision
# review it opens recognizes it. Written by Revise and read by Review, so it belongs with the
# uncertainty vocabulary rather than in either stage's module.
CONFLICT_UNCERTAINTY_PREFIX = "conflict-uncertainty:"


def conflict_uncertainty_from_message(message: str) -> Uncertainty | None:
    """Parse a resolver's private durable uncertainty marker; PR comments do not count."""
    match = _CONFLICT_UNCERTAINTY_RE.search(message or "")
    if match is None:
        return None
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    options = _string_list(raw.get("options"))
    missing = raw.get("missing_guidance")
    recommendation = raw.get("recommendation")
    if (options is None or len(options) != 2
            or not isinstance(missing, str) or not missing.strip()
            or not isinstance(recommendation, str) or not recommendation.strip()):
        return None
    return Uncertainty(options, missing.strip(), recommendation.strip())


@dataclass(frozen=True, slots=True)
class ReviewResult:
    clean: bool
    parsed: bool
    detail: str = ""
    depth: ReviewDepth = ReviewDepth.TARGETED
    depth_reason: str = ""
    axis: ReviewAxis = ReviewAxis.COMBINED
    change_author_tool: str = ""
    reviewed_sha: str = ""
    final_sha: str = ""
    pushed_sha: str = ""
    fixes: tuple[str, ...] = ()
    follow_ups: tuple[FollowUp, ...] = ()
    checks: tuple[str, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    uncertainty: Uncertainty | None = None
    decision: str = ""
    ui_verification: UIVerification = UIVerification.NOT_REQUIRED

    @property
    def changed(self) -> bool:
        return bool(self.pushed_sha)

    @property
    def needs_maintainer(self) -> bool:
        return self.uncertainty is not None or any(
            finding.action is ReviewAction.ASK for finding in self.findings)


def other_tool(tool: str) -> str | None:
    return {"claude": "codex", "codex": "claude"}.get(tool)


def _invalid(detail: str) -> ReviewResult:
    return ReviewResult(clean=False, parsed=False, detail=detail)


def _string_list(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip()
                                               for item in value):
        return None
    return tuple(item.strip() for item in value)


def parse_review_result(payload: str, *, expected_sha: str | None = None,
                        expected_depth: str | ReviewDepth | None = None,
                        expected_axis: str | ReviewAxis | None = None,
                        expected_author: str | None = None,
                        owned_heads: tuple[str, ...] = ()) -> ReviewResult:
    """Parse one strict ADR 0047 result; malformed proof always fails closed.

    ``owned_heads`` are final heads whose push provenance the caller has already proven durably —
    an earlier attempt of the same logical review pushed them, and the retained checkout owns
    them. A continuation reviewer honestly reports ``pushed_sha: ""`` (it pushed nothing itself),
    so a moved final head named in ``owned_heads`` is accepted without in-payload provenance;
    every other provenance rule stays strict."""
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            return _invalid("review result is not an object")
        reviewed_sha = data.get("reviewed_sha")
        final_sha = data.get("final_sha")
        pushed_sha = data.get("pushed_sha")
        if not all(isinstance(value, str) for value in (reviewed_sha, final_sha, pushed_sha)):
            return _invalid("review range is malformed")
        if not reviewed_sha or (expected_sha is not None and reviewed_sha != expected_sha):
            return _invalid("reviewed_sha does not match the assigned head")
        if not final_sha:
            return _invalid("final_sha is missing")
        fixes = _string_list(data.get("fixes"))
        checks = _string_list(data.get("checks"))
        if fixes is None or checks is None:
            return _invalid("fixes or checks is not a non-empty string list")
        if not checks:
            return _invalid("review checks are missing")
        if (final_sha != reviewed_sha and not pushed_sha
                and final_sha not in owned_heads):
            return _invalid("changed final head has no push provenance")
        if pushed_sha and (pushed_sha != final_sha or final_sha == reviewed_sha):
            return _invalid("push provenance does not identify a new final head")
        if fixes and not pushed_sha:
            return _invalid("shipped fixes have no push provenance")

        depth = ReviewDepth(str(data.get("depth", "")).lower())
        axis = ReviewAxis(str(data.get("axis", "")).lower())
        depth_reason = data.get("depth_reason")
        author = data.get("change_author_tool")
        if not isinstance(depth_reason, str) or not depth_reason.strip():
            return _invalid("depth reason is missing")
        if author not in {"claude", "codex"}:
            return _invalid("change author tool is missing")
        if expected_depth is not None:
            assigned_depth = ReviewDepth(str(expected_depth).lower())
            if _DEPTH_ORDER[depth] < _DEPTH_ORDER[assigned_depth]:
                return _invalid("review depth downgraded below its durable assignment")
        if expected_axis is not None and axis is not ReviewAxis(str(expected_axis).lower()):
            return _invalid("review axis does not match its durable assignment")
        if expected_author is not None and author != expected_author:
            return _invalid("change author tool does not match its durable assignment")

        raw_follow_ups = data.get("follow_ups")
        if not isinstance(raw_follow_ups, list):
            return _invalid("follow_ups is not a list")
        follow_ups = []
        if len(raw_follow_ups) > 1:
            return _invalid("review has more than one follow-up proposal")
        for raw in raw_follow_ups:
            if not isinstance(raw, dict):
                return _invalid("follow-up is not an object")
            if set(raw) != {"evidence", "desired_outcome"}:
                return _invalid("follow-up proposal has unsupported fields")
            values = tuple(raw.get(key) for key in ("evidence", "desired_outcome"))
            if not all(isinstance(value, str) and value.strip() for value in values):
                return _invalid("follow-up proposal lacks evidence or desired outcome")
            follow_ups.append(FollowUp(*(value.strip() for value in values)))

        raw_findings = data.get("findings")
        if not isinstance(raw_findings, list):
            return _invalid("findings is not a list")
        findings = []
        for raw in raw_findings:
            if not isinstance(raw, dict):
                return _invalid("finding is not an object")
            action = ReviewAction(str(raw.get("action", "")))
            summary, grounding = raw.get("summary"), raw.get("grounding")
            if not isinstance(summary, str) or not summary.strip():
                return _invalid("finding summary is missing")
            if not isinstance(grounding, str) or not grounding.strip():
                return _invalid("finding grounding is missing")
            line = raw.get("line", 0)
            if not isinstance(line, int):
                return _invalid("finding line is not an integer")
            failure_class = str(raw.get("failure_class", ""))
            if failure_class and failure_class not in {
                    "fix_introduced_defect", "original_defect", "plan_gap",
                    "reviewer_false_claim", "slice_scope_error", "speculative_preference"}:
                return _invalid("finding failure class is unknown")
            findings.append(ReviewFinding(
                action, summary.strip(), grounding.strip(), str(raw.get("file", "")), line,
                failure_class))

        uncertainty = None
        raw_uncertainty = data.get("uncertainty")
        if raw_uncertainty is not None:
            if not isinstance(raw_uncertainty, dict):
                return _invalid("uncertainty is not an object")
            options = _string_list(raw_uncertainty.get("options"))
            missing = raw_uncertainty.get("missing_guidance")
            recommendation = raw_uncertainty.get("recommendation")
            if (options is None or len(options) != 2
                    or not isinstance(missing, str) or not missing.strip()
                    or not isinstance(recommendation, str) or not recommendation.strip()):
                return _invalid("uncertainty needs two options, missing guidance, and recommendation")
            uncertainty = Uncertainty(options, missing.strip(), recommendation.strip())

        decision = data.get("decision", "")
        if not isinstance(decision, str):
            return _invalid("decision is not a string")
        if axis is ReviewAxis.DECISION and not decision.strip() and uncertainty is None:
            return _invalid("decision pass returned neither a decision nor uncertainty")
        try:
            ui_verification = UIVerification(str(data.get("ui_verification", "not_required")))
        except ValueError:
            return _invalid("ui verification state is unknown")
        needs_follow_up = any(item.action is ReviewAction.FOLLOW_UP for item in findings)
        if needs_follow_up != bool(follow_ups):
            return _invalid("necessary follow-up finding and proposal must appear together")

        blocking = uncertainty is not None or any(
            finding.action in {ReviewAction.FIX, ReviewAction.ASK} for finding in findings)
        clean = (data.get("verdict") == "PASS" and not blocking
                 and ui_verification is not UIVerification.UNAVAILABLE)
        return ReviewResult(
            clean=clean, parsed=True, depth=depth, depth_reason=depth_reason.strip(), axis=axis,
            change_author_tool=author, reviewed_sha=reviewed_sha, final_sha=final_sha,
            pushed_sha=pushed_sha, fixes=fixes, follow_ups=tuple(follow_ups), checks=checks,
            findings=tuple(findings), uncertainty=uncertainty, decision=decision.strip(),
            ui_verification=ui_verification)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return _invalid(f"review result parse error: {type(exc).__name__}")


def assign_depth(proposed: str, reason: str, changed_files: list[str] | tuple[str, ...],
                 *, context: str = "", conflict_competes: bool = False,
                 guarded: bool = False) -> DepthAssignment:
    """Apply ADR 0047's non-downgradable minimum to an author's depth proposal."""
    try:
        proposed_depth = ReviewDepth(str(proposed).strip().lower())
    except ValueError:
        proposed_depth = ReviewDepth.TARGETED
        reason = "author proposal was missing or invalid"
    sensitive = guarded or conflict_competes or bool(_FULL_CONTEXT_RE.search(context or "")) or any(
        term in path.lower() for path in changed_files for term in _FULL_PATH_TERMS)
    minimum = ReviewDepth.FULL if sensitive else proposed_depth
    depth = max((proposed_depth, minimum), key=_DEPTH_ORDER.__getitem__)
    if guarded:
        return DepthAssignment(depth, "guarded profile requires Full review")
    if conflict_competes:
        return DepthAssignment(depth, "competing product behavior requires Full review")
    if sensitive and proposed_depth is not ReviewDepth.FULL:
        return DepthAssignment(depth, "sensitive or shared behavior requires Full review")
    return DepthAssignment(depth, reason.strip() or "author-proposed review depth")
