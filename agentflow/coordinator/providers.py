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

import json
import sys
from dataclasses import dataclass
from enum import Enum

from agentflow.coordinator.session import read_session
from agentflow.coordinator.store import default_store_path
from agentflow.coordinator.telemetry import AttemptUsage, claude_usage, codex_usage


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
    has_end_fact: bool = False                  # the supervisor published a durable end fact for this
                                                # attempt — the provider family ended on its own, not
                                                # with the daemon (the restart-resume discriminator)
    usage: AttemptUsage = AttemptUsage()        # normalized spend for this attempt (tokens/cost/turns);
                                                # empty when the stream reported none (never zero-by-assumption)

    def classification(self) -> str:
        """The coordinator's provider label (recoverable | permanent | incomplete | unknown)."""
        return _CLASSIFICATION[self.cause]


# The Anthropic API error `type` values an `assistant` message may carry (assistant.error),
# mapped to our typed causes. These are the real SDK/API error types — not invented `type:error`
# stream events. A capacity (rate limit) is recoverable; overloaded/api are transient server
# conditions; auth/permission/billing/not-found/invalid-request are permanent human holds.
_CLAUDE_ERROR_CAUSES = {
    # Agent SDK's SDKAssistantMessageError string union.
    "rate_limit": ProviderCause.CAPACITY,
    "authentication_failed": ProviderCause.PERMANENT,
    "invalid_request": ProviderCause.PERMANENT,
    "server_error": ProviderCause.SERVER,
    "max_output_tokens": ProviderCause.PROCESS,
    # API error objects retained for forward-compatible structured observations.
    "rate_limit_error": ProviderCause.CAPACITY,
    "overloaded_error": ProviderCause.SERVER,
    "api_error": ProviderCause.SERVER,
    "timeout_error": ProviderCause.SERVER,
    "authentication_error": ProviderCause.PERMANENT,
    "permission_error": ProviderCause.PERMANENT,
    "billing_error": ProviderCause.PERMANENT,
    "not_found_error": ProviderCause.PERMANENT,
    "invalid_request_error": ProviderCause.PERMANENT,
    "request_too_large": ProviderCause.PERMANENT,
}

# Terminal `result.subtype` failures (SDKResultMessage). `success` is a clean end; the error
# subtypes are a process-level interruption that no typed provider cause explains, so they map to
# the recoverable/incomplete PROCESS cause rather than being guessed at as capacity or permanent.
# ``error_max_turns`` is now a real per-stage ceiling (ADR 0044): hitting it is a recoverable
# TIMEOUT-class end, the same class as the wall-clock deadline, not an incomplete PROCESS end.
_CLAUDE_RESULT_CAUSES = {
    "error_max_turns": ProviderCause.TIMEOUT,
    "error_during_execution": ProviderCause.PROCESS,
    "error_max_budget_usd": ProviderCause.PERMANENT,
    "error_max_structured_output_retries": ProviderCause.PROCESS,
}

# The stream message types this classifier actively models. Every other type (system init, user
# tool results, partial stream_event, telemetry, …) is preserved verbatim as unrecognized so
# nothing is silently dropped and an unknown shape stays fail-safe.
_CLAUDE_KNOWN_TYPES = {"assistant", "result", "rate_limit_event"}


def _claude_result_error(event: dict):
    """Classify the Agent SDK ``ResultMessage`` error surface.

    A result may retain ``subtype=success`` while reporting an API failure through
    ``is_error``/``api_error_status``/``errors``. Status is the only typed fact used here:
    429 is capacity, authentication/payment/permission statuses are permanent, and 5xx
    (including Anthropic's 529 overload) are server interruptions. Other shaped failures stay
    unknown and are preserved by the caller rather than guessed from prose.
    """
    status = event.get("api_error_status")
    try:
        status = int(status) if not isinstance(status, bool) else None
    except (TypeError, ValueError):
        status = None
    errors = event.get("errors")
    has_errors = isinstance(errors, (list, tuple)) and bool(errors)
    has_error = event.get("is_error") is True or status is not None or has_errors
    if not has_error:
        return None, False
    if status == 429:
        return ProviderCause.CAPACITY, True
    if status in {401, 402, 403}:
        return ProviderCause.PERMANENT, True
    if status is not None and 500 <= status <= 599:
        return ProviderCause.SERVER, True
    return ProviderCause.UNKNOWN, True


