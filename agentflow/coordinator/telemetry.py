"""Per-attempt spend telemetry — normalized at the provider seam, persisted durably.

Every provider event stream already carries usage (Claude reports input/cache/output
tokens, a turn count, a duration, and a harness-computed cost; Codex reports per-turn
token usage). This module turns those raw provider shapes into one normalized
:class:`AttemptUsage` behind the provider adapters, and the coordinator stamps a durable
:class:`AttemptTelemetry` entry for every attempt that ends — keyed by the attempt's
launch token, so a daemon restart that re-observes the same ended family overwrites the
same entry instead of double-counting (persisted exactly once per attempt).

The durable entries survive the record they describe: a stage record retires and is
re-used, but its attempts' telemetry stays on disk under ``coordinator/telemetry``, so the
bounded :func:`project` aggregate (totals and missing-data counts by stage/model/outcome)
answers cost-per-successful-stage questions long after the records themselves have moved on.

Only numbers and stage identity are ever persisted — no prompt, no provider message text,
no event bodies. Missing usage stays explicit as ``None``; it is never assumed to be zero,
because an attempt that produced no usage (an abort before any turn) is a real attempt whose
spend is genuinely unknown, not free.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from uuid import uuid4

# The usage sub-object keys each provider models. Everything else in a provider's usage
# object is preserved verbatim under ``AttemptUsage.unrecognized`` so a new provider field is
# never silently dropped — but only the usage object is read, so no message content can leak.
_CLAUDE_USAGE_KEYS = {
    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"}
_CODEX_USAGE_KEYS = {
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}


@dataclass(frozen=True)
class ModelCost:
    """One provider-reported model's attributed spend within an attempt."""

    model: str
    cost_usd: float | None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    @property
    def tokens(self) -> int | None:
        values = (
            self.input_tokens, self.cached_input_tokens, self.cache_creation_tokens,
            self.output_tokens, self.reasoning_output_tokens,
        )
        return sum(value for value in values if value is not None) \
            if any(value is not None for value in values) else None


@dataclass(frozen=True)
class AttemptUsage:
    """One attempt's normalized spend, pool-agnostic in shape and faithful per pool.

    Token fields are non-cached input, cache reads, cache creation, output, and (Codex-only)
    reasoning output. ``cost_usd`` is the provider-reported dollar equivalent when one exists
    (Claude's harness-computed figure); Codex has no provider dollar cost, so it stays
    ``None``. Every field defaults to ``None`` — an absent value means "not reported", never
    zero — and :attr:`present` is false only when the provider reported no usage at all.
    """

    input_tokens: int | None = None            # non-cached input (Codex input net of cached)
    cached_input_tokens: int | None = None      # cache reads
    cache_creation_tokens: int | None = None    # cache-creation input (Claude); Codex has none
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None  # Codex reasoning output; None for Claude
    cost_usd: float | None = None               # provider-reported dollar equivalent, if any
    turns: int | None = None
    duration_ms: int | None = None
    model: str | None = None                    # provider-reported model, when the stream carries it
    model_costs: tuple[ModelCost, ...] = ()     # provider-model attribution, when reported
    unrecognized: tuple = ()                    # unmodeled usage sub-fields, preserved verbatim

    @property
    def present(self) -> bool:
        """Whether the provider reported any usage for this attempt."""
        return any(getattr(self, name) is not None for name in _TOKEN_FIELDS)


_TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_creation_tokens",
    "output_tokens", "reasoning_output_tokens")


