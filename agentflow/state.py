"""Where agentflow keeps its own local state, and the one way to name a file inside it.

The state directory is operator configuration: ``AGENTFLOW_STATE``, defaulting to
``~/.agentflow``. Eleven modules used to read that variable and build their own paths from it,
so the rule for what may live where was written eleven times and enforced nowhere.

:func:`state_path` is the single way in. It resolves the root, joins the caller's segments, and
proves the result is still *inside* the root before handing it back — a segment that climbs out
with ``..``, or an absolute segment that ignores the root entirely, is refused rather than
followed. Callers pass fixed, code-authored segments today; the containment check is what makes
that a property of this module rather than a promise about every call site.
"""

from __future__ import annotations

import os
from pathlib import Path


class OutsideStateDirectory(ValueError):
    """A requested path escaped the state directory, so it was refused."""


def state_dir() -> Path:
    """agentflow's local state directory, honoring ``AGENTFLOW_STATE``. Fully resolved, so every
    path derived from it is compared against one canonical root."""
    configured = os.environ.get("AGENTFLOW_STATE") or "~/.agentflow"
    expanded = os.path.expanduser(configured)
    if not os.path.isabs(expanded) or ".." in expanded.split(os.sep):
        raise OutsideStateDirectory(
            "AGENTFLOW_STATE must be an absolute path with no relative path segments"
        )
    resolved = os.path.realpath(expanded)
    if not resolved.startswith(os.sep):
        raise OutsideStateDirectory("AGENTFLOW_STATE must resolve to an absolute path")
    return Path(resolved)


def state_path(*segments: str) -> Path:
    """One file or directory inside the state directory, named by fixed segments.

    Raises :class:`OutsideStateDirectory` if the segments would land anywhere else. The check is
    on the *resolved* path, so ``..`` and absolute segments are both caught rather than trusted.
    """
    root = state_dir()
    candidate = root.joinpath(*segments).resolve()
    if candidate != root and root not in candidate.parents:
        raise OutsideStateDirectory(f"{candidate} is not inside {root}")
    return candidate
