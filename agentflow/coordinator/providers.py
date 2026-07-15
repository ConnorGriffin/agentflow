"""The two provider adapters extract facts; they never decide policy (ADR 0030).

Claude and Codex are the only real adapters at the provider seam. They preserve process
identity and liveness, structured events with an opaque copy of unrecognized fields, exit
status or signal and whether the supervisor timed out, the typed provider cause, and any
partial output and captured final message. They return that :class:`ProviderObservation`;
the coordinator alone turns it into a wait, hold, or completion.

The classification is pure over already-parsed facts, so a fixture stream or a fixture
account fact exercises every supported cause without spawning a CLI
(see tests/test_coordinator_providers.py). The constraint that Codex may only trust its
typed account/rate-limit surface — never `codex exec --json` prose — is enforced here, not
by a Codex-specific policy in the coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProviderCause(str, Enum):
    """The typed reason a provider attempt ended, independent of the stage outcome."""

    NONE = "none"            # the attempt ran clean; the stage outcome decides completion
    CAPACITY = "capacity"    # a rate/quota limit with a reset — recoverable, waits for reset
    PERMANENT = "permanent"  # auth, billing, permission, or configuration — a human hold
    SERVER = "server"        # a server/transport failure — recoverable next cycle
    TIMEOUT = "timeout"      # the supervisor deadline fired — recoverable next cycle
    PROCESS = "process"      # exit status or signal without a typed cause — incomplete
    UNKNOWN = "unknown"      # nothing typed established a cause — bounded unknown interruption


# How each typed cause maps to the coordinator's outcome-first classification label. The
# coordinator, not the adapter, applies the precedence; this is only the vocabulary bridge.
_CLASSIFICATION = {
    ProviderCause.NONE: "incomplete",
    ProviderCause.CAPACITY: "recoverable",
    ProviderCause.PERMANENT: "permanent",
    ProviderCause.SERVER: "recoverable",
    ProviderCause.TIMEOUT: "recoverable",
    ProviderCause.PROCESS: "incomplete",
    ProviderCause.UNKNOWN: "unknown",
}


@dataclass(frozen=True)
class ProviderObservation:
    """Everything one attempt preserved. Opaque to policy beyond ``cause``/``reset_at``."""

    cause: ProviderCause = ProviderCause.UNKNOWN
    reset_at: int | None = None                 # capacity reset → the coordinator's eligible_at
    exit_status: int | None = None
    signal: int | None = None
    timed_out: bool = False
    final_message: str = ""
    partial_output: str = ""
    events: tuple[dict, ...] = ()               # the structured provider events, preserved
    unrecognized: tuple[dict, ...] = ()         # an opaque copy of fields we did not model
    family: str | None = None
    process_alive: bool = False

    def classification(self) -> str:
        """The coordinator's provider label (recoverable | permanent | incomplete | unknown)."""
        return _CLASSIFICATION[self.cause]


# The Claude event subtypes we understand. Anything else is preserved as unrecognized and
# leaves the cause unknown rather than being guessed at.
_CLAUDE_CAUSES = {
    "capacity": ProviderCause.CAPACITY,
    "rate_limit": ProviderCause.CAPACITY,
    "authentication": ProviderCause.PERMANENT,
    "billing": ProviderCause.PERMANENT,
    "permission": ProviderCause.PERMANENT,
    "configuration": ProviderCause.PERMANENT,
    "server": ProviderCause.SERVER,
    "transport": ProviderCause.SERVER,
}
_CLAUDE_KNOWN_TYPES = {"assistant", "result", "error"}


def classify_claude(events, *, exit_status=None, signal=None, timed_out=False,
                    family=None, process_alive=False) -> ProviderObservation:
    """Extract facts from Claude's structured stream. A recognized ``error`` subtype gives a
    typed cause; a supervisor timeout or a non-zero exit without one is a recoverable process
    interruption; a clean exit leaves the cause to the stage outcome. Unrecognized event
    fields are preserved verbatim so nothing is silently dropped."""
    events = tuple(events)
    cause = ProviderCause.NONE
    reset_at = None
    final_message = ""
    unrecognized: list[dict] = []
    for event in events:
        etype = event.get("type")
        if etype == "assistant":
            final_message = event.get("text", final_message)
        elif etype == "result":
            final_message = event.get("final_message", final_message)
        elif etype == "error":
            mapped = _CLAUDE_CAUSES.get(event.get("subtype"))
            if mapped is not None:
                cause = mapped
                if mapped is ProviderCause.CAPACITY:
                    reset_at = event.get("reset_at", reset_at)
            else:
                cause = ProviderCause.UNKNOWN
                unrecognized.append(dict(event))
        if etype not in _CLAUDE_KNOWN_TYPES:
            unrecognized.append(dict(event))
    if cause is ProviderCause.NONE:
        if timed_out:
            cause = ProviderCause.TIMEOUT
        elif signal is not None or (exit_status not in (None, 0)):
            cause = ProviderCause.PROCESS
    return ProviderObservation(
        cause=cause, reset_at=reset_at, exit_status=exit_status, signal=signal,
        timed_out=timed_out, final_message=final_message, events=events,
        unrecognized=tuple(unrecognized), family=family, process_alive=process_alive)


# The typed Codex account/rate-limit facts (from the app-server surface or a typed companion
# query). Only these establish a cause; `codex exec --json` prose never does.
_CODEX_ACCOUNT_CAUSES = {
    "rate_limited": ProviderCause.CAPACITY,
    "capacity": ProviderCause.CAPACITY,
    "unauthenticated": ProviderCause.PERMANENT,
    "billing": ProviderCause.PERMANENT,
    "plan_required": ProviderCause.PERMANENT,
}


def classify_codex(*, account_fact=None, exit_status=None, signal=None, timed_out=False,
                   final_message="", family=None, process_alive=False) -> ProviderObservation:
    """Extract facts from a Codex attempt. Only a typed ``account_fact`` (from the account/
    rate-limit surface) may establish capacity vs. a permanent plan problem; the model's
    prose is captured as ``final_message`` but never diagnoses. An untyped failure — even a
    non-zero exit — remains a bounded unknown interruption unless the supervisor timed out."""
    cause = ProviderCause.NONE
    reset_at = None
    if account_fact is not None:
        mapped = _CODEX_ACCOUNT_CAUSES.get(account_fact.get("kind"))
        if mapped is not None:
            cause = mapped
            if mapped is ProviderCause.CAPACITY:
                reset_at = account_fact.get("reset_at")
    if cause is ProviderCause.NONE:
        if timed_out:
            cause = ProviderCause.TIMEOUT
        elif signal is not None or (exit_status not in (None, 0)):
            cause = ProviderCause.UNKNOWN  # a bare Codex exit is never diagnostic (ADR 0030)
    return ProviderObservation(
        cause=cause, reset_at=reset_at, exit_status=exit_status, signal=signal,
        timed_out=timed_out, final_message=final_message, family=family,
        process_alive=process_alive)
