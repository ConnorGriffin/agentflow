"""Read-only observational learning report over terminal review and revise records."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path

from agentflow.coordinator.record import COMPLETED, HELD
from agentflow.coordinator.store import Store
from agentflow.coordinator.telemetry import AttemptTelemetry, read_attempts_with_health
from agentflow.review_policy import ReviewState

SCHEMA = "agentflow-learning-report-v1"


def report(repository: str, start: date, end: date, store_path: Path | str) -> dict:
    """Project durable terminal facts into the deliberately narrow learning report."""
    records = Store.load_records_read_only(store_path)
    lesson_uses = Store.load_lesson_use_attributions_read_only(store_path)
    telemetry = read_attempts_with_health(store_path)
    by_identity: dict[str, list[AttemptTelemetry]] = defaultdict(list)
    for entry in telemetry.entries:
        by_identity[entry.identity].append(entry)
    start_at = int(datetime.combine(start, time.min, timezone.utc).timestamp())
    end_at = int(datetime.combine(end, time.min, timezone.utc).timestamp())
    included = []
    for record in records.values():
        attempts = by_identity.get(record.identity, [])
        if (record.repo != repository or record.stage not in {"review", "revise"}
                or record.state not in {COMPLETED, HELD} or not attempts):
            continue
        cohort = max(item.finalized_at for item in attempts)
        if start_at <= cohort < end_at:
            included.append((record, attempts))

    subjects: dict[str, list] = defaultdict(list)
    for record, _attempts in included:
        subjects[record.subject].append(record)

    subject_items = []
    for subject, subject_records in subjects.items():
        revise = [record for record in subject_records if record.stage == "revise"]
        rounds = {record.round for record in revise if record.round > 0}
        rounds.update(("legacy", record.identity) for record in revise if record.round <= 0)
        pointers = [{
            "stage": record.stage,
            "identity": record.identity,
            "revision": record.subject_revision or None,
            "state": record.state,
            "finalized_at": max(item.finalized_at for item in by_identity[record.identity]),
        } for record in subject_records]
        pointers.sort(key=lambda item: (item["stage"], item["identity"]))
        subject_items.append({"subject": subject, "revise_rounds": len(rounds), "records": pointers})
    subject_items.sort(key=lambda item: item["subject"])
    cohort_subjects = {"pre_adoption": [], "post_adoption": []}
    for item in subject_items:
        cohort = ("post_adoption" if any(pointer["identity"] in lesson_uses
                  for pointer in item["records"]) else "pre_adoption")
        cohort_subjects[cohort].append(item)

    records_by_identity = {record.identity: record for record, _attempts in included}
    token_fields = (
        "input_tokens", "cached_input_tokens", "cache_creation_tokens",
        "output_tokens", "reasoning_output_tokens")

    def summarize(items):
        denominator = len(items)
        identities = {pointer["identity"] for item in items for pointer in item["records"]}
        cohort_attempts = [item for identity in identities for item in by_identity[identity]]
        cohort_records = [records_by_identity[identity] for identity in identities]
        revise_total = sum(item["revise_rounds"] for item in items)
        revised = sum(item["revise_rounds"] > 0 for item in items)
        terminal = {state: sum(record.state == state for record in cohort_records)
                    for state in (COMPLETED, HELD)}
        unavailable = 0
        finding_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        for record in cohort_records:
            review = ReviewState.from_record(record)
            if review is None:
                unavailable += 1
                continue
            for finding in review.findings:
                finding_counts[(record.stage, review.assignment.axis.value,
                                finding.action.value)] += 1
        elapsed_total = elapsed_known = token_total = token_known = cost_known = 0
        cost_total = 0
        for attempt in cohort_attempts:
            if attempt.started_at > 0 and attempt.finalized_at > 0:
                elapsed_total += max(0, attempt.finalized_at - attempt.started_at)
                elapsed_known += 1
            values = [getattr(attempt.usage, name) for name in token_fields]
            if any(value is not None for value in values):
                token_total += sum(value for value in values if value is not None)
                token_known += 1
            if attempt.usage.cost_usd is not None:
                cost_total += attempt.usage.cost_usd
                cost_known += 1
        summary = {
            "terminal_subjects": denominator,
            "subjects_with_revise": revised,
            "revision_required_rate": {"numerator": revised, "denominator": denominator},
            "revise_rounds": {"total": revise_total,
                              "mean": {"numerator": revise_total, "denominator": denominator}},
            "terminal_records": {"completed": terminal[COMPLETED], "held": terminal[HELD]},
            "review_state_unavailable": unavailable,
            "attempts": len(cohort_attempts),
            "elapsed_seconds": {"total": elapsed_total, "attempts_known": elapsed_known,
                                "attempts_unknown": len(cohort_attempts) - elapsed_known},
            "tokens": {"total": token_total, "attempts_known": token_known,
                       "attempts_unknown": len(cohort_attempts) - token_known},
            "cost_usd": {"total": cost_total, "attempts_known": cost_known,
                         "attempts_unknown": len(cohort_attempts) - cost_known},
        }
        return summary, [
            {"stage": stage, "axis": axis, "action": action, "count": count}
            for (stage, axis, action), count in sorted(finding_counts.items())]

    summary, finding_groups = summarize(subject_items)

    def cohort_summary(items):
        summary, groups = summarize(items)
        return summary | {
            "finding_groups": groups,
            "attributed_stage_records": sum(pointer["identity"] in lesson_uses
                                              for item in items for pointer in item["records"]),
            "attribution_missing": sum(pointer["identity"] not in lesson_uses
                                       for item in items for pointer in item["records"]),
        }
    return {
        "schema": SCHEMA,
        "status": "degraded" if telemetry.skipped is None or telemetry.skipped else "complete",
        "repository": repository,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "telemetry_entries_read": len(telemetry.entries),
        "telemetry_entries_skipped": telemetry.skipped,
        "summary": summary,
        "attribution": {
            "kind": "observational_non_causal",
            "cohorts": {name: cohort_summary(items) for name, items in cohort_subjects.items()},
        },
        "finding_groups": finding_groups,
        "subjects": subject_items,
    }


def dumps(repository: str, start: date, end: date, store_path: Path | str) -> str:
    return json.dumps(report(repository, start, end, store_path), sort_keys=True,
                      separators=(",", ":")) + "\n"
