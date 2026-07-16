"""The Build tracer rollout — one durable, drain-safe switch between legacy and coordinated
Build (ADR 0028/0030, issue #103).

Moving Build behind the session coordinator is not a flag flip. A live legacy build, a
dispatch claim, a PID marker, or an ambiguous worktree that predates the coordinator could be
mistaken for coordinator-owned work, so the switch is a *drain*: it stops new legacy provider
sessions, lets the ones already running finish, and only enters coordinated Build-only
behavior once nothing current-format is left to confuse for a continuation record. Rollback is
the same shape in reverse — it stops new coordinator submissions but keeps reconciling the
records that still own work, and legacy launching cannot resume while any of them do.

Only the operator's **desired mode** is durable; it survives daemon restart in one small JSON
file under the state directory. The **phase** is re-derived every cycle from that durable mode
plus what the world actually shows, so a restart mid-drain simply re-evaluates and continues
draining. That derivation is the whole safety property: in every combination of mode and
evidence, at most one of legacy launching and coordinated submission is enabled — a cycle can
never launch both legacy and coordinator-owned provider work.

The evidence itself (live legacy sessions, claims, dirty worktrees, PID markers) is gathered
by the caller and passed in, so this module is a pure state machine over durable intent plus
observed facts — exercised directly, without a daemon or GitHub.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from agentflow.coordinator.store import state_dir

# The three phases a cycle acts on. `draining` launches nothing new in either direction.
LEGACY = "legacy"
DRAINING = "draining"
COORDINATED = "coordinated"

# The durable desired mode the operator sets; the phase is derived from it each cycle.
MODE_LEGACY = "legacy"
MODE_COORDINATED = "coordinated"
_MODES = {MODE_LEGACY, MODE_COORDINATED}


@dataclass(frozen=True)
class Phase:
    """The phase a cycle should act on, plus the evidence still holding a drain open. Exactly
    one of :attr:`launch_legacy` and :attr:`submit_coordinated` is ever true; ``draining``
    makes both false so no new provider work of either kind starts this cycle."""

    name: str
    blocked_by: tuple[str, ...] = field(default_factory=tuple)

    @property
    def launch_legacy(self) -> bool:
        """Whether the legacy Build path may start new provider sessions this cycle."""
        return self.name == LEGACY

    @property
    def submit_coordinated(self) -> bool:
        """Whether Build may submit and admit coordinator-owned work this cycle."""
        return self.name == COORDINATED


class Rollout:
    """The durable legacy↔coordinated switch. ``mode`` is the operator's persisted intent;
    ``phase`` derives what a cycle may do from that intent and the observed world."""

    def __init__(self, path: Path | str | None = None, *, log=None) -> None:
        self.path = Path(path) if path is not None else state_dir() / "rollout.json"
        self._log = log or (lambda _line: None)

    # --- durable desired mode -----------------------------------------------------------

    @property
    def mode(self) -> str:
        """The operator's durable desired mode. A missing, partial, or corrupt file reads as
        legacy — the original behavior — and the phase derivation still refuses to resume
        legacy launching while coordinator records own work, so this default is fail-closed."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return MODE_LEGACY
        mode = data.get("mode") if isinstance(data, dict) else None
        return mode if mode in _MODES else MODE_LEGACY

    def request_coordinated(self) -> None:
        """Ask to roll Build forward onto the coordinator. Durably records the intent; the
        drain to coordinated only completes once the legacy evidence clears (see :meth:`phase`)."""
        self._write(MODE_COORDINATED)

    def request_legacy(self) -> None:
        """Ask to roll Build back to the legacy path. Durably records the intent; legacy
        launching stays suspended until no coordinator record still owns work (see :meth:`phase`)."""
        self._write(MODE_LEGACY)

    # --- derived phase ------------------------------------------------------------------

    def phase(self, *, legacy_evidence=(), coordinator_active: bool = False) -> Phase:
        """Derive this cycle's phase from the durable mode and the observed world.

        - Rolling forward (``coordinated``) waits while any current-format session, claim, PID
          marker, or ambiguous worktree remains: the phase is ``draining`` and names that
          evidence rather than guessing or clearing it. Only a clean world reaches
          ``coordinated``.
        - Rolling back (``legacy``) waits while any coordinator record still owns work: the
          phase is ``draining`` so legacy launching cannot resume and steal a record's retry.
          Only once no coordinator record owns work does it reach ``legacy``.
        """
        evidence = tuple(legacy_evidence)
        if self.mode == MODE_COORDINATED:
            if evidence:
                self._log("rollout: draining to coordinated — waiting on "
                          + "; ".join(evidence))
                return Phase(DRAINING, evidence)
            return Phase(COORDINATED)
        if coordinator_active:
            self._log("rollout: draining to legacy — coordinator records still own work")
            return Phase(DRAINING, ("coordinator records still own work",))
        return Phase(LEGACY)

    # --- durable write ------------------------------------------------------------------

    def _write(self, mode: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        tmp.write_text(json.dumps({"mode": mode}))
        os.replace(tmp, self.path)
