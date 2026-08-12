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
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from enum import Enum

from agentflow.coordinator.quota import QuotaFact, build_fact, epoch_seconds
from agentflow.coordinator.session import read_session
from agentflow.coordinator.store import default_store_path
from agentflow.coordinator.telemetry import (
    AttemptUsage, claude_usage, codex_usage, lead_codex_worker_usage)


class ProviderCause(str, Enum):
    """The typed reason a provider attempt ended, independent of the stage outcome."""

    NONE = "none"            # the attempt ran clean; the stage outcome decides completion
    CAPACITY = "capacity"    # a rate/quota limit with a reset — recoverable, waits for reset
    PERMANENT = "permanent"  # auth, billing, permission, or configuration — a human hold
    SERVER = "server"        # a server/transport failure — recoverable next cycle
    TIMEOUT = "timeout"      # the supervisor deadline fired — recoverable next cycle
    PROCESS = "process"      # exit status or signal without a typed cause — incomplete
    UNKNOWN = "unknown"      # nothing typed established a cause — bounded unknown interruption


class EndingReason(str, Enum):
    """*Which* condition ended the attempt, where the cause alone is too coarse to act on —
    a fact, not a policy (ADR 0030).

    ``ProviderCause.PERMANENT`` covers conditions with nothing in common but "a human has to
    act": a refused sign-in, a request the provider itself rejected, a configured spend ceiling,
    an environment that could not carry a session at all. They need different remediations, so
    the adapter preserves which one fired and the stage handoff picks its copy from it (issue
    #342). ``ProviderCause.TIMEOUT`` is the same shape once the per-stage turn cap joins the
    wall-clock deadline in it (#411): both are clock-class ends, but only one of them is fixed by
    giving the session more room, so the adapter names which ceiling stopped it. This is a sibling
    of ``cause``, never a replacement: ``classification()`` still reads ``cause`` alone, so no
    trigger changes category. An ending nothing typed named — including a synthesized non-provider
    observation — stays ``UNSPECIFIED`` so the handoff never prescribes a wrong remedy.
    """

    UNSPECIFIED = "unspecified"              # nothing typed named which condition it was
    ACCESS = "access"                        # sign-in, billing, plan, or permission refusal
    REJECTED_REQUEST = "rejected-request"    # the provider refused the request itself
    SPEND = "spend"                          # a configured cost ceiling stopped the run
    ENVIRONMENT = "environment"              # the shell never started, so nothing was attempted
    TURN_CAP = "turn-cap"                    # the per-stage turn ceiling cut the session off


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
    ending_reason: EndingReason = EndingReason.UNSPECIFIED  # which condition ended it, where the
                                                # cause alone is too coarse (permanent, clock-class)
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
    quota: QuotaFact | None = None              # the provider's five-hour utilization/reset fact, when the
                                                # stream reported one; the persisted Claude dispatch authority (#305)

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

# Which condition each typed trigger names, keyed by the same typed error type / result subtype /
# Codex account kind the cause tables use. A trigger absent from here stays ``UNSPECIFIED`` — a
# missing entry never invents a remediation. Every key belongs to exactly one cause (the cause
# tables above decide that), so the reason a key names is the reason of the cause it maps to.
_ENDING_REASONS = {
    # Claude assistant-error types.
    "authentication_failed": EndingReason.ACCESS,
    "authentication_error": EndingReason.ACCESS,
    "permission_error": EndingReason.ACCESS,
    "billing_error": EndingReason.ACCESS,
    "invalid_request": EndingReason.REJECTED_REQUEST,
    "invalid_request_error": EndingReason.REJECTED_REQUEST,
    "request_too_large": EndingReason.REJECTED_REQUEST,
    "not_found_error": EndingReason.REJECTED_REQUEST,
    # Claude terminal result subtypes.
    "error_max_budget_usd": EndingReason.SPEND,
    "error_max_turns": EndingReason.TURN_CAP,
    # Codex typed account kinds.
    "unauthenticated": EndingReason.ACCESS,
    "billing": EndingReason.ACCESS,
    "plan_required": EndingReason.ACCESS,
}


