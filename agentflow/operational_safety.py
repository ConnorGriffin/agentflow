"""Bounded operational containment behind one coordinator-store owner (ADR 585).

The public interface accepts typed, content-free observations and immutable launch
configuration.  It can request one deterministic rerun, quarantine one exact route
cell, or restore an approved canary's declared predecessor.  It has no filesystem,
GitHub, prompt, policy, routing, autonomy, or merge adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import sqlite3
import threading
import time
from typing import Callable, Literal, Mapping, Protocol
from uuid import uuid4

from agentflow.evidence import EvidenceError, PromotionReceipt
from agentflow.promotion_contract import PromotionAuthorityError, parse_promotion_scope


def _canonical_text(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False)


def _canonical_bytes(value: object) -> bytes:
    return _canonical_text(value).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def canonical_result_schema(schema: Mapping[str, object] | None) -> tuple[str | None, str | None]:
    """Return the canonical compact provider-neutral schema and its lowercase SHA-256."""
    if schema is None:
        return None, None
    text = _launch_canonical_text(schema)
    return text, sha256(text.encode("utf-8")).hexdigest()


def _state_id(cell_key: str, active: str, quarantined: str | None,
              generation: int) -> str:
    return _digest({"cell_key": cell_key, "active": active,
                    "quarantined": quarantined, "generation": generation})


DEPENDENCY_RECEIPTS = {
    "issue_582_merge": "a58dc0c84a7459774631048a67b3e71f8328d144",
    "capability_manifest_sha256":
        "cba84e63be53884e6ed566a534883912f7d22156aad7e4a5590515140d18fcad",
    "issue_584_merge": "ef08dd3d2f691aa154ddaa193e6161b559099396",
    "promotion_scope_registry_sha256":
        "83e02ca43be08e0505d7075c5bdbe8ae032bf28ca50e4074a0632b4fd14a6006",
}


class SafetyRefused(RuntimeError):
    """The requested action is outside the bounded automatic authority."""


@dataclass(frozen=True, slots=True)
class LaunchConfigV1:
    """The complete immutable launch policy registered for one RouteCell."""

    schema: Literal["agentflow-launch-v1"]
    provider: Literal["claude", "codex"]
    internal_model: str
    cli_model: str
    stage_profile_id: str
    reasoning_effort: str | None
    turn_ceiling: int
    wall_ceiling_s: int
    build_lease: tuple[int, int, int] | None
    allowed_tools: tuple[str, ...] | None
    sandbox_policy: Literal["read-only", "workspace-write"]
    result_schema_json: str | None
    result_schema_digest: str | None

    def __post_init__(self) -> None:
        if type(self.schema) is not str or self.schema != "agentflow-launch-v1":
            raise SafetyRefused("invalid launch configuration schema")
        if type(self.provider) is not str or self.provider not in {"claude", "codex"}:
            raise SafetyRefused("invalid launch configuration provider")
        for name in ("internal_model", "cli_model", "stage_profile_id"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise SafetyRefused(f"invalid launch configuration {name}")
        if self.reasoning_effort is not None and (
                type(self.reasoning_effort) is not str or not self.reasoning_effort):
            raise SafetyRefused("invalid launch configuration reasoning_effort")
        for name in ("turn_ceiling", "wall_ceiling_s"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise SafetyRefused(f"invalid launch configuration {name}")
        if self.build_lease is not None and (
                type(self.build_lease) is not tuple or len(self.build_lease) != 3
                or any(type(value) is not int or value <= 0 for value in self.build_lease)
                or not (self.build_lease[0] <= self.build_lease[1] <= self.build_lease[2])
                or not self.stage_profile_id.startswith("build/")):
            raise SafetyRefused("invalid launch configuration build_lease")
        if self.allowed_tools is not None and (
                type(self.allowed_tools) is not tuple or not self.allowed_tools
                or any(type(tool) is not str or not tool for tool in self.allowed_tools)):
            raise SafetyRefused("invalid launch configuration allowed_tools")
        if (type(self.sandbox_policy) is not str
                or self.sandbox_policy not in {"read-only", "workspace-write"}):
            raise SafetyRefused("invalid launch configuration sandbox_policy")
        if ((self.allowed_tools is not None) != (self.sandbox_policy == "read-only")):
            raise SafetyRefused("launch sandbox and tool policy disagree")
        if (self.result_schema_json is None) != (self.result_schema_digest is None):
            raise SafetyRefused("launch result schema fields must be null together")
        if self.result_schema_json is not None:
            if type(self.result_schema_json) is not str or type(self.result_schema_digest) is not str:
                raise SafetyRefused("invalid launch result schema fields")
            try:
                schema = _load_unique_json(self.result_schema_json)
            except (TypeError, ValueError, UnicodeError) as error:
                raise SafetyRefused("invalid launch result schema JSON") from error
            if (not isinstance(schema, dict)
                    or _launch_canonical_text(schema) != self.result_schema_json
                    or sha256(self.result_schema_json.encode("utf-8")).hexdigest()
                    != self.result_schema_digest):
                raise SafetyRefused("invalid launch result schema digest")


_LAUNCH_CONFIG_FIELDS = tuple(LaunchConfigV1.__dataclass_fields__)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object member")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _load_unique_json(value: str) -> object:
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _launch_canonical_text(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False)


def encode_launch_config(config: LaunchConfigV1) -> bytes:
    if type(config) is not LaunchConfigV1:
        raise SafetyRefused("launch configuration must be the exact v1 value")
    return _launch_canonical_text(asdict(config)).encode("utf-8")


def launch_config_digest(config: LaunchConfigV1) -> str:
    return sha256(encode_launch_config(config)).hexdigest()


def decode_launch_config(content: bytes) -> LaunchConfigV1:
    """Decode only exact canonical ``agentflow-launch-v1`` bytes."""
    if type(content) is not bytes:
        raise SafetyRefused("launch configuration must be UTF-8 bytes")
    try:
        raw = _load_unique_json(content.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as error:
        raise SafetyRefused("launch configuration is unreadable") from error
    if not isinstance(raw, dict) or set(raw) != set(_LAUNCH_CONFIG_FIELDS):
        raise SafetyRefused("launch configuration fields are not closed v1")
    values = dict(raw)
    if isinstance(values["build_lease"], list):
        values["build_lease"] = tuple(values["build_lease"])
    if isinstance(values["allowed_tools"], list):
        values["allowed_tools"] = tuple(values["allowed_tools"])
    try:
        config = LaunchConfigV1(**values)
    except TypeError as error:
        raise SafetyRefused("launch configuration fields are malformed") from error
    if encode_launch_config(config) != content:
        raise SafetyRefused("launch configuration is not canonical UTF-8 JSON")
    return config


@dataclass(frozen=True)
class DeterministicCheck:
    identifier: str
    version: str
    side_effect_free: bool
    subject_revision_required: bool
    route_cell_required: bool
    success_predicate: str

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


# Code, not configuration, owns eligibility.  Adding a check is a reviewed source change.
DETERMINISTIC_CHECKS = (
    DeterministicCheck(
        "capability-parity", "1", True, True, True, "outcome == 'pass'"),
    DeterministicCheck(
        "route-health", "1", True, True, True, "outcome == 'pass'"),
)
DETERMINISTIC_CHECK_ALLOWLIST_DIGEST = _digest(
    [asdict(item) for item in DETERMINISTIC_CHECKS])
_CHECKS = {(item.identifier, item.version): item for item in DETERMINISTIC_CHECKS}
PROMOTION_VERIFIER = ("github-authority", "v1")

ROUTE_CELL_CONTRACT = {
    "schema": "agentflow-route-cell-v1",
    "identity_fields": [
        "repository", "stage", "provider", "model", "route_id",
        "launch_config_digest",
    ],
    "launch_config": "canonical-json-sha256",
}
ROUTE_CELL_CONTRACT_DIGEST = _digest(ROUTE_CELL_CONTRACT)

ACTION_STATE_MAP = {
    "rerun": "claimed -> effect_lease_claimed -> idempotent_effect -> result_committed",
    "quarantine": "claimed -> exact_cell_quarantined + result_committed",
    "rollback": "claimed -> predecessor_pointer_restored + result_committed",
}


@dataclass(frozen=True, slots=True)
class RerunTransportContract:
    identity: str
    version: str
    lookup: str
    trigger: str
    trust_boundary: str

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


# This declaration identifies the narrow remote trust boundary.  It is not an
# authority token: OperationalSafety accepts only the exact concrete owner below.
RERUN_TRIGGER_ONCE_CONTRACT = RerunTransportContract(
    identity="agentflow-rerun-trigger-once-by-action-id",
    version="1",
    lookup="evidence_for(action_id)",
    trigger="trigger_once(action_id, intent)",
    trust_boundary="transport durably deduplicates ActionIntent.action_id",
)
RERUN_TRIGGER_ONCE_CONTRACT_DIGEST = RERUN_TRIGGER_ONCE_CONTRACT.digest


OPERATIONAL_SAFETY_CONTRACT = {
    "schema": "agentflow-operational-safety-v1",
    "coordinator_store_schema": 2,
    "route_cell_contract_digest": ROUTE_CELL_CONTRACT_DIGEST,
    "deterministic_check_allowlist_digest": DETERMINISTIC_CHECK_ALLOWLIST_DIGEST,
    "action_state_map": ACTION_STATE_MAP,
    "observation_authority": "evidence_ref -> exact code-owned declaration result",
    "canary_receipt": "read-only #584 receipt authority binds approval declaration digest",
    "promotion_verifier": "/".join(PROMOTION_VERIFIER),
    "route_state": "recomputed cell-key+pointers+generation digest on every read",
    "rerun_effect_owner": "exact ActionIdempotentRerunEffect concrete type",
    "rerun_transport_contract_digest": RERUN_TRIGGER_ONCE_CONTRACT_DIGEST,
    "rerun_transaction": "short lease CAS -> external idempotent effect -> short result CAS",
    "rollback_trigger": "committed exact-cell quarantine result",
    "reopen_proof": "authority-read pass bound to safety_state_id+route_cell+declaration",
    "admission_seam": "participate_in_admission(existing_store_connection, route_cell_digest)",
}
OPERATIONAL_SAFETY_CONTRACT_DIGEST = _digest(OPERATIONAL_SAFETY_CONTRACT)

_RERUN_LEASE_NS = 30_000_000_000
_RERUN_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class RouteCell:
    repository: str
    stage: str
    provider: str
    model: str
    route_id: str
    launch_config_digest: str
    digest: str

    @property
    def key(self) -> str:
        return _digest({
            "repository": self.repository,
            "stage": self.stage,
            "provider": self.provider,
            "model": self.model,
            "route_id": self.route_id,
        })


@dataclass(frozen=True)
class ResolvedLaunch:
    route_cell: RouteCell
    config_bytes: bytes


@dataclass(frozen=True, slots=True)
class AdmittedLaunch:
    """One verified, decoded envelope shared by argv construction and supervision."""

    route_cell: RouteCell
    launch_config: LaunchConfigV1


def decode_admitted_launch(resolved: ResolvedLaunch) -> AdmittedLaunch:
    if type(resolved) is not ResolvedLaunch or type(resolved.route_cell) is not RouteCell:
        raise SafetyRefused("resolved launch has an invalid shape")
    cell = resolved.route_cell
    config = decode_launch_config(resolved.config_bytes)
    body = {
        "repository": cell.repository,
        "stage": cell.stage,
        "provider": cell.provider,
        "model": cell.model,
        "route_id": cell.route_id,
        "launch_config_digest": cell.launch_config_digest,
    }
    if (_digest(body) != cell.digest
            or sha256(resolved.config_bytes).hexdigest() != cell.launch_config_digest):
        raise SafetyRefused("admitted launch digests do not bind its content")
    if config.provider != cell.provider or config.internal_model != cell.model:
        raise SafetyRefused("launch configuration provider/model mismatch")
    if cell.route_id != f"production/{config.stage_profile_id}":
        raise SafetyRefused("launch configuration route identity mismatch")
    profile = config.stage_profile_id.split("/")
    efforts = {"default", "low", "medium", "high", "extra"}
    if cell.stage in {"build", "revise"}:
        if (len(profile) != 3 or profile[0] != cell.stage
                or profile[1] not in {"standard", "deep"} or profile[2] not in efforts):
            raise SafetyRefused("launch configuration stage profile mismatch")
    elif config.stage_profile_id != cell.stage:
        raise SafetyRefused("launch configuration stage profile mismatch")
    return AdmittedLaunch(cell, config)


@dataclass(frozen=True)
class ObservationRequest:
    repository: str
    subject: str
    subject_revision: str
    check_id: str
    check_version: str
    route_cell_digest: str
    evidence_ref: str

    @property
    def scope_identity(self) -> str:
        return _digest({
            "repository": self.repository,
            "subject": self.subject,
            "subject_revision": self.subject_revision,
            "check_id": self.check_id,
            "check_version": self.check_version,
            "route_cell_digest": self.route_cell_digest,
        })


@dataclass(frozen=True)
class CheckEvidence:
    """Authority-read, content-free result for one deterministic declaration."""

    observation_id: str
    repository: str
    subject: str
    subject_revision: str
    check_id: str
    check_version: str
    route_cell_digest: str
    declaration_digest: str
    outcome: str
    evidence_ref: str
    proof: str
    safety_state_id: str = ""


class CheckEvidenceUnavailable(RuntimeError):
    """The result transport is unreadable; this is never semantic failure evidence."""


class CheckEvidenceAuthority(Protocol):
    def read(self, evidence_ref: str) -> CheckEvidence: ...


class PromotionReceiptAuthority(Protocol):
    def read(self, receipt_id: str) -> PromotionReceipt: ...


@dataclass(frozen=True)
class _AuthorizedObservation:
    request: ObservationRequest
    observation_id: str
    declaration: DeterministicCheck
    outcome: str
    evidence_ref: str
    proof: str

    @property
    def scope_identity(self) -> str:
        return self.request.scope_identity


@dataclass(frozen=True)
class ActionIntent:
    action_id: str
    idempotency_key: str
    kind: str
    route_cell_digest: str
    declaration_digest: str
    evidence_ref: str
    exit_condition: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    evidence_ref: str
    proof: str


@dataclass(frozen=True)
class SafetyAlert:
    alert_id: str
    kind: str
    route_cell_digest: str
    evidence_ref: str


@dataclass(frozen=True)
class EffectEvidence:
    evidence_ref: str
    proof: str


@dataclass(frozen=True, slots=True)
class TriggerOnceByActionIdTransportV1:
    """Exact binding for the one unavoidable remote idempotency trust boundary."""

    evidence_for: Callable[[str], EffectEvidence | None]
    trigger_once: Callable[[str, ActionIntent], EffectEvidence]
    contract: RerunTransportContract = field(
        default=RERUN_TRIGGER_ONCE_CONTRACT, init=False)


@dataclass
class _EffectFlight:
    completed: threading.Event = field(default_factory=threading.Event)
    joined: threading.Event = field(default_factory=threading.Event)
    result: EffectEvidence | None = None
    error: BaseException | None = None


class ActionIdempotentRerunEffect:
    """Code-owned action-ID coalescing and evidence sharing around one v1 transport."""

    __slots__ = ("_transport", "_lock", "_results", "_flights")

    def __init__(self, transport: TriggerOnceByActionIdTransportV1) -> None:
        if (type(transport) is not TriggerOnceByActionIdTransportV1
                or transport.contract is not RERUN_TRIGGER_ONCE_CONTRACT):
            raise SafetyRefused("rerun transport is not the exact trigger-once v1 binding")
        self._transport = transport
        self._lock = threading.Lock()
        self._results: dict[str, EffectEvidence] = {}
        self._flights: dict[str, _EffectFlight] = {}

    def evidence_for(self, action_id: str) -> EffectEvidence | None:
        with self._lock:
            result = self._results.get(action_id)
            flight = self._flights.get(action_id)
        if result is not None:
            return result
        if flight is not None:
            return self._join(flight)
        result = self._transport.evidence_for(action_id)
        if result is None:
            return None
        self._validate_evidence(result)
        with self._lock:
            existing = self._results.setdefault(action_id, result)
        if existing != result:
            raise SafetyRefused("rerun transport returned conflicting action evidence")
        return existing

    def apply(self, intent: ActionIntent) -> EffectEvidence:
        action_id = intent.action_id
        with self._lock:
            result = self._results.get(action_id)
            if result is not None:
                return result
            flight = self._flights.get(action_id)
            owner = flight is None
            if owner:
                flight = _EffectFlight()
                self._flights[action_id] = flight
        assert flight is not None
        if not owner:
            return self._join(flight)
        try:
            result = self._transport.trigger_once(action_id, intent)
            self._validate_evidence(result)
        except BaseException as error:
            self._finish(action_id, flight, error=error)
            raise
        self._finish(action_id, flight, result=result)
        return result

    @staticmethod
    def _validate_evidence(result: object) -> None:
        if type(result) is not EffectEvidence:
            raise SafetyRefused("rerun transport returned invalid effect evidence")

    @staticmethod
    def _join(flight: _EffectFlight) -> EffectEvidence:
        flight.joined.set()
        flight.completed.wait()
        if flight.error is not None:
            raise flight.error
        if flight.result is None:
            raise SafetyRefused("rerun effect completed without evidence")
        return flight.result

    def _finish(self, action_id: str, flight: _EffectFlight, *,
                result: EffectEvidence | None = None,
                error: BaseException | None = None) -> None:
        with self._lock:
            if result is not None:
                existing = self._results.setdefault(action_id, result)
                if existing != result:
                    error = SafetyRefused(
                        "rerun transport returned conflicting action evidence")
                    result = None
            flight.result = result
            flight.error = error
            if self._flights.get(action_id) is flight:
                del self._flights[action_id]
            flight.completed.set()


@dataclass(frozen=True)
class RouteSafetyState:
    route_cell_digest: str
    quarantined: bool
    safety_state_id: str
    generation: int


@dataclass(frozen=True)
class _RoutePointerState:
    cell_key: str
    active_digest: str
    quarantined_digest: str | None
    quarantine_action_id: str | None
    safety_state_id: str
    generation: int


@dataclass(frozen=True)
class CanaryActivationRequest:
    promotion_receipt_id: str
    bad_route_cell_digest: str
    predecessor_route_cell_digest: str
    approved_disabled_generation: int

    @property
    def digest(self) -> str:
        return _digest({
            "schema": "agentflow-canary-approval-v1",
            "bad_route_cell_digest": self.bad_route_cell_digest,
            "predecessor_route_cell_digest": self.predecessor_route_cell_digest,
            "approved_disabled_generation": self.approved_disabled_generation,
        })


@dataclass(frozen=True)
class CanaryState:
    cell_key: str
    active_route_cell_digest: str
    active_receipt_id: str | None
    active_receipt_digest: str | None
    predecessor_route_cell_digest: str | None
    disabled_generation: int
    generation: int


@dataclass(frozen=True, slots=True)
class _AdmissionContext:
    """Store-derived facts passed to sealed admission owners."""

    stage_identity: str
    repository: str
    stage: str
    provider: str
    model: str
    route_cell_digest: str


@dataclass(frozen=True, slots=True)
class _SafetyAdmissionResult:
    safety_state_id: str


class OperationalSafety:
    """One owner for bounded operational action and RouteCell state."""

    def __init__(self, store: object, *,
                 check_evidence: CheckEvidenceAuthority | None = None,
                 promotion_receipts: PromotionReceiptAuthority | None = None,
                 rerun_effect: ActionIdempotentRerunEffect | None = None) -> None:
        self._conn: sqlite3.Connection = getattr(store, "_conn")
        self._lock = getattr(store, "_lock")
        self._check_evidence = check_evidence
        self._promotion_receipts = promotion_receipts
        if rerun_effect is not None:
            self._validate_rerun_effect(rerun_effect)
        self._rerun_effect = rerun_effect

    @staticmethod
    def initialize_schema(conn: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS safety_launch_configs ("
            " digest TEXT PRIMARY KEY, content BLOB NOT NULL)",
            "CREATE TABLE IF NOT EXISTS safety_route_cells ("
            " digest TEXT PRIMARY KEY, cell_key TEXT NOT NULL, repository TEXT NOT NULL,"
            " stage TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,"
            " route_id TEXT NOT NULL, launch_config_digest TEXT NOT NULL, data TEXT NOT NULL,"
            " FOREIGN KEY(launch_config_digest) REFERENCES safety_launch_configs(digest))",
            "CREATE TABLE IF NOT EXISTS safety_route_state ("
            " cell_key TEXT PRIMARY KEY, active_digest TEXT NOT NULL,"
            " quarantined_digest TEXT, quarantine_action_id TEXT,"
            " safety_state_id TEXT NOT NULL, generation INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS safety_observations ("
            " observation_id TEXT PRIMARY KEY, scope_identity TEXT NOT NULL,"
            " route_cell_digest TEXT NOT NULL, outcome TEXT NOT NULL, verified INTEGER NOT NULL,"
            " evidence_ref TEXT NOT NULL, declaration_digest TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS safety_actions ("
            " action_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,"
            " kind TEXT NOT NULL, route_cell_digest TEXT NOT NULL,"
            " declaration_digest TEXT NOT NULL, evidence_ref TEXT NOT NULL,"
            " exit_condition TEXT NOT NULL, payload TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS safety_action_results ("
            " action_id TEXT PRIMARY KEY, evidence_ref TEXT NOT NULL, proof TEXT NOT NULL,"
            " FOREIGN KEY(action_id) REFERENCES safety_actions(action_id))",
            "CREATE TABLE IF NOT EXISTS safety_rerun_claims ("
            " action_id TEXT PRIMARY KEY, owner_token TEXT NOT NULL,"
            " generation INTEGER NOT NULL, expires_at_ns INTEGER NOT NULL,"
            " FOREIGN KEY(action_id) REFERENCES safety_actions(action_id))",
            "CREATE TABLE IF NOT EXISTS safety_alerts ("
            " alert_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,"
            " kind TEXT NOT NULL, route_cell_digest TEXT NOT NULL, evidence_ref TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS safety_canary_state ("
            " cell_key TEXT PRIMARY KEY, active_digest TEXT NOT NULL, active_receipt_id TEXT,"
            " active_receipt_digest TEXT, predecessor_digest TEXT,"
            " disabled_generation INTEGER NOT NULL,"
            " generation INTEGER NOT NULL)",
        )
        for statement in statements:
            conn.execute(statement)

    def register_route_cell(
            self, repository: str, stage: str, provider: str, model: str,
            route_id: str, launch_config: Mapping[str, object] | LaunchConfigV1) -> RouteCell:
        config_bytes = (encode_launch_config(launch_config)
                        if type(launch_config) is LaunchConfigV1
                        else _canonical_bytes(launch_config))
        config_digest = sha256(config_bytes).hexdigest()
        body = {
            "repository": repository,
            "stage": stage,
            "provider": provider,
            "model": model,
            "route_id": route_id,
            "launch_config_digest": config_digest,
        }
        cell = RouteCell(**body, digest=_digest(body))
        if type(launch_config) is LaunchConfigV1:
            decode_admitted_launch(ResolvedLaunch(cell, config_bytes))
        with self._transaction():
            row = self._conn.execute(
                "SELECT content FROM safety_launch_configs WHERE digest = ?",
                (config_digest,)).fetchone()
            if row is not None:
                if self._launch_config(config_digest, conn=self._conn) != config_bytes:
                    raise SafetyRefused("launch config digest collision")
            self._conn.execute(
                "INSERT OR IGNORE INTO safety_launch_configs (digest, content) VALUES (?, ?)",
                (config_digest, config_bytes))
            stored_cell = self._conn.execute(
                "SELECT data FROM safety_route_cells WHERE digest = ?",
                (cell.digest,)).fetchone()
            if stored_cell is not None:
                if self._cell(cell.digest, conn=self._conn) != cell:
                    raise SafetyRefused("RouteCell digest collision")
            self._conn.execute(
                "INSERT OR IGNORE INTO safety_route_cells "
                "(digest, cell_key, repository, stage, provider, model, route_id, "
                "launch_config_digest, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cell.digest, cell.key, repository, stage, provider, model, route_id,
                 config_digest, _canonical_text(body)))
            state = self._conn.execute(
                "SELECT active_digest FROM safety_route_state WHERE cell_key = ?",
                (cell.key,)).fetchone()
            if state is None:
                state_id = _state_id(cell.key, cell.digest, None, 0)
                self._conn.execute(
                    "INSERT INTO safety_route_state VALUES (?, ?, NULL, NULL, ?, 0)",
                    (cell.key, cell.digest, state_id))
                self._conn.execute(
                    "INSERT INTO safety_canary_state VALUES (?, ?, NULL, NULL, NULL, 0, 0)",
                    (cell.key, cell.digest))
            else:
                self._canary_pointer_state(cell.key, conn=self._conn)
        return cell

    def resolve(self, repository: str, stage: str, provider: str,
                model: str, route_id: str) -> ResolvedLaunch:
        cell_key = _digest({
            "repository": repository, "stage": stage, "provider": provider,
            "model": model, "route_id": route_id,
        })
        with self._lock:
            canary = self._canary_pointer_state(cell_key, conn=self._conn)
            cell = self._cell(canary.active_route_cell_digest, conn=self._conn)
            config = self._launch_config(cell.launch_config_digest, conn=self._conn)
        return ResolvedLaunch(cell, config)

    def observe(self, request: ObservationRequest) -> tuple[ActionIntent, ...]:
        observation = self._authorize_observation(request)
        declaration = observation.declaration
        created: list[ActionIntent] = []
        with self._transaction():
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO safety_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (observation.observation_id, observation.scope_identity,
                 request.route_cell_digest, observation.outcome,
                 int(observation.outcome != "unreadable"),
                 observation.evidence_ref, declaration.digest)).rowcount
            if not inserted:
                stored = self._conn.execute(
                    "SELECT scope_identity, route_cell_digest, outcome, verified,"
                    " evidence_ref, declaration_digest FROM safety_observations"
                    " WHERE observation_id = ?", (observation.observation_id,)).fetchone()
                expected = (
                    observation.scope_identity, request.route_cell_digest,
                    observation.outcome, int(observation.outcome != "unreadable"),
                    observation.evidence_ref, declaration.digest,
                )
                if stored != expected:
                    raise SafetyRefused("observation identity collision")
                return self._actions_for_scope(observation.scope_identity)
            if observation.outcome == "pass":
                return ()
            rerun = self._claim_action(
                "rerun", observation.scope_identity, request.route_cell_digest,
                declaration.digest, observation.evidence_ref,
                "matching pass or one bounded failure observation",
                {"scope_identity": observation.scope_identity,
                 "repository": request.repository,
                 "subject": request.subject,
                 "check_id": request.check_id,
                 "check_version": request.check_version,
                 "subject_revision": request.subject_revision})
            created.append(rerun)
            if observation.outcome == "unreadable":
                self._alert(
                    "transport", "transport:" + observation.scope_identity,
                    request.route_cell_digest, observation.evidence_ref)
                return tuple(created)
            count = self._conn.execute(
                "SELECT COUNT(*) FROM safety_observations "
                "WHERE scope_identity = ? AND outcome = 'fail' AND verified = 1",
                (observation.scope_identity,)).fetchone()[0]
            if count < 2:
                return tuple(created)
            quarantine = self._quarantine(observation, declaration)
            created.append(quarantine)
            return tuple(created)

    def reconcile(self, action_id: str) -> ActionResult:
        effect = self._require_rerun_effect()
        while True:
            intent, owner_token = self._claim_rerun_effect(action_id)
            if owner_token is None:
                result = self.action_result(action_id)
                if result is None:
                    raise SafetyRefused("internal action has no committed result")
                return result
            try:
                evidence = effect.evidence_for(action_id)
                if evidence is None:
                    evidence = effect.apply(intent)
                if (not evidence.evidence_ref or not evidence.proof
                        or action_id not in evidence.proof):
                    raise SafetyRefused("rerun effect evidence does not bind the action ID")
            except BaseException:
                self._release_rerun_effect(action_id, owner_token)
                raise
            try:
                superseded = False
                with self._transaction():
                    existing = self._action_result(action_id)
                    if existing is not None:
                        self._conn.execute(
                            "DELETE FROM safety_rerun_claims"
                            " WHERE action_id = ? AND owner_token = ?",
                            (action_id, owner_token))
                        return existing
                    claim = self._conn.execute(
                        "SELECT owner_token FROM safety_rerun_claims WHERE action_id = ?",
                        (action_id,)).fetchone()
                    if claim is None or claim[0] != owner_token:
                        superseded = True
                    else:
                        self._complete_action(
                            intent.action_id, evidence.evidence_ref, evidence.proof)
                        self._conn.execute(
                            "DELETE FROM safety_rerun_claims"
                            " WHERE action_id = ? AND owner_token = ?",
                            (action_id, owner_token))
                        return self._action_result(action_id)  # type: ignore[return-value]
                if superseded:
                    continue
            except BaseException:
                self._release_rerun_effect(action_id, owner_token)
                raise

    @staticmethod
    def _validate_rerun_effect(effect: object) -> None:
        if type(effect) is not ActionIdempotentRerunEffect:
            raise SafetyRefused("rerun effect is not the exact code-owned concrete owner")
        transport = effect._transport
        if (type(transport) is not TriggerOnceByActionIdTransportV1
                or transport.contract is not RERUN_TRIGGER_ONCE_CONTRACT):
            raise SafetyRefused("rerun transport is not the exact trigger-once v1 binding")

    def _require_rerun_effect(self) -> ActionIdempotentRerunEffect:
        effect = self._rerun_effect
        if effect is None:
            raise SafetyRefused("rerun effect adapter is unavailable")
        self._validate_rerun_effect(effect)
        return effect

    def _claim_rerun_effect(self, action_id: str) -> tuple[ActionIntent, str | None]:
        """Return the intent plus this caller's durable lease, waiting for active owners."""
        owner_token = uuid4().hex
        while True:
            with self._transaction():
                intent = self._action(action_id)
                existing = self._action_result(action_id)
                if intent.kind != "rerun":
                    if existing is None:
                        raise SafetyRefused("internal action has no committed result")
                    return intent, None
                if existing is not None:
                    self._conn.execute(
                        "DELETE FROM safety_rerun_claims WHERE action_id = ?",
                        (action_id,))
                    return intent, None
                now = time.time_ns()
                claim = self._conn.execute(
                    "SELECT owner_token, generation, expires_at_ns"
                    " FROM safety_rerun_claims WHERE action_id = ?",
                    (action_id,)).fetchone()
                if claim is None:
                    self._conn.execute(
                        "INSERT INTO safety_rerun_claims VALUES (?, ?, 0, ?)",
                        (action_id, owner_token, now + _RERUN_LEASE_NS))
                    return intent, owner_token
                if claim[2] <= now:
                    changed = self._conn.execute(
                        "UPDATE safety_rerun_claims SET owner_token = ?, generation = ?,"
                        " expires_at_ns = ? WHERE action_id = ? AND owner_token = ?"
                        " AND generation = ? AND expires_at_ns = ?",
                        (owner_token, claim[1] + 1, now + _RERUN_LEASE_NS,
                         action_id, claim[0], claim[1], claim[2])).rowcount
                    if changed == 1:
                        return intent, owner_token
            time.sleep(_RERUN_POLL_SECONDS)

    def _release_rerun_effect(self, action_id: str, owner_token: str) -> None:
        with self._transaction():
            self._conn.execute(
                "DELETE FROM safety_rerun_claims"
                " WHERE action_id = ? AND owner_token = ?",
                (action_id, owner_token))

    def action(self, action_id: str) -> ActionIntent:
        with self._lock:
            return self._action(action_id)

    def action_result(self, action_id: str) -> ActionResult | None:
        with self._lock:
            return self._action_result(action_id)

    def alerts(self, route_cell_digest: str) -> tuple[SafetyAlert, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT alert_id, kind, route_cell_digest, evidence_ref"
                " FROM safety_alerts WHERE route_cell_digest = ? ORDER BY alert_id",
                (route_cell_digest,)).fetchall()
        return tuple(SafetyAlert(*row) for row in rows)

    def route_state(self, route_cell_digest: str) -> RouteSafetyState:
        with self._lock:
            cell = self._cell(route_cell_digest, conn=self._conn)
            state = self._route_pointer_state(cell.key, conn=self._conn)
            self._canary_pointer_state(cell.key, conn=self._conn)
            if state.active_digest != route_cell_digest:
                raise SafetyRefused("route cell is not active")
            return RouteSafetyState(
                route_cell_digest, state.quarantined_digest == route_cell_digest,
                state.safety_state_id, state.generation)

    def reopen(self, route_cell_digest: str, expected_safety_state_id: str,
               evidence_refs: tuple[str, ...]) -> RouteSafetyState:
        required = {(item.identifier, item.version)
                    for item in DETERMINISTIC_CHECKS}
        supplied: set[tuple[str, str]] = set()
        if self._check_evidence is None:
            raise SafetyRefused("check evidence authority is unavailable")
        for evidence_ref in evidence_refs:
            try:
                evidence = self._check_evidence.read(evidence_ref)
            except CheckEvidenceUnavailable as error:
                raise SafetyRefused("reopen evidence is unreadable") from error
            except (EvidenceError, KeyError, ValueError) as error:
                raise SafetyRefused("reopen evidence authority refused the reference") from error
            if not isinstance(evidence, CheckEvidence):
                raise SafetyRefused("reopen evidence authority returned an invalid result")
            declaration = _CHECKS.get((evidence.check_id, evidence.check_version))
            if (declaration is None
                    or evidence.outcome != "pass" or not evidence.proof
                    or evidence.evidence_ref != evidence_ref
                    or evidence.route_cell_digest != route_cell_digest
                    or evidence.safety_state_id != expected_safety_state_id
                    or evidence.declaration_digest != declaration.digest):
                raise SafetyRefused("reopen evidence authority binding was refused")
            supplied.add((evidence.check_id, evidence.check_version))
        if supplied != required:
            raise SafetyRefused("fresh capability-parity and route-health evidence required")
        cell = self._cell(route_cell_digest)
        with self._transaction():
            state = self._route_pointer_state(cell.key, conn=self._conn)
            if (state.safety_state_id != expected_safety_state_id
                    or state.active_digest != route_cell_digest
                    or state.quarantined_digest != route_cell_digest):
                raise SafetyRefused("quarantine compare-and-swap refused")
            generation = state.generation + 1
            state_id = _state_id(cell.key, route_cell_digest, None, generation)
            self._conn.execute(
                "UPDATE safety_route_state SET quarantined_digest = NULL,"
                " quarantine_action_id = NULL, safety_state_id = ?, generation = ?"
                " WHERE cell_key = ? AND safety_state_id = ?",
                (state_id, generation, cell.key, expected_safety_state_id))
        return self.route_state(route_cell_digest)

    def approve_canary(self, approval: CanaryActivationRequest) -> CanaryState:
        bad = self._cell(approval.bad_route_cell_digest)
        predecessor = self._cell(approval.predecessor_route_cell_digest)
        if bad.key != predecessor.key or bad.digest == predecessor.digest:
            raise SafetyRefused("canary predecessor must be a different version of one cell")
        receipt = self._approved_canary(approval, bad.repository)
        with self._transaction():
            state = self._canary_pointer_state(bad.key, conn=self._conn)
            if (state.active_route_cell_digest != predecessor.digest
                    or state.disabled_generation != approval.approved_disabled_generation):
                raise SafetyRefused("canary approval is stale")
            generation = state.generation + 1
            self._conn.execute(
                "UPDATE safety_canary_state SET active_digest = ?, active_receipt_id = ?,"
                " active_receipt_digest = ?, predecessor_digest = ?, generation = ?"
                " WHERE cell_key = ?",
                (bad.digest, receipt.receipt_id, approval.digest, predecessor.digest,
                 generation, bad.key))
            self._activate_pointer(bad.key, predecessor.digest, bad.digest)
        return self.canary_state(bad.digest)

    def rollback_canary(self, approval: CanaryActivationRequest) -> ActionResult:
        rollback_scope = _digest({
            "approval_digest": approval.digest,
            "promotion_receipt_id": approval.promotion_receipt_id,
        })
        action_id = self._action_id(
            "rollback", rollback_scope, approval.bad_route_cell_digest)
        with self._lock:
            existing = self._action_result(action_id)
            if existing is not None:
                intent = self._action(action_id)
                expected_payload = {
                    "scope_identity": rollback_scope,
                    "receipt_id": approval.promotion_receipt_id,
                    "bad_route_cell_digest": approval.bad_route_cell_digest,
                    "predecessor_route_cell_digest": approval.predecessor_route_cell_digest,
                    "disabled_generation": approval.approved_disabled_generation,
                }
                if (intent.kind != "rollback"
                        or intent.route_cell_digest != approval.bad_route_cell_digest
                        or intent.declaration_digest != approval.digest
                        or intent.payload != expected_payload):
                    raise SafetyRefused("rollback result does not bind the exact request")
                return existing
        bad = self._cell(approval.bad_route_cell_digest)
        predecessor = self._cell(approval.predecessor_route_cell_digest)
        if bad.key != predecessor.key:
            raise SafetyRefused("canary rollback crosses route cells")
        receipt = self._approved_canary(approval, bad.repository)
        if receipt.receipt_id != approval.promotion_receipt_id:
            raise SafetyRefused("promotion receipt does not bind the rollback request")
        with self._transaction():
            existing = self._action_result(action_id)
            if existing is not None:
                self._action(action_id)
                return existing
            state = self._canary_pointer_state(bad.key, conn=self._conn)
            if (state.active_route_cell_digest != bad.digest
                    or state.active_receipt_id != receipt.receipt_id
                    or state.active_receipt_digest != approval.digest
                    or state.predecessor_route_cell_digest != predecessor.digest
                    or state.disabled_generation != approval.approved_disabled_generation):
                raise SafetyRefused("canary rollback compare-and-swap refused")
            safety = self._route_pointer_state(bad.key, conn=self._conn)
            if (safety.active_digest != bad.digest
                    or safety.quarantined_digest != bad.digest
                    or not safety.quarantine_action_id):
                raise SafetyRefused("canary rollback requires its committed quarantine")
            quarantine_result = self._action_result(safety.quarantine_action_id)
            if quarantine_result is None:
                raise SafetyRefused("canary rollback requires its committed quarantine result")
            proof = _canonical_text({
                "approval_digest": approval.digest,
                "promotion_receipt_id": receipt.receipt_id,
                "quarantine_action_id": safety.quarantine_action_id,
                "quarantine_result_proof": quarantine_result.proof,
            })
            intent = self._claim_action(
                "rollback", rollback_scope, bad.digest,
                approval.digest, quarantine_result.evidence_ref,
                f"fresh human approval naming disabled generation "
                f"{state.disabled_generation + 1}",
                {"scope_identity": rollback_scope,
                 "receipt_id": receipt.receipt_id,
                 "bad_route_cell_digest": bad.digest,
                 "predecessor_route_cell_digest": predecessor.digest,
                 "disabled_generation": state.disabled_generation})
            disabled = state.disabled_generation + 1
            generation = state.generation + 1
            self._conn.execute(
                "UPDATE safety_canary_state SET active_digest = ?, active_receipt_id = NULL,"
                " active_receipt_digest = NULL, predecessor_digest = NULL,"
                " disabled_generation = ?, generation = ?"
                " WHERE cell_key = ? AND active_digest = ? AND active_receipt_id = ?",
                (predecessor.digest, disabled, generation, bad.key, bad.digest,
                 receipt.receipt_id))
            self._activate_pointer(bad.key, bad.digest, predecessor.digest)
            self._complete_action(intent.action_id, quarantine_result.evidence_ref, proof)
            return self._action_result(intent.action_id)  # type: ignore[return-value]

    def canary_state(self, route_cell_digest: str) -> CanaryState:
        with self._lock:
            cell = self._cell(route_cell_digest, conn=self._conn)
            return self._canary_pointer_state(cell.key, conn=self._conn)

    def _participate_in_admission(
            self, context: _AdmissionContext) -> _SafetyAdmissionResult:
        """Validate Store-derived Record facts before any later admission owner runs."""
        cell = self._cell(context.route_cell_digest, conn=self._conn)
        if ((cell.repository, cell.stage, cell.provider, cell.model)
                != (context.repository, context.stage, context.provider, context.model)):
            raise SafetyRefused("route cell does not bind the durable record")
        state = self._route_pointer_state(cell.key, conn=self._conn)
        self._canary_pointer_state(cell.key, conn=self._conn)
        if (state.active_digest != context.route_cell_digest
                or state.quarantined_digest == context.route_cell_digest):
            raise SafetyRefused("route cell is not admissible")
        return _SafetyAdmissionResult(state.safety_state_id)

    def _authorize_observation(self, request: ObservationRequest) -> _AuthorizedObservation:
        declaration = _CHECKS.get((request.check_id, request.check_version))
        if declaration is None or not declaration.side_effect_free:
            raise SafetyRefused("check is not in the deterministic allowlist")
        if not request.repository or not request.subject or not request.evidence_ref:
            raise SafetyRefused("observation identity and evidence are required")
        if declaration.subject_revision_required and not request.subject_revision:
            raise SafetyRefused("subject revision is required")
        if declaration.route_cell_required:
            cell = self._cell(request.route_cell_digest)
            if cell.repository != request.repository:
                raise SafetyRefused("observation crosses repositories")
        if self._check_evidence is None:
            raise SafetyRefused("check evidence authority is unavailable")
        try:
            evidence = self._check_evidence.read(request.evidence_ref)
        except CheckEvidenceUnavailable:
            return _AuthorizedObservation(
                request, "unreadable-" + _digest(asdict(request)), declaration,
                "unreadable", request.evidence_ref, "")
        except (EvidenceError, KeyError, ValueError) as error:
            raise SafetyRefused("check evidence authority refused the reference") from error
        if not isinstance(evidence, CheckEvidence):
            raise SafetyRefused("check evidence authority returned an invalid result")
        expected = (
            request.repository, request.subject, request.subject_revision,
            request.check_id, request.check_version, request.route_cell_digest,
            declaration.digest, request.evidence_ref,
        )
        actual = (
            evidence.repository, evidence.subject, evidence.subject_revision,
            evidence.check_id, evidence.check_version, evidence.route_cell_digest,
            evidence.declaration_digest, evidence.evidence_ref,
        )
        if (actual != expected or evidence.outcome not in {"pass", "fail"}
                or not evidence.observation_id or not evidence.proof):
            raise SafetyRefused("check evidence authority binding was refused")
        return _AuthorizedObservation(
            request, evidence.observation_id, declaration, evidence.outcome,
            evidence.evidence_ref, evidence.proof)

    def _approved_canary(self, approval: CanaryActivationRequest,
                         repository: str) -> PromotionReceipt:
        if (not approval.promotion_receipt_id
                or isinstance(approval.approved_disabled_generation, bool)
                or not isinstance(approval.approved_disabled_generation, int)
                or approval.approved_disabled_generation < 0):
            raise SafetyRefused("valid canary approval identity and generation are required")
        if self._promotion_receipts is None:
            raise SafetyRefused("promotion receipt authority is unavailable")
        try:
            receipt = self._promotion_receipts.read(approval.promotion_receipt_id)
        except (EvidenceError, PromotionAuthorityError, KeyError, ValueError) as error:
            raise SafetyRefused("promotion receipt authority refused the approval") from error
        if (not isinstance(receipt, PromotionReceipt)
                or receipt.receipt_id != approval.promotion_receipt_id
                or not receipt.authoritative or receipt.authority is None):
            raise SafetyRefused("authoritative promotion receipt is required")
        pointer = receipt.authority.pointer
        try:
            scope = parse_promotion_scope(pointer.scope)
        except PromotionAuthorityError as error:
            raise SafetyRefused("promotion receipt scope was refused") from error
        if ((receipt.authority.verifier_id, receipt.authority.verifier_version)
                != PROMOTION_VERIFIER
                or pointer.content_hash_algorithm != "sha256"
                or pointer.content_hash != approval.digest
                or receipt.policy_version != scope.new
                or (scope.kind == "repository" and scope.repository != repository)):
            raise SafetyRefused("promotion receipt does not bind this canary declaration")
        return receipt

    def _quarantine(self, observation: _AuthorizedObservation,
                    declaration: DeterministicCheck) -> ActionIntent:
        cell = self._cell(observation.request.route_cell_digest, conn=self._conn)
        state = self._route_pointer_state(cell.key, conn=self._conn)
        if state.active_digest != cell.digest:
            raise SafetyRefused("stale RouteCell cannot quarantine the active route")
        intent = self._claim_action(
            "quarantine", observation.scope_identity, cell.digest, declaration.digest,
            observation.evidence_ref,
            "fresh capability-parity and route-health proof for this safety state",
            {"scope_identity": observation.scope_identity})
        if state.quarantined_digest is None:
            generation = state.generation + 1
            state_id = _state_id(cell.key, cell.digest, cell.digest, generation)
            self._conn.execute(
                "UPDATE safety_route_state SET quarantined_digest = ?,"
                " quarantine_action_id = ?, safety_state_id = ?, generation = ?"
                " WHERE cell_key = ? AND active_digest = ? AND quarantined_digest IS NULL",
                (cell.digest, intent.action_id, state_id, generation, cell.key, cell.digest))
        self._alert("route", "route:" + observation.scope_identity,
                    cell.digest, observation.evidence_ref)
        self._complete_action(intent.action_id, observation.evidence_ref,
                              "exact RouteCell quarantined before admission")
        return intent

    def _claim_action(self, kind: str, scope: str, route_cell_digest: str,
                      declaration_digest: str, evidence_ref: str,
                      exit_condition: str, payload: Mapping[str, object]) -> ActionIntent:
        key = self._action_key(kind, scope, route_cell_digest)
        action_id = "safety-" + key
        inserted = self._conn.execute(
            "INSERT OR IGNORE INTO safety_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (action_id, key, kind, route_cell_digest, declaration_digest, evidence_ref,
             exit_condition, _canonical_text(payload))).rowcount
        stored = self._action(action_id)
        expected = ActionIntent(
            action_id, key, kind, route_cell_digest, declaration_digest,
            evidence_ref, exit_condition, payload,
        )
        if (inserted and stored != expected) or (not inserted and (
                stored.action_id, stored.idempotency_key, stored.kind,
                stored.route_cell_digest, stored.declaration_digest,
                stored.exit_condition, stored.payload,
        ) != (
                expected.action_id, expected.idempotency_key, expected.kind,
                expected.route_cell_digest, expected.declaration_digest,
                expected.exit_condition, expected.payload,
        )):
            raise SafetyRefused("action identity collision")
        return stored

    @staticmethod
    def _action_key(kind: str, scope: str, route_cell_digest: str) -> str:
        return _digest({"kind": kind, "scope": scope,
                        "route_cell_digest": route_cell_digest})

    @classmethod
    def _action_id(cls, kind: str, scope: str, route_cell_digest: str) -> str:
        return "safety-" + cls._action_key(kind, scope, route_cell_digest)

    def _action(self, action_id: str) -> ActionIntent:
        row = self._conn.execute(
            "SELECT action_id, idempotency_key, kind, route_cell_digest,"
            " declaration_digest, evidence_ref, exit_condition, payload"
            " FROM safety_actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            raise SafetyRefused("unknown action")
        try:
            payload = json.loads(row[-1])
        except (TypeError, ValueError) as error:
            raise SafetyRefused("stored action payload is unreadable") from error
        if not isinstance(payload, dict) or _canonical_text(payload) != row[-1]:
            raise SafetyRefused("stored action payload is not canonical")
        intent = ActionIntent(*row[:-1], payload)
        if (intent.idempotency_key
                != self._action_key(intent.kind, self._action_scope(intent),
                                    intent.route_cell_digest)
                or intent.action_id != "safety-" + intent.idempotency_key):
            raise SafetyRefused("stored action identity was not accepted")
        return intent

    @staticmethod
    def _action_scope(intent: ActionIntent) -> str:
        scope = intent.payload.get("scope_identity")
        if not isinstance(scope, str) or not scope:
            raise SafetyRefused("stored action scope was not accepted")
        return scope

    def _action_result(self, action_id: str) -> ActionResult | None:
        row = self._conn.execute(
            "SELECT action_id, evidence_ref, proof FROM safety_action_results"
            " WHERE action_id = ?", (action_id,)).fetchone()
        return ActionResult(*row) if row is not None else None

    def _actions_for_scope(self, scope: str) -> tuple[ActionIntent, ...]:
        rows = self._conn.execute(
            "SELECT action_id FROM safety_actions WHERE payload LIKE ? ORDER BY kind",
            (f'%"scope_identity":"{scope}"%',)).fetchall()
        return tuple(self._action(row[0]) for row in rows)

    def _complete_action(self, action_id: str, evidence_ref: str, proof: str) -> None:
        inserted = self._conn.execute(
            "INSERT OR IGNORE INTO safety_action_results VALUES (?, ?, ?)",
            (action_id, evidence_ref, proof)).rowcount
        stored = self._action_result(action_id)
        if (inserted and stored != ActionResult(action_id, evidence_ref, proof)) or (
                not inserted and (stored is None or stored.action_id != action_id
                                  or stored.proof != proof)):
            raise SafetyRefused("action result identity collision")

    def _alert(self, kind: str, key: str, route_cell_digest: str,
               evidence_ref: str) -> None:
        alert_id = "alert-" + _digest({"kind": kind, "key": key})
        self._conn.execute(
            "INSERT OR IGNORE INTO safety_alerts VALUES (?, ?, ?, ?, ?)",
            (alert_id, key, kind, route_cell_digest, evidence_ref))

    def _activate_pointer(self, cell_key: str, expected: str, replacement: str) -> None:
        state = self._route_pointer_state(cell_key, conn=self._conn)
        replacement_cell = self._cell(replacement, conn=self._conn)
        if state.active_digest != expected or replacement_cell.key != cell_key:
            raise SafetyRefused("active RouteCell pointer changed")
        generation = state.generation + 1
        quarantined = (state.quarantined_digest
                       if state.quarantined_digest == replacement else None)
        state_id = _state_id(cell_key, replacement, quarantined, generation)
        changed = self._conn.execute(
            "UPDATE safety_route_state SET active_digest = ?, quarantined_digest = ?,"
            " quarantine_action_id = NULL, safety_state_id = ?, generation = ?"
            " WHERE cell_key = ? AND active_digest = ?",
            (replacement, quarantined, state_id, generation, cell_key, expected)).rowcount
        if changed != 1:
            raise SafetyRefused("active RouteCell compare-and-swap refused")

    def _route_pointer_state(
            self, cell_key: str, *,
            conn: sqlite3.Connection | None = None) -> _RoutePointerState:
        connection = conn or self._conn
        row = connection.execute(
            "SELECT cell_key, active_digest, quarantined_digest, quarantine_action_id,"
            " safety_state_id, generation FROM safety_route_state WHERE cell_key = ?",
            (cell_key,)).fetchone()
        if row is None:
            raise SafetyRefused("route cell is not active")
        state = _RoutePointerState(*row)
        if (state.cell_key != cell_key or isinstance(state.generation, bool)
                or not isinstance(state.generation, int) or state.generation < 0
                or state.safety_state_id != _state_id(
                    state.cell_key, state.active_digest,
                    state.quarantined_digest, state.generation)):
            raise SafetyRefused("stored RouteCell safety state was not accepted")
        active = self._cell(state.active_digest, conn=connection)
        if active.key != state.cell_key:
            raise SafetyRefused("active RouteCell pointer crosses cell keys")
        if state.quarantined_digest is None:
            if state.quarantine_action_id is not None:
                raise SafetyRefused("unquarantined RouteCell retains a quarantine action")
            return state
        if (state.quarantined_digest != state.active_digest
                or not state.quarantine_action_id):
            raise SafetyRefused("quarantine pointer is not the exact active RouteCell")
        quarantined = self._cell(state.quarantined_digest, conn=connection)
        if quarantined.key != state.cell_key:
            raise SafetyRefused("quarantine pointer crosses cell keys")
        intent = self._action(state.quarantine_action_id)
        result = self._action_result(state.quarantine_action_id)
        if (intent.kind != "quarantine"
                or intent.route_cell_digest != state.quarantined_digest
                or result is None):
            raise SafetyRefused("quarantine pointer lacks its committed action result")
        return state

    def _validate_canary_state(
            self, state: CanaryState, *, conn: sqlite3.Connection) -> None:
        active = self._cell(state.active_route_cell_digest, conn=conn)
        if active.key != state.cell_key:
            raise SafetyRefused("canary active pointer crosses cell keys")
        receipt_fields = (
            state.active_receipt_id, state.active_receipt_digest,
            state.predecessor_route_cell_digest,
        )
        if all(item is None for item in receipt_fields):
            return
        if any(item is None for item in receipt_fields):
            raise SafetyRefused("canary receipt state is incomplete")
        predecessor = self._cell(
            state.predecessor_route_cell_digest, conn=conn)  # type: ignore[arg-type]
        if predecessor.key != state.cell_key or predecessor.digest == active.digest:
            raise SafetyRefused("canary predecessor pointer crosses cell keys")

    def _canary_pointer_state(
            self, cell_key: str, *, conn: sqlite3.Connection) -> CanaryState:
        route_state = self._route_pointer_state(cell_key, conn=conn)
        row = conn.execute(
            "SELECT cell_key, active_digest, active_receipt_id, active_receipt_digest,"
            " predecessor_digest, disabled_generation, generation"
            " FROM safety_canary_state WHERE cell_key = ?", (cell_key,)).fetchone()
        if row is None:
            raise SafetyRefused("unknown canary cell")
        state = CanaryState(*row)
        if (state.cell_key != cell_key
                or isinstance(state.disabled_generation, bool)
                or not isinstance(state.disabled_generation, int)
                or state.disabled_generation < 0
                or isinstance(state.generation, bool)
                or not isinstance(state.generation, int) or state.generation < 0):
            raise SafetyRefused("stored canary generation was not accepted")
        if state.active_route_cell_digest != route_state.active_digest:
            raise SafetyRefused("canary and RouteCell active pointers disagree")
        self._validate_canary_state(state, conn=conn)
        return state

    def _cell(self, digest: str, *, conn: sqlite3.Connection | None = None) -> RouteCell:
        connection = conn or self._conn
        row = connection.execute(
            "SELECT digest, cell_key, repository, stage, provider, model, route_id,"
            " launch_config_digest, data FROM safety_route_cells WHERE digest = ?",
            (digest,)).fetchone()
        if row is None:
            raise SafetyRefused("unknown RouteCell")
        try:
            body = json.loads(row[8])
        except (TypeError, ValueError) as error:
            raise SafetyRefused("stored RouteCell is unreadable") from error
        expected_keys = {
            "repository", "stage", "provider", "model", "route_id",
            "launch_config_digest",
        }
        if (not isinstance(body, dict) or set(body) != expected_keys
                or _canonical_text(body) != row[8]
                or _digest(body) != digest):
            raise SafetyRefused("stored RouteCell digest was not accepted")
        try:
            cell = RouteCell(**body, digest=digest)
        except TypeError as error:
            raise SafetyRefused("stored RouteCell shape was not accepted") from error
        columns = (row[2], row[3], row[4], row[5], row[6], row[7])
        values = (cell.repository, cell.stage, cell.provider, cell.model,
                  cell.route_id, cell.launch_config_digest)
        if row[0] != digest or row[1] != cell.key or columns != values:
            raise SafetyRefused("stored RouteCell columns do not bind its digest")
        self._launch_config(cell.launch_config_digest, conn=connection)
        return cell

    def _launch_config(self, digest: str, *,
                       conn: sqlite3.Connection | None = None) -> bytes:
        connection = conn or self._conn
        row = connection.execute(
            "SELECT content FROM safety_launch_configs WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            raise SafetyRefused("unknown launch configuration")
        content = bytes(row[0])
        if sha256(content).hexdigest() != digest:
            raise SafetyRefused("stored launch configuration digest was not accepted")
        try:
            decoded = json.loads(content)
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            raise SafetyRefused("stored launch configuration is unreadable") from error
        if isinstance(decoded, dict) and decoded.get("schema") == "agentflow-launch-v1":
            decode_launch_config(content)
        elif not isinstance(decoded, dict) or _canonical_bytes(decoded) != content:
            raise SafetyRefused("stored launch configuration is not canonical")
        return content

    class _Transaction:
        def __init__(self, owner: "OperationalSafety") -> None:
            self.owner = owner

        def __enter__(self) -> None:
            self.owner._lock.acquire()
            try:
                self.owner._conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                self.owner._lock.release()
                raise

        def __exit__(self, kind, value, traceback) -> None:
            try:
                if kind:
                    self.owner._conn.execute("ROLLBACK")
                else:
                    try:
                        self.owner._conn.execute("COMMIT")
                    except BaseException:
                        self.owner._conn.execute("ROLLBACK")
                        raise
            finally:
                self.owner._lock.release()

    def _transaction(self) -> "OperationalSafety._Transaction":
        return self._Transaction(self)