def _claude_assistant_text(event: dict) -> str | None:
    """Extract text from Claude's ``assistant.message.content`` blocks (SDKAssistantMessage)."""
    message = event.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return None
    text = [
        block["text"] for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(text) if text else None


def _claude_error_type(event: dict):
    """The Anthropic error ``type`` carried on an ``assistant`` message's error value, if any.
    The error may sit directly on the event (``assistant.error``) or inside its API message
    (``assistant.message.error``); both are the SDK's assistant-turn error surface."""
    error = event.get("error")
    if error is None:
        message = event.get("message")
        error = message.get("error") if isinstance(message, dict) else None
    if error is None:
        return None, False
    if isinstance(error, str):
        return error, True
    if isinstance(error, dict):
        return error.get("type"), True
    return None, True  # a truthy but unshaped error — present, but no typed cause


def _epoch(value):
    """Coerce a ``resetsAt`` (Unix seconds or ISO-8601) into epoch seconds, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        from datetime import datetime
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def classify_claude(events, *, exit_status=None, signal=None, timed_out=False,
                    partial_output="", family=None, process_alive=False,
                    has_end_fact=False) -> ProviderObservation:
    """Extract facts from Claude's structured Agent SDK stream. A typed cause comes from the real
    stream shapes — an ``assistant`` message's error value, a rejected ``rate_limit_event``
    (with its ``resetsAt``), or a terminal ``result.subtype`` failure — never an invented
    ``type:error`` event. A supervisor timeout or a non-zero exit without any of those is a
    recoverable process interruption; a clean exit leaves the cause to the stage outcome. The
    real ``assistant.message.content`` text and terminal result output are still parsed. When
    Claude returns a native ``structured_output``, its JSON object is the stage message; the
    accompanying ``result`` prose is only a human-readable summary and cannot satisfy the stage
    contract. Every unrecognized event or unshaped error is preserved verbatim so nothing is
    silently dropped (fail-safe)."""
    events = tuple(events)
    cause = ProviderCause.NONE
    reset_at = None
    final_message = ""
    unrecognized: list[dict] = []
    for event in events:
        etype = event.get("type")
        if etype == "assistant":
            text = _claude_assistant_text(event)
            if text is not None:
                final_message = text
            error_type, has_error = _claude_error_type(event)
            if has_error:
                mapped = _CLAUDE_ERROR_CAUSES.get(error_type)
                if mapped is not None:
                    if cause is ProviderCause.NONE:
                        cause = mapped
                else:
                    if cause is ProviderCause.NONE:
                        cause = ProviderCause.UNKNOWN
                    unrecognized.append(dict(event))  # an error we could not type — preserve it
        elif etype == "rate_limit_event":
            info = event.get("rate_limit_info")
            rejected = (isinstance(info, dict)
                        and (info.get("status") == "rejected" or info.get("rejected") is True))
            if rejected:
                # A rejected rate limit is a real capacity interruption; an allowed one is
                # informational and establishes no cause.
                if cause is ProviderCause.NONE:
                    cause = ProviderCause.CAPACITY
                reset_at = _epoch(info.get("resetsAt")) or reset_at
        elif etype == "result":
            result = event.get("result")
            structured_output = event.get("structured_output")
            if structured_output is not None:
                final_message = json.dumps(structured_output)
            elif isinstance(result, str):
                final_message = result
            subtype = event.get("subtype")
            result_cause, has_result_error = _claude_result_error(event)
            if has_result_error:
                if (cause is ProviderCause.NONE
                        or (cause is ProviderCause.UNKNOWN
                            and result_cause is not ProviderCause.UNKNOWN)):
                    cause = result_cause
                if result_cause is ProviderCause.UNKNOWN:
                    unrecognized.append(dict(event))
            elif subtype and subtype != "success":
                mapped = _CLAUDE_RESULT_CAUSES.get(subtype)
                if mapped is not None:
                    if cause is ProviderCause.NONE:
                        cause = mapped
                else:
                    if cause is ProviderCause.NONE:
                        cause = ProviderCause.UNKNOWN
                    unrecognized.append(dict(event))  # an unmodeled terminal failure — preserve
        if etype not in _CLAUDE_KNOWN_TYPES:
            unrecognized.append(dict(event))
    if cause is ProviderCause.NONE:
        if timed_out:
            cause = ProviderCause.TIMEOUT
        elif signal is not None or (exit_status not in (None, 0)):
            cause = ProviderCause.PROCESS
    return ProviderObservation(
        cause=cause, reset_at=reset_at, exit_status=exit_status, signal=signal,
        timed_out=timed_out, final_message=final_message, partial_output=partial_output,
        events=events,
        unrecognized=tuple(unrecognized), family=family, process_alive=process_alive,
        has_end_fact=has_end_fact, usage=claude_usage(events))


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
                   final_message="", partial_output="", events=(), family=None,
                   process_alive=False, has_end_fact=False) -> ProviderObservation:
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
    events = tuple(events)
    for event in events:
        item = event.get("item") if isinstance(event, dict) else None
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message"):
            final_message = item.get("text", final_message)
    return ProviderObservation(
        cause=cause, reset_at=reset_at, exit_status=exit_status, signal=signal,
        timed_out=timed_out, final_message=final_message, partial_output=partial_output,
        events=events, unrecognized=events, family=family, process_alive=process_alive,
        has_end_fact=has_end_fact, usage=codex_usage(events))


# --- the two production provider adapters (ADR 0030) ------------------------------------
#
# Each adapter builds the real structured-session command for a launched attempt and, once the
# family ends, reconstructs the full observation from that attempt's durable session artifacts
# (`agentflow.coordinator.session`) through the pure classifiers above. They extract facts
# only: `verify` always returns False because provider success can never stand in for a stage
# outcome — that check belongs to the stage adapter (ADR 0030 completion locality).

PROVIDER_INPUT_V1 = "agentflow-provider-input-v1"


def _stage_result_schema(stage: str) -> dict | None:
    """The provider-neutral result contract a stage's terminal decision must match, or None
    for a code-writing stage that emits no structured decision. Intake and Review own their
    schemas (domain validation lives with their parsers); this seam only names which stage
    uses which, so no provider-specific schema detail leaks into coordinator policy."""
    if stage == "intake":
        from agentflow.intake import INTAKE_RESULT_SCHEMA
        return INTAKE_RESULT_SCHEMA
    if stage == "review":
        from agentflow.reviewer import REVIEW_VERDICT_SCHEMA
        return REVIEW_VERDICT_SCHEMA
    return None


def _durable_prompt(record) -> str:
    """Resolve a versioned provider-input envelope, falling back to the legacy raw prompt. A
    continuation carrying a recovery envelope (issue #225) appends those bounded durable facts so
    the fresh session resumes from them rather than replaying the identical base prompt."""
    raw = record.input_ptr or ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        prompt = raw
    else:
        if (isinstance(payload, dict) and payload.get("format") == PROVIDER_INPUT_V1
                and isinstance(payload.get("prompt"), str)):
            prompt = payload["prompt"]
        else:
            prompt = raw
    envelope = getattr(record, "recovery_envelope", None)
    return f"{prompt}\n\n{envelope}" if envelope else prompt


class ClaudeProviderAdapter:
    """Launches a structured Claude session and observes its durable events + exit."""

    def __init__(self, prompt_of=None) -> None:
        self._prompt_of = prompt_of or _durable_prompt

    def command(self, record) -> list[str]:
        from agentflow.coordinator.profiles import profile_for
        from agentflow.runner import ClaudeRunner
        return ClaudeRunner().structured_argv(
            self._prompt_of(record), record.model, record.source,
            schema=_stage_result_schema(record.stage), profile=profile_for(record))

    def observe(self, record) -> ProviderObservation:
        session = read_session(default_store_path(), record.launch_token)
        return classify_claude(
            session.events, exit_status=session.exit_status, signal=session.signal,
            timed_out=session.timed_out, partial_output=session.partial_output,
            family=record.family, process_alive=record.process_alive,
            has_end_fact=session.has_end_fact)

    def verify(self, record, obs) -> bool:
        return False


class CodexProviderAdapter:
    """Launches a structured Codex session; classifies only from the typed account fact and the
    exit status — never the `codex exec --json` prose, which is preserved but never diagnoses."""

    def __init__(self, prompt_of=None, account_of=None) -> None:
        self._prompt_of = prompt_of or _durable_prompt
        self._account_of = account_of

    def command(self, record) -> list[str]:
        from agentflow.coordinator.profiles import profile_for
        from agentflow.runner import CodexRunner
        return CodexRunner().structured_argv(
            self._prompt_of(record), record.model, record.source,
            schema=_stage_result_schema(record.stage), profile=profile_for(record))

    def observe(self, record) -> ProviderObservation:
        session = read_session(default_store_path(), record.launch_token)
        if self._account_of:
            account = self._account_of(record)
        else:
            from agentflow.runner import CodexRunner
            account = CodexRunner().account_fact()
        return classify_codex(
            account_fact=account, exit_status=session.exit_status, signal=session.signal,
            timed_out=session.timed_out, partial_output=session.partial_output,
            events=session.events,
            family=record.family, process_alive=record.process_alive,
            has_end_fact=session.has_end_fact)

    def verify(self, record, obs) -> bool:
        return False


_ADAPTERS = {"claude": ClaudeProviderAdapter, "codex": CodexProviderAdapter}


def _dormant_provider_command(record) -> list[str]:
    """A no-op provider that starts and exits for a bare record with no prompt (ADR 0030)."""
    return [sys.executable, "-c", ""]


def provider_command(record) -> list[str]:
    """The real provider argv for a launched attempt, dispatched on its pool. A record with no
    durable input pointer has no prompt to run, so it falls back to the no-op provider: the
    path is fully wired for real Claude/Codex sessions. An unknown pool never reaches a launch
    (it has no permit ledger)."""
    adapter = _ADAPTERS.get(record.pool)
    if adapter is None or not record.input_ptr or not record.source:
        return _dormant_provider_command(record)
    return adapter().command(record)


class ProviderObserver:
    """The coordinator's default stage adapter: it reconstructs a launched attempt's
    observation through the pool's provider adapter and never verifies a stage outcome (that
    is the live stage's job). This is what makes a bare ``Coordinator()`` the real wired path
    rather than a stub — a launched Claude or Codex family is observed from its durable
    artifacts, not guessed at."""

    def observe(self, record) -> ProviderObservation:
        adapter = _ADAPTERS.get(record.pool)
        if adapter is None:
            return ProviderObservation()
        return adapter().observe(record)

    def verify(self, record, obs) -> bool:
        return False
