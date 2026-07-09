"""The trust ratchet (ADR 0007/0008) — a repo's correction rate, so autonomy can be
loosened when earned and tightened when it regresses.

Each terminal *autonomous* outcome is recorded per repo. `correction_rate` is the
fraction that needed a human or a revise (parked / merge-after-revise); a low,
sustained rate over enough samples is the signal to loosen the repo's profile (the
dashboard's "ready to loosen?" cue, ADR 0010). The compute functions are pure — the
test surface; the store is a small JSON file (GitHub remains the system of record).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE = Path(os.environ.get("AGENTFLOW_STATE",
             os.path.expanduser("~/.agentflow"))) / "ratchet.json"

CLEAN_MERGE = "clean_merge"
CORRECTIONS = {"merge_after_revise", "parked"}
_MAX = 200  # bounded history per repo


def correction_rate(outcomes: list[str]) -> float:
    """Fraction of outcomes that needed correction (a human or a revise). Pure."""
    if not outcomes:
        return 0.0
    return sum(1 for o in outcomes if o in CORRECTIONS) / len(outcomes)


def ready_to_loosen(outcomes: list[str], min_samples: int = 10, threshold: float = 0.1) -> bool:
    """True once there are enough recent samples AND their correction rate is low.
    Never fires on a thin history — trust is earned over a track record. Pure."""
    if len(outcomes) < min_samples:
        return False
    return correction_rate(outcomes[-min_samples:]) <= threshold


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def record(repo: str, outcome: str, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load(path)
    hist = (data.get(repo) or [])[-(_MAX - 1):]
    hist.append(outcome)
    data[repo] = hist
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def status(repo: str, path: Path = STATE) -> dict:
    hist = _load(path).get(repo) or []
    return {"repo": repo, "samples": len(hist),
            "correction_rate": round(correction_rate(hist[-20:]), 3),
            "ready_to_loosen": ready_to_loosen(hist)}
