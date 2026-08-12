"""Validate checked-in Evidence producer fixtures.

Run ``uv run python -m agentflow.evidence_contract docs/evidence`` in producer
repositories before they submit an envelope to the Evidence interface.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from agentflow.evidence import (FAILURE_CLASSES, VALIDATION_STATES, AuthorityPointer, EvidenceError,
                                Observation, SubjectRevision)

_OBSERVATION_FIELDS = {"observation_id", "subject", "failure_class", "validation_state",
                       "signature_digest", "normalizer_version", "source", "observed_at",
                       "reviewed_parent_revision", "fixer_revision"}
_FORBIDDEN = {"prompt", "transcript", "source_body", "secret", "finding", "summary",
              "grounding", "payload", "excerpt", "body", "text"}


def _observation(value: dict) -> Observation:
    if not isinstance(value, dict) or set(value) - _OBSERVATION_FIELDS or set(value) & _FORBIDDEN:
        raise EvidenceError("envelope admits an unsupported or unredacted field")
    subject = value["subject"]
    source = value["source"]
    return Observation(value["observation_id"], SubjectRevision(**subject), value["failure_class"],
                       value["validation_state"], value["signature_digest"],
                       value["normalizer_version"], AuthorityPointer(**source), value["observed_at"],
                       value.get("reviewed_parent_revision", ""), value.get("fixer_revision", ""))


def validate_fixtures(directory: Path) -> None:
    contract = json.loads((directory / "contract-v1.json").read_text())
    if contract != {"version": 1, "envelope": "observation", "allowed_fields": sorted(_OBSERVATION_FIELDS),
                    "failure_classes": sorted(FAILURE_CLASSES),
                    "validation_states": sorted(VALIDATION_STATES)}:
        raise EvidenceError("unrecognized evidence contract")
    for fixture in sorted(directory.glob("*.json")):
        if fixture.name == "contract-v1.json": continue
        body = json.loads(fixture.read_text())
        if fixture.name.startswith("negative-"):
            try: _observation(body)
            except EvidenceError: continue
            raise EvidenceError(f"negative fixture was admitted: {fixture.name}")
        _observation(body)


def main() -> int:
    try:
        validate_fixtures(Path(sys.argv[1] if len(sys.argv) == 2 else "docs/evidence"))
    except (EvidenceError, OSError, json.JSONDecodeError) as exc:
        print(f"evidence contract invalid: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