def _int(value):
    """Coerce a token count to ``int`` without letting a bool or bad shape poison the sum."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _extra(usage: dict, known: set) -> tuple:
    """The usage sub-fields we did not model, preserved so nothing is silently dropped."""
    return tuple(sorted(k for k in usage if k not in known))


def _model_int(details: dict, *keys: str) -> int | None:
    for key in keys:
        if key in details:
            return _int(details[key])
    return None


def _model_cost(model: str, details: dict) -> ModelCost | None:
    cost = _float(details.get("costUSD", details.get("cost_usd")))
    attributed = ModelCost(
        model=model,
        cost_usd=cost,
        input_tokens=_model_int(details, "inputTokens", "input_tokens"),
        cached_input_tokens=_model_int(
            details, "cacheReadInputTokens", "cache_read_input_tokens", "cached_input_tokens"),
        cache_creation_tokens=_model_int(
            details, "cacheCreationInputTokens", "cache_creation_input_tokens",
            "cache_creation_tokens"),
        output_tokens=_model_int(details, "outputTokens", "output_tokens"),
        reasoning_output_tokens=_model_int(
            details, "reasoningOutputTokens", "reasoning_output_tokens"),
    )
    return attributed if cost is not None or attributed.tokens is not None else None


def claude_usage(events) -> AttemptUsage:
    """Normalize the usage on Claude's terminal ``result`` stream event (Agent SDK stream-json).

    The final ``result`` carries ``usage`` (input / cache-creation / cache-read / output
    tokens), a harness ``total_cost_usd``, ``num_turns``, ``duration_ms``, and ``modelUsage``
    keyed by the model that ran. A stream with no result — or a result with no usage — yields
    an empty :class:`AttemptUsage`, so missing usage stays explicit rather than reading as zero.
    """
    result = None
    for event in events:
        if isinstance(event, dict) and event.get("type") == "result":
            result = event  # the terminal result wins if several appear
    if result is None:
        return AttemptUsage()
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    model_usage = result.get("modelUsage")
    model = None
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        model = next(iter(model_usage))
    model_costs = ()
    if isinstance(model_usage, dict):
        model_costs = tuple(
            attributed
            for model_id, details in model_usage.items()
            if isinstance(model_id, str) and isinstance(details, dict)
            if (attributed := _model_cost(model_id, details)) is not None)
    return AttemptUsage(
        input_tokens=_int(usage.get("input_tokens")),
        cached_input_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        reasoning_output_tokens=None,  # Claude does not separate reasoning output
        cost_usd=_float(result.get("total_cost_usd")),
        turns=_int(result.get("num_turns")),
        duration_ms=_int(result.get("duration_ms")),
        model=model,
        model_costs=model_costs,
        unrecognized=_extra(usage, _CLAUDE_USAGE_KEYS))


def codex_usage(events) -> AttemptUsage:
    """Normalize Codex usage by summing every ``turn.completed`` event's ``usage``.

    Codex reports ``input_tokens`` *including* cached input, so the normalized non-cached
    input nets the cached count out. There is no provider dollar cost and no cache-creation
    notion, so both stay ``None``. The turn count is the number of completed turns. A stream
    with no completed turn (an abort before the first turn) yields an empty usage — a real
    attempt whose spend is genuinely unknown, not free.
    """
    turns = 0
    totals = {k: 0 for k in _CODEX_USAGE_KEYS}
    seen = set()
    extra: set = set()
    for event in events:
        if not (isinstance(event, dict) and event.get("type") == "turn.completed"):
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        turns += 1
        for key, value in usage.items():
            if key in _CODEX_USAGE_KEYS:
                amount = _int(value)
                if amount is not None:
                    totals[key] += amount
                    seen.add(key)
            else:
                extra.add(key)
    if turns == 0:
        return AttemptUsage()
    cached = totals["cached_input_tokens"] if "cached_input_tokens" in seen else None
    gross_input = totals["input_tokens"] if "input_tokens" in seen else None
    net_input = gross_input
    if gross_input is not None and cached is not None:
        net_input = max(0, gross_input - cached)
    reasoning = totals["reasoning_output_tokens"] if "reasoning_output_tokens" in seen else None
    return AttemptUsage(
        input_tokens=net_input,
        cached_input_tokens=cached,
        cache_creation_tokens=None,  # Codex has no cache-creation notion
        output_tokens=totals["output_tokens"] if "output_tokens" in seen else None,
        reasoning_output_tokens=reasoning,
        cost_usd=None,               # Codex reports no provider dollar cost
        turns=turns,
        duration_ms=None,
        model=None,                  # Codex's stream does not report the model
        unrecognized=tuple(sorted(extra)))


def _codex_sessions_root() -> Path:
    """Where Codex rollout ``.jsonl`` files live (issue #516 slice 2). The root is fixed at the
    user's own home directory with no override of any kind, so no untrusted path ever reaches
    the ``rglob`` below."""
    return Path.home() / ".codex" / "sessions"


def _rollout_records(path: Path) -> list[dict]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _rollout_cwd(records: list[dict]) -> str | None:
    for record in records:
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                return payload["cwd"]
    return None


def _rollout_worker_usage(records: list[dict]) -> tuple[str, dict] | None:
    """The last ``token_count`` event's cumulative totals and the model that ran, from one
    Codex rollout. ``None`` when the rollout carries no token fact at all."""
    last_totals: dict | None = None
    model: str | None = None
    session_meta_model: str | None = None
    for record in records:
        rtype = record.get("type")
        payload = record.get("payload")
        if rtype == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info")
            totals = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(totals, dict):
                last_totals = totals
        elif rtype == "turn_context" and isinstance(payload, dict) \
                and isinstance(payload.get("model"), str):
            model = payload["model"]              # the latest one wins
        elif rtype == "session_meta" and isinstance(payload, dict) \
                and isinstance(payload.get("model"), str) and session_meta_model is None:
            session_meta_model = payload["model"]
    if last_totals is None:
        return None
    return (model or session_meta_model or "codex"), last_totals


def lead_codex_worker_usage(record) -> tuple:
    """Codex worker spend a lead (Claude ``fable``) build/revise attempt delegated to
    ``codex exec --cd <worktree>``, observed from the workers' own rollout files rather than
    self-reported by the lead (frozen decision 2, issue #516 slice 2).

    Every rollout under the sessions root whose ``session_meta.cwd`` realpath-matches the
    record's workspace, and whose mtime is at/after the attempt's own ``started_at``, contributes
    its last cumulative ``token_count`` totals, summed per attributed model (falling back to the
    literal ``"codex"`` identity when no model fact is found). The floor is the attempt's own
    admission time, not a backdated slack: a worker cannot start before the attempt that spawned
    it, so a rollout last written before this attempt was admitted belongs to some earlier
    attempt that reused the same workspace (a retry, or a prior build before a revise), never to
    this one — a slack that reached backward past admission would double-book that earlier
    attempt's spend onto this attempt too. A record with no ``started_at`` (falsy) applies no
    mtime floor. Failure anywhere — an unreadable sessions root, a malformed rollout, a bad path —
    degrades to no worker entries; this must never fail the caller's observation.
    """
    try:
        if record.stage not in {"build", "revise"} or record.model != "fable" or not record.source:
            return ()
        root = _codex_sessions_root()
        workspace = os.path.realpath(record.source)
        cutoff = record.started_at if record.started_at else None
        try:
            paths = list(root.rglob("*.jsonl"))
        except OSError:
            return ()
        totals_by_model: dict[str, dict[str, int]] = {}
        for path in paths:
            try:
                if cutoff is not None and path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            records = _rollout_records(path)
            cwd = _rollout_cwd(records)
            if cwd is None:
                continue
            try:
                if os.path.realpath(cwd) != workspace:
                    continue
            except OSError:
                continue
            found = _rollout_worker_usage(records)
            if found is None:
                continue
            model, usage = found
            acc = totals_by_model.setdefault(model, {
                "input_tokens": 0, "cached_input_tokens": 0,
                "output_tokens": 0, "reasoning_output_tokens": 0})
            gross_input = _int(usage.get("input_tokens")) or 0
            cached = _int(usage.get("cached_input_tokens")) or 0
            acc["input_tokens"] += max(0, gross_input - cached)   # net cached out, like codex_usage
            acc["cached_input_tokens"] += cached
            acc["output_tokens"] += _int(usage.get("output_tokens")) or 0
            acc["reasoning_output_tokens"] += _int(usage.get("reasoning_output_tokens")) or 0
        return tuple(
            ModelCost(model=name, cost_usd=None,
                      input_tokens=totals["input_tokens"] or None,
                      cached_input_tokens=totals["cached_input_tokens"] or None,
                      output_tokens=totals["output_tokens"] or None,
                      reasoning_output_tokens=totals["reasoning_output_tokens"] or None)
            for name, totals in totals_by_model.items())
    except Exception:
        # A worker-capture scan must never fail the observation it rides on — degrade quietly.
        return ()


@dataclass(frozen=True)
class AttemptTelemetry:
    """One ended attempt's durable telemetry entry, keyed by its launch token.

    It carries the stage identity and routing dials the attempt ran under, the terminal
    provider cause and whether the stage outcome was verified, the wall-clock span the
    coordinator owns, and the normalized :class:`AttemptUsage`. Restart replays and retries
    each run under their own launch token, so each is a separate entry.
    """

    token: str                       # launch token — the per-attempt identity and idempotency key
    identity: str                    # stage identity (repo|subject|stage|target|round|conflict)
    repo: str
    subject: str
    stage: str
    pool: str
    model: str
    complexity: str
    effort: str | None               # work effort dial (low/medium/high/extra)
    reasoning_effort: str | None     # provider reasoning effort, when known (else explicit None)
    attempt: int                     # attempt number within the stage (initial + continuations)
    continuation: bool               # this attempt was an automatic continuation of a prior one
    restart_resumes: int             # restart replays already spent on this stage identity
    round: int                       # finding-driven revise round behind the stage
    conflict_round: int              # conflict-resolution revise round behind the stage
    verified: bool                   # the stage's outcome was verified this attempt
    outcome: str                     # the stage-native verified outcome, or "" when unverified
    cause: str                       # terminal provider cause (none/capacity/permanent/…)
    classification: str              # coordinator label (recoverable/permanent/incomplete/unknown)
    started_at: int                  # epoch the attempt was admitted
    finalized_at: int                # epoch the coordinator finalized the ended family
    verify_miss: str = ""            # the first failed verification conjunct ("check: detail")
                                     # when unverified and the verifier is typed, else ""
    usage: AttemptUsage = field(default_factory=AttemptUsage)

    def outcome_key(self) -> str:
        """The projection's outcome dimension: the verified stage outcome, or the terminal
        cause of an attempt that did not verify one."""
        if self.verified:
            return self.outcome or "verified"
        return f"unverified:{self.cause}"


_ENTRY_FIELDS = {f.name for f in fields(AttemptTelemetry)}


def telemetry_dir(store_path: Path | str) -> Path:
    """Where per-attempt telemetry entries live, beside the records database and sessions."""
    return Path(store_path).parent / "telemetry"


def _entry_path(store_path: Path | str, token: str) -> Path:
    return telemetry_dir(store_path) / f"{token}.json"


def record_attempt(store_path: Path | str, entry: AttemptTelemetry) -> None:
    """Persist one attempt's telemetry atomically, keyed by its launch token.

    Writing under the immutable launch token makes the write idempotent: a daemon restart
    that re-observes the same ended family re-derives the identical entry and replaces it in
    place, so an attempt's spend is persisted exactly once no matter how often reconciliation
    re-runs. A telemetry write must never break a cycle, so an unwritable directory is
    swallowed — the durable session artifact still holds the raw usage to re-derive later.
    """
    if not entry.token:
        return
    path = _entry_path(store_path, entry.token)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        with tmp.open("w") as stream:
            json.dump(asdict(entry), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except (OSError, NameError):
            pass


def read_attempts(store_path: Path | str) -> list[AttemptTelemetry]:
    """Every persisted attempt entry. An unreadable or malformed file is skipped, never
    fatal — a corrupt tail must not blind the whole projection."""
    entries: list[AttemptTelemetry] = []
    directory = telemetry_dir(store_path)
    try:
        names = sorted(p for p in directory.iterdir() if p.suffix == ".json")
    except OSError:
        return entries
    for path in names:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        usage_data = data.get("usage")
        usage = _decode_usage(usage_data) if isinstance(usage_data, dict) else AttemptUsage()
        fields_in = {k: v for k, v in data.items() if k in _ENTRY_FIELDS and k != "usage"}
        try:
            entries.append(AttemptTelemetry(usage=usage, **fields_in))
        except TypeError:
            continue  # an entry from an incompatible shape — skipped, not fatal
    return entries


def _decode_usage(data: dict) -> AttemptUsage:
    known = {f.name for f in fields(AttemptUsage)}
    picked = {k: v for k, v in data.items() if k in known}
    if isinstance(picked.get("unrecognized"), list):
        picked["unrecognized"] = tuple(picked["unrecognized"])
    model_costs = picked.get("model_costs")
    if isinstance(model_costs, list):
        picked["model_costs"] = tuple(
            attributed
            for item in model_costs
            if isinstance(item, dict) and isinstance(item.get("model"), str)
            if (attributed := _model_cost(item["model"], item)) is not None)
    elif "model_costs" in picked:
        picked["model_costs"] = ()
    try:
        return AttemptUsage(**picked)
    except TypeError:
        return AttemptUsage()


@dataclass(frozen=True)
class TelemetryTotals:
    """Summed spend and explicit missing-data counts for one cell (or the fleet)."""

    attempts: int = 0
    verified: int = 0
    missing_usage: int = 0            # attempts the provider reported no usage for
    cost_missing: int = 0             # attempts with no provider dollar cost (all Codex, some Claude)
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class TelemetryCell:
    """One (stage, model, outcome) cell of the bounded projection."""

    stage: str
    model: str
    outcome: str
    totals: TelemetryTotals


@dataclass(frozen=True)
class TelemetryProjection:
    """The bounded aggregate: per (stage, model, outcome) cells and a fleet-wide total.

    The number of cells is bounded by the small set of distinct stage × model × outcome
    combinations, so the projection stays cheap to publish no matter how many attempts
    accumulate. Missing usage and missing cost are first-class counts, never hidden as zero.
    """

    cells: tuple[TelemetryCell, ...] = ()
    total: TelemetryTotals = field(default_factory=TelemetryTotals)


@dataclass(frozen=True)
class ModelSpendRow:
    """Spend attributed to one model for one stage inside a requested date window.

    ``estimated`` is true when any dollar figure folded into ``cost_usd`` was priced from the
    routing rate card rather than provider-billed — a total mixing billed and estimated
    dollars is itself estimated (issue #516). ``delegate_uncaptured_attempts`` counts, among
    the attempts aggregated into this row, how many are lead-run build/revise attempts whose
    delegated Codex worker spend has not been captured into their usage — the row is real but
    known-incomplete, so an aggregate reading it never silently treats it as fully measured.
    """

    stage: str
    model: str
    attempts: int
    tokens: int | None
    cost_usd: float | None
    estimated: bool = False
    delegate_uncaptured_attempts: int = 0


@dataclass(frozen=True)
class SpendReport:
    start: int
    end: int
    rows: tuple[ModelSpendRow, ...]


def format_spend_report(report: SpendReport) -> str:
    """Render the small operator report without hiding unavailable token or dollar facts.

    An estimated dollar figure renders flagged (``~1.234567 est``) rather than indistinguishably
    from a provider-billed one; a row with no dollar fact at all renders ``—``, never
    ``0.000000`` — unknown is never zero. A row aggregating lead-run attempts whose delegate
    spend was not captured carries a trailing note so the gap is visible, not silent.
    """
    lines = ["stage\tmodel\tattempts\ttokens\tdollars\tnote"]
    for row in report.rows:
        tokens = str(row.tokens) if row.tokens is not None else "—"
        if row.cost_usd is None:
            dollars = "—"
        elif row.estimated:
            dollars = f"~{row.cost_usd:.6f} est"
        else:
            dollars = f"{row.cost_usd:.6f}"
        note = (f"delegate spend not counted ({row.delegate_uncaptured_attempts})"
               if row.delegate_uncaptured_attempts else "")
        lines.append(f"{row.stage}\t{row.model}\t{row.attempts}\t{tokens}\t{dollars}\t{note}")
    return "\n".join(lines)


def project(store_path: Path | str) -> TelemetryProjection:
    """Aggregate every persisted attempt into the bounded stage/model/outcome projection."""
    return project_attempts(read_attempts(store_path))


def project_attempts(entries) -> TelemetryProjection:
    """The pure projection over already-read entries — the fixture-testable core."""
    cells: dict[tuple, list[int | float]] = {}
    fleet = _blank_accumulator()
    for entry in entries:
        key = (entry.stage, entry.model, entry.outcome_key())
        acc = cells.setdefault(key, _blank_accumulator())
        _accumulate(acc, entry)
        _accumulate(fleet, entry)
    ordered = sorted(cells.items())
    return TelemetryProjection(
        cells=tuple(
            TelemetryCell(stage=k[0], model=k[1], outcome=k[2], totals=_freeze(acc))
            for k, acc in ordered),
        total=_freeze(fleet))


def _window_epoch(value: int | float | date | datetime) -> int:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
    if isinstance(value, date):
        return int(datetime.combine(value, datetime_time(), tzinfo=timezone.utc).timestamp())
    return int(value)


def _usage_token_total(usage: AttemptUsage) -> int | None:
    values = tuple(getattr(usage, name) for name in _TOKEN_FIELDS)
    return sum(value for value in values if value is not None) \
        if any(value is not None for value in values) else None


_LEAD_LED_STAGES = frozenset({"build", "revise"})


def _codex_priced(model: str) -> bool:
    """Whether ``model`` names a Codex worker's spend: a routing-known Codex model, or the
    ``"codex"`` fallback identity a merged worker entry carries when no model fact was found
    (issue #516 slice 2). Never a guess — only these two typed shapes count."""
    if model == "codex":
        return True
    from agentflow.routing import routing
    return routing.provider_for(model) == "codex"


def _delegate_spend_uncaptured(entry: AttemptTelemetry) -> bool:
    """Whether ``entry`` is a lead-run build/revise attempt whose delegated Codex worker spend
    has not been merged into its usage yet (issue #516, frozen decision 1). Once a worker-capture
    merge (slice 2) adds a Codex-priced model-cost entry, this returns false for that attempt."""
    if entry.stage not in _LEAD_LED_STAGES or entry.model != "fable":
        return False
    return not any(_codex_priced(cost.model) for cost in entry.usage.model_costs)


def _priced_dollars(model: str, tokens_in, tokens_out, tokens_reasoning, billed):
    """The dollar figure and whether it is provider-billed, estimated from the rate card, or
    unknown. Billed always wins; an estimate is only produced when a real token fact exists and
    the model prices from the routing table's rate card — never a guess past that."""
    if billed is not None:
        return billed, False
    from agentflow.routing import routing
    estimate = routing.estimate_cost_usd(
        model, input_tokens=tokens_in, output_tokens=tokens_out,
        reasoning_output_tokens=tokens_reasoning)
    return (estimate, True) if estimate is not None else (None, False)


def spend_report(store_path: Path | str, *, start: int | float | date | datetime,
                 end: int | float | date | datetime) -> SpendReport:
    """Group persisted spend by stage and attributed model over ``[start, end)``.

    Provider model attribution wins over the dispatched record model. An attempt with no
    per-model attribution (including Codex) falls back to its reported model and then its
    dispatched model, keeping token-only rows even when no dollar equivalent exists.

    A row whose dollars are not provider-billed is priced at report time from the routing rate
    card (fresh input + output/reasoning-output tokens; cached reads excluded) and flagged
    ``estimated`` — a total mixing billed and estimated figures is itself estimated. Stored
    telemetry entries are only read here, never rewritten (frozen decision 4).
    """
    start_epoch, end_epoch = _window_epoch(start), _window_epoch(end)
    if end_epoch <= start_epoch:
        raise ValueError("spend report end must be after start")
    # value = [attempts, tokens, token_seen, dollars, dollar_seen, estimated_seen, uncounted]
    cells: dict[tuple[str, str], list[int | float | bool]] = {}
    for entry in read_attempts(store_path):
        if not start_epoch <= entry.started_at < end_epoch:
            continue
        attributed = entry.usage.model_costs
        uncounted = _delegate_spend_uncaptured(entry)
        if attributed:
            rows = tuple(
                (cost.model, cost.tokens,
                 _priced_dollars(cost.model, cost.input_tokens, cost.output_tokens,
                                 cost.reasoning_output_tokens, cost.cost_usd))
                for cost in attributed)
        else:
            model = entry.usage.model or entry.model
            rows = ((model, _usage_token_total(entry.usage),
                     _priced_dollars(model, entry.usage.input_tokens, entry.usage.output_tokens,
                                     entry.usage.reasoning_output_tokens, entry.usage.cost_usd)),)
        for model, tokens, (dollars, estimated) in rows:
            cell = cells.setdefault((entry.stage, model), [0, 0, False, 0.0, False, False, 0])
            cell[0] += 1
            if tokens is not None:
                cell[1] += tokens
                cell[2] = True
            if dollars is not None:
                cell[3] += dollars
                cell[4] = True
                if estimated:
                    cell[5] = True
            if uncounted:
                cell[6] += 1
    return SpendReport(
        start_epoch,
        end_epoch,
        tuple(
            ModelSpendRow(stage, model, int(values[0]),
                          int(values[1]) if values[2] else None,
                          float(values[3]) if values[4] else None,
                          estimated=bool(values[5]),
                          delegate_uncaptured_attempts=int(values[6]))
            for (stage, model), values in sorted(cells.items())
        ),
    )


# The accumulator is a plain list indexed by the TelemetryTotals field order, so summing is a
# single loop and the frozen result is built once at the end.
_TOTALS_FIELDS = [f.name for f in fields(TelemetryTotals)]


def _blank_accumulator() -> list:
    return [0] * len(_TOTALS_FIELDS)


def _accumulate(acc: list, entry: AttemptTelemetry) -> None:
    idx = _TOTALS_FIELDS.index
    acc[idx("attempts")] += 1
    if entry.verified:
        acc[idx("verified")] += 1
    usage = entry.usage
    if not usage.present:
        acc[idx("missing_usage")] += 1
    if usage.cost_usd is None:
        acc[idx("cost_missing")] += 1
    else:
        acc[idx("cost_usd")] += usage.cost_usd
    for name in _TOKEN_FIELDS:
        value = getattr(usage, name)
        if value is not None:
            acc[idx(name)] += value


def _freeze(acc: list) -> TelemetryTotals:
    return TelemetryTotals(**dict(zip(_TOTALS_FIELDS, acc)))