def _ending_reason(key) -> EndingReason:
    """The condition a typed trigger names, or ``UNSPECIFIED`` for anything else."""
    return _ENDING_REASONS.get(key, EndingReason.UNSPECIFIED)

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

# The stream message types this classifier types a *cause* from. Every other type (system init,
# user tool results, partial stream_event, telemetry, …) is preserved verbatim as unrecognized so
# nothing is silently dropped and an unknown shape stays fail-safe. Preserved is not unread: the
# dead-shell scan below reads the ``user`` tool results too, correlating each back to its own
# tool-use block (#386) — it just establishes no cause from any single event's type.
_CLAUDE_KNOWN_TYPES = {"assistant", "result", "rate_limit_event"}


def _claude_result_error(event: dict):
    """Classify the Agent SDK ``ResultMessage`` error surface.

    A result may retain ``subtype=success`` while reporting an API failure through
    ``is_error``/``api_error_status``/``errors``. Status is the only typed fact used here:
    429 is capacity, authentication/payment/permission statuses are permanent, and 5xx
    (including Anthropic's 529 overload) are server interruptions, and a status that types the
    failure always wins. A failure this surface cannot type is reported as unknown; the caller
    then falls back to the record's own ``subtype`` — also a typed field — and preserves the
    record verbatim if that names nothing either. Neither reads the prose.
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



def _claude_utilization_pct(info: dict):
    """The five-hour utilization percentage on a ``rate_limit_info``, normalized to 0..100.

    Claude Code reports ``utilization`` on the documented ``rate_limit_event.rate_limit_info``
    surface (docs/research/provider-interruption-signals.md). It may arrive as a 0..1 fraction
    or an already-scaled percentage; either is normalized here. Any other shape yields ``None``
    so a malformed field never becomes a fabricated reading."""
    value = info.get("utilization")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    pct = value * 100 if value <= 1 else value
    return pct if 0 <= pct <= 100 else None


def _claude_quota_fact(info: dict, observed_at: int) -> QuotaFact | None:
    """Build a validated five-hour quota fact from a ``rate_limit_info``, or ``None``. Extracted
    for every status (allowed and rejected alike): an ``allowed`` reading is exactly the fresh
    utilization the balancer needs to size headroom, not only the rejection that stops a run.

    A non-five-hour window never contributes: Claude Code's headless stream emits one window at a
    time (the one in warning — often ``seven_day``), so without this guard a seven_day reading would
    be mis-persisted as the five-hour authority (issue #309). An event that omits ``rateLimitType``
    is still admitted — that preserves the pre-existing contract (#305) — because ``build_fact``'s
    window check is the backstop: a seven_day reset lands ~7 days out, far outside the five-hour
    window an ``observed_at`` sits in, so it is rejected there anyway. The independent OAuth poll is
    the primary producer; this stream fact only corroborates it when a five-hour event arrives."""
    if info.get("rateLimitType") not in (None, "five_hour"):
        return None
    pct = _claude_utilization_pct(info)
    resets_at = epoch_seconds(info.get("resetsAt") or info.get("resets_at"))
    if pct is None or resets_at is None:
        return None
    return build_fact("claude", pct, resets_at, observed_at, "claude:rate_limit_event")


# --- the dead shell (issue #386) ---------------------------------------------------------
#
# A session whose shell cannot be *spawned* never reaches the work at all. That is not the
# agent failing at the task and not a provider condition; it is the environment refusing to
# carry a session, and it needs its own ending so the maintainer is not told the agent ran out
# of tries. The facts are already in the stream: the harness reports the refusal as the shell
# tool's own error result.

# The shell tool whose results are read here. Only a call that tries to *start a process*
# counts — a session that never asked for one cannot have had its shell refused.
_SHELL_TOOLS = frozenset({"Bash"})

# The harness's own exec-level start-failure line, anchored to its beginning. This is the
# harness reporting that it could not bring a process into existence — not model prose and not
# a command's own output, which `docs/research/provider-interruption-signals.md` forbids
# diagnosing from. The observed instance is the sandbox profile outgrowing the OS argument
# limit ("Could not start /bin/zsh: … exceed the OS exec argument limit (E2BIG)", ADR 0050),
# but any refusal to spawn the shell is the same ending, so the shape — not that one cause —
# is what is matched.
_SHELL_START_FAILURE = re.compile(
    r"\s*(?:could not|failed to|unable to|cannot)\s+(?:start|spawn|launch|execute)\b",
    re.IGNORECASE)


def _message_blocks(event) -> list:
    """The content blocks of a stream event's API message, or an empty list."""
    message = event.get("message") if isinstance(event, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _tool_result_text(block: dict) -> str:
    """A ``tool_result`` block's text, whether it carries a bare string or content blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part["text"] for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _shell_never_started(events) -> bool:
    """Whether this session asked for a shell and never got one — no command ever ran (#386).

    Two independent anchors, both required, so nothing here is diagnosed from loose keyword
    matching. Each shell result is correlated back to its own ``tool_use`` block, so only a real
    shell call is read; and *every* one of those results must be the harness's exec-level
    start-failure line. A single shell result that is anything else means a command did run, and
    a rejection after that is an ordinary adjustable one — the kind the shell crib teaches a
    session to work around — not a dead shell.
    """
    shell_calls = {
        block["id"] for event in events for block in _message_blocks(event)
        if isinstance(block, dict) and block.get("type") == "tool_use"
        and block.get("name") in _SHELL_TOOLS and isinstance(block.get("id"), str)}
    if not shell_calls:
        return False
    refused = False
    for event in events:
        for block in _message_blocks(event):
            if (not isinstance(block, dict) or block.get("type") != "tool_result"
                    or block.get("tool_use_id") not in shell_calls):
                continue
            if (block.get("is_error") is True
                    and _SHELL_START_FAILURE.match(_tool_result_text(block))):
                refused = True
            else:
                return False   # a shell result that is not a start failure — a command ran
    return refused


# The endings a dead shell may claim: the ones that would otherwise read as an ordinary
# incomplete or interrupted session. A provider that reported a typed condition of its own
# (capacity, permanent, server) keeps it — that is the real story, and a dead shell never
# overrides a fact the provider itself established.
_ENVIRONMENT_OVERRIDABLE = frozenset({
    ProviderCause.NONE, ProviderCause.PROCESS, ProviderCause.TIMEOUT, ProviderCause.UNKNOWN})


def classify_claude(events, *, exit_status=None, signal=None, timed_out=False,
                    partial_output="", family=None, process_alive=False,
                    has_end_fact=False, observed_at=None) -> ProviderObservation:
    """Extract facts from Claude's structured Agent SDK stream. A typed cause comes from the real
    stream shapes — an ``assistant`` message's error value, a rejected ``rate_limit_event``
    (with its ``resetsAt``), a terminal ``result.subtype`` failure, or every shell tool result in
    the session being the harness's own start failure — never an invented ``type:error`` event.
    A supervisor timeout or a non-zero exit without any of those is a recoverable process
    interruption, and a clean exit leaves the cause to the stage outcome — except over a session
    whose shell never started, which is a permanent environment fault however the process ended,
    because no attempt can reach the work (#386). The real ``assistant.message.content`` text and
    terminal result output are still parsed. When
    Claude returns a native ``structured_output``, its JSON object is the stage message; the
    accompanying ``result`` prose is only a human-readable summary and cannot satisfy the stage
    contract. Every unrecognized event or unshaped error is preserved verbatim so nothing is
    silently dropped (fail-safe)."""
    events = tuple(events)
    observed_at = int(observed_at if observed_at is not None else time.time())
    cause = ProviderCause.NONE
    ending_reason = EndingReason.UNSPECIFIED
    reset_at = None
    final_message = ""
    quota: QuotaFact | None = None
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
                        ending_reason = _ending_reason(error_type)
                else:
                    if cause is ProviderCause.NONE:
                        cause = ProviderCause.UNKNOWN
                    unrecognized.append(dict(event))  # an error we could not type — preserve it
        elif etype == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict):
                # The five-hour utilization/reset fact is the persisted dispatch authority (#305).
                # It is read on every status — an ``allowed`` reading is the fresh headroom the
                # balancer needs, not only the rejection that stops a run. The freshest wins.
                fact = _claude_quota_fact(info, observed_at)
                if fact is not None:
                    quota = fact
            rejected = (isinstance(info, dict)
                        and (info.get("status") == "rejected" or info.get("rejected") is True))
            if rejected:
                # A rejected rate limit is a real capacity interruption; an allowed one is
                # informational and establishes no cause.
                if cause is ProviderCause.NONE:
                    cause = ProviderCause.CAPACITY
                reset_at = epoch_seconds(info.get("resetsAt")) or reset_at
        elif etype == "result":
            result = event.get("result")
            structured_output = event.get("structured_output")
            if structured_output is not None:
                final_message = json.dumps(structured_output)
            elif isinstance(result, str):
                final_message = result
            subtype = event.get("subtype")
            failed_subtype = subtype if subtype and subtype != "success" else None
            subtype_cause = _CLAUDE_RESULT_CAUSES.get(failed_subtype)
            result_cause, has_result_error = _claude_result_error(event)
            if has_result_error:
                reason_key = None
                if result_cause is ProviderCause.UNKNOWN and subtype_cause is not None:
                    # A typed status keeps precedence: a session cut off *while* rate-limited is
                    # a capacity end, and capacity is the fact that decides when to retry. But a
                    # reported failure carrying no status at all leaves the subtype as the only
                    # typed fact on the record, and it names the ending — reading it is not
                    # diagnosing from prose. Without this the turn cap's own mapping below was
                    # unreachable by the very ending it was written for (#411).
                    result_cause, reason_key = subtype_cause, failed_subtype
                if (cause is ProviderCause.NONE
                        or (cause is ProviderCause.UNKNOWN
                            and result_cause is not ProviderCause.UNKNOWN)):
                    cause = result_cause
                    if reason_key is not None:
                        ending_reason = _ending_reason(reason_key)
                    else:
                        # The only permanent statuses this surface types (401/402/403) are all
                        # access refusals.
                        ending_reason = (EndingReason.ACCESS
                                         if result_cause is ProviderCause.PERMANENT
                                         else EndingReason.UNSPECIFIED)
                if result_cause is ProviderCause.UNKNOWN:
                    unrecognized.append(dict(event))
            elif failed_subtype:
                if subtype_cause is not None:
                    if cause is ProviderCause.NONE:
                        cause = subtype_cause
                        ending_reason = _ending_reason(failed_subtype)
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
    if cause in _ENVIRONMENT_OVERRIDABLE and _shell_never_started(events):
        # The environment could not carry a session: the agent never reached the work, so
        # neither a continuation nor a wait can help and the ending is a human hold (#386). It
        # stays a PERMANENT *cause* so the classification table and every branch reading it are
        # untouched; only the reason says this was the environment rather than the provider.
        cause = ProviderCause.PERMANENT
        ending_reason = EndingReason.ENVIRONMENT
    return ProviderObservation(
        cause=cause, ending_reason=ending_reason,
        reset_at=reset_at, exit_status=exit_status, signal=signal,
        timed_out=timed_out, final_message=final_message, partial_output=partial_output,
        events=events,
        unrecognized=tuple(unrecognized), family=family, process_alive=process_alive,
        has_end_fact=has_end_fact, usage=claude_usage(events), quota=quota)


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
    non-zero exit — remains a bounded unknown interruption unless the supervisor timed out.

    A Codex session that loses its shell is *not* recognized here: the exec JSON surface carries
    no typed tool-result fact to correlate a refusal back to a shell call, and its prose never
    diagnoses, so a Codex dead shell keeps today's classification (ADR 386)."""
    cause = ProviderCause.NONE
    ending_reason = EndingReason.UNSPECIFIED
    reset_at = None
    if account_fact is not None:
        kind = account_fact.get("kind")
        mapped = _CODEX_ACCOUNT_CAUSES.get(kind)
        if mapped is not None:
            cause = mapped
            ending_reason = _ending_reason(kind)
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
        cause=cause, ending_reason=ending_reason,
        reset_at=reset_at, exit_status=exit_status, signal=signal,
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

_SESSION_LEAD_MARKER = "\n## Session lead — benchmarked capability routing\n"
_SESSION_LEAD_OPENING = (
    "\nYou are the accountable Session lead. Do not write the implementation directly. Plan the work,\n"
    "delegate exploration, implementation, and fix work, verify every result, and ship only verified\n"
    "work. Fable is lead-only and is never a delegate target.\n")
_SESSION_LEAD_CLOSING = (
    "\nIf a worker from either provider fails to launch or dies on a provider error (rate limit, quota\n"
    "exhausted, API unreachable) rather than on the work itself, treat every rung from that provider in\n"
    "that ladder as unavailable for the rest of this session: re-enter at the first remaining-provider\n"
    "rung of the same ladder instead of retrying the failed provider; record the substitution in the final handoff.\n"
    "If that specific ladder has no rung from any other provider (a single-provider ladder — check the\n"
    "routes above; some areas mix providers only in one of their routes), do not invent a substitute\n"
    "model and do not silently do the work yourself: stop delegating in that area and hand back the\n"
    "provider failure by name in the final handoff instead of a result. This is separate from a failed\n"
    "verification — it is never a finding to re-delegate against.\n")
_SESSION_LEAD_START = re.compile(
    r"\n(?:\n(?:Claude|Codex) is currently unavailable[^\n]*\n(?:Exception:[^\n]*\n)?)*"
    + re.escape(_SESSION_LEAD_MARKER) + r"\Z")


class SessionLeadInputError(ValueError):
    """A durable session-lead input cannot be safely refreshed before launch."""


def _stage_result_schema(stage: str) -> dict | None:
    """The provider-neutral result contract a stage's terminal decision must match, or None
    for a code-writing stage that emits no structured decision. Intake, Review and the attack own
    their schemas (domain validation lives with their parsers); this seam only names which stage
    uses which, so no provider-specific schema detail leaks into coordinator policy."""
    if stage == "intake":
        from agentflow.intake import INTAKE_RESULT_SCHEMA
        return INTAKE_RESULT_SCHEMA
    if stage == "review":
        from agentflow.reviewer import REVIEW_VERDICT_SCHEMA
        return REVIEW_VERDICT_SCHEMA
    if stage == "attack":
        from agentflow.attack import ATTACK_RESULT_SCHEMA
        return ATTACK_RESULT_SCHEMA
    return None


def _durable_prompt(record) -> str:
    """Resolve a versioned provider-input envelope, falling back to the legacy raw prompt. A
    continuation carrying a recovery envelope (issue #225) appends those bounded durable facts so
    the fresh session resumes from them rather than replaying the identical base prompt."""
    prompt = _base_durable_prompt(record)
    if has_session_lead_provenance(record):
        prompt = _refresh_session_lead_contract(record, prompt)
    elif _SESSION_LEAD_MARKER in prompt:
        raise SessionLeadInputError(
            "session-lead input has an ambiguous Session lead contract boundary; retain the "
            "durable task brief and resubmit")
    envelope = getattr(record, "recovery_envelope", None)
    return f"{prompt}\n\n{envelope}" if envelope else prompt


def _refresh_session_lead_contract(record, prompt: str) -> str:
    """Keep a durable task brief while replacing its runtime-owned lead contract.

    The final generated contract has a stable opening, routing sections, and provider-failure
    footer across the native-helper and bounded-worker generations. A record that cannot prove
    that structure cannot be separated without risking user text, so it is refused before a
    provider process is started.
    """
    task_brief, _contract = split_terminal_session_lead_contract(prompt)
    from agentflow.pool_control import POOLS, pool_paused
    from agentflow.routing import routing
    from agentflow.runner import codex_spent_at_render
    return task_brief + routing.session_lead_instructions(
        record.stage, record.effort, parent_provider=record.pool,
        codex_spent=codex_spent_at_render(),
        unavailable_providers=frozenset(pool for pool in POOLS if pool_paused(pool)))


def split_terminal_session_lead_contract(prompt: str) -> tuple[str, str]:
    """Return a proven task brief and its complete terminal generated lead contract."""
    marker = prompt.rfind(_SESSION_LEAD_MARKER)
    if marker < 0:
        raise SessionLeadInputError(
            "session-lead input cannot be safely refreshed: expected a generated "
            "Session lead contract boundary; retain the durable task brief and resubmit")
    suffix = prompt[marker:]
    start = _SESSION_LEAD_START.search(prompt[:marker + len(_SESSION_LEAD_MARKER)])
    if (start is None or not suffix.startswith(_SESSION_LEAD_MARKER + _SESSION_LEAD_OPENING)
            or "\nRoutes (workers enter at the first rung; a banned model never takes that area's work):\n"
            not in suffix or "\nProvider launch identifiers: " not in suffix
            or not suffix.endswith(_SESSION_LEAD_CLOSING)):
        raise SessionLeadInputError(
            "session-lead input cannot be safely refreshed: expected a complete generated "
            "Session lead contract; retain the durable task brief and resubmit")
    return prompt[:start.start()], prompt[start.start():]


def validate_session_lead_input(record) -> None:
    """Refuse an ambiguous legacy session-lead record before it reserves provider capacity."""
    _durable_prompt(record)


def has_session_lead_provenance(record) -> bool:
    """Recognize current records and Codex native-helper records that predate #555.

    A marker embedded only in a raw prompt is not provenance: task text may contain the same
    heading, and replacing what follows it would silently turn that task text into policy.
    """
    return bool(getattr(record, "session_lead", False)
                or getattr(record, "native_helpers_marker", None))


def _base_durable_prompt(record) -> str:
    """Decode the durable prompt envelope without applying runtime launch policy."""
    raw = record.input_ptr or ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if (isinstance(payload, dict) and payload.get("format") == PROVIDER_INPUT_V1
            and isinstance(payload.get("prompt"), str)):
        return payload["prompt"]
    return raw


class ProviderArgv(list):
    """The provider CLI argv a launch runs, kept as a list for existing callers."""


class ClaudeProviderAdapter:
    """Launches a structured Claude session and observes its durable events + exit."""

    def __init__(self, prompt_of=None) -> None:
        self._prompt_of = prompt_of or _durable_prompt

    def command(self, record) -> list[str]:
        from agentflow.coordinator.profiles import profile_for
        from agentflow.runner import ClaudeRunner
        return ProviderArgv(ClaudeRunner().structured_argv(
            self._prompt_of(record), record.model, record.source,
            schema=_stage_result_schema(record.stage), profile=profile_for(record)))

    def observe(self, record) -> ProviderObservation:
        session = read_session(default_store_path(), record.launch_token)
        observation = classify_claude(
            session.events, exit_status=session.exit_status, signal=session.signal,
            timed_out=session.timed_out, partial_output=session.partial_output,
            family=record.family, process_alive=record.process_alive,
            has_end_fact=session.has_end_fact)
        # The lead (Claude `fable`) shells out to `codex exec` workers whose spend lands
        # nowhere unless it is merged in here, from the workers' own rollout files — never
        # self-reported by the lead (frozen decision 2, issue #516). One telemetry entry still
        # comes out of this observation; the worker totals just widen its usage.
        worker_costs = lead_codex_worker_usage(record)
        if worker_costs:
            usage = observation.usage
            observation = replace(observation, usage=replace(
                usage, model_costs=usage.model_costs + worker_costs))
        return observation

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
        return ProviderArgv(CodexRunner().structured_argv(
            self._prompt_of(record), record.model, record.source,
            schema=_stage_result_schema(record.stage), profile=profile_for(record)))

    def observe(self, record) -> ProviderObservation:
        session = read_session(default_store_path(), record.launch_token)
        if self._account_of:
            account = self._account_of(record)
        else:
            from agentflow.runner import CodexRunner
            account = CodexRunner().account_fact()
        observation = classify_codex(
            account_fact=account, exit_status=session.exit_status, signal=session.signal,
            timed_out=session.timed_out, partial_output=session.partial_output,
            events=session.events,
            family=record.family, process_alive=record.process_alive,
            has_end_fact=session.has_end_fact)
        worker_costs = lead_codex_worker_usage(record)
        if worker_costs:
            observation = replace(observation, usage=replace(
                observation.usage, model_costs=observation.usage.model_costs + worker_costs))
        return observation

    def verify(self, record, obs) -> bool:
        return False


_ADAPTERS = {"claude": ClaudeProviderAdapter, "codex": CodexProviderAdapter}


def _dormant_provider_command(record) -> list[str]:
    """A no-op provider that starts and exits for a bare record with no prompt (ADR 0030)."""
    return ProviderArgv([sys.executable, "-c", ""])


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
