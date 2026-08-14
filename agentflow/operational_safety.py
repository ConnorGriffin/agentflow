"""Bounded operational containment behind one coordinator-store owner (ADR 585).

The public interface accepts typed, content-free observations and immutable launch
configuration.  It can request one deterministic rerun, quarantine one exact route
cell, or restore an approved canary's declared predecessor.  It has no filesystem,
GitHub, prompt, policy, routing, autonomy, or merge adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import sqlite3
from typing import Mapping, Protocol


def _canonical_text(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False)


def _canonical_bytes(value: object) -> bytes:
    return _canonical_text(value).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


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
    "rerun": "claimed -> externally_effected -> result_committed",
    "quarantine": "claimed -> exact_cell_quarantined + result_committed",
    "rollback": "claimed -> predecessor_pointer_restored + result_committed",
}
OPERATIONAL_SAFETY_CONTRACT = {
    "schema": "agentflow-operational-safety-v1",
    "coordinator_store_schema": 2,
    "route_cell_contract_digest": ROUTE_CELL_CONTRACT_DIGEST,
    "deterministic_check_allowlist_digest": DETERMINISTIC_CHECK_ALLOWLIST_DIGEST,
    "action_state_map": ACTION_STATE_MAP,
    "canary_receipt": "id+bad_digest+predecessor_digest+disabled_generation+receipt_digest",
    "rollback_trigger": "active_bad_route_cell_is_quarantined",
    "reopen_proof": "safety_state_id+route_cell_digest+check_declaration_digest+pass",
    "admission_seam": "participate_in_admission(existing_store_connection, route_cell_digest)",
}
OPERATIONAL_SAFETY_CONTRACT_DIGEST = _digest(OPERATIONAL_SAFETY_CONTRACT)


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


@dataclass(frozen=True)
class SafetyObservation:
    repository: str
    subject: str
    subject_revision: str
    check_id: str
    check_version: str
    route_cell_digest: str
    outcome: str                    # pass | fail | unreadable
    evidence_ref: str
    verified: bool = False

    @property
    def identity(self) -> str:
        return _digest(asdict(self))

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


class RerunEffect(Protocol):
    """Transport-only adapter for the sole external automatic effect."""

    def evidence_for(self, action_id: str) -> EffectEvidence | None: ...

    def apply(self, intent: ActionIntent) -> EffectEvidence: ...


@dataclass(frozen=True)
class RouteSafetyState:
    route_cell_digest: str
    quarantined: bool
    safety_state_id: str
    generation: int


@dataclass(frozen=True)
class ReopenProof:
    check_id: str
    check_version: str
    route_cell_digest: str
    safety_state_id: str
    declaration_digest: str
    evidence_ref: str
    passed: bool


@dataclass(frozen=True)
class CanaryApproval:
    receipt_id: str
    bad_route_cell_digest: str
    predecessor_route_cell_digest: str
    approved_disabled_generation: int

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class CanaryState:
    cell_key: str
    active_route_cell_digest: str
    active_receipt_id: str | None
    active_receipt_digest: str | None
    predecessor_route_cell_digest: str | None
    disabled_generation: int
    generation: int


class OperationalSafety:
    """One owner for bounded operational action and RouteCell state."""

    def __init__(self, store: object, rerun_effect: RerunEffect | None = None) -> None:
        self._conn: sqlite3.Connection = getattr(store, "_conn")
        self._lock = getattr(store, "_lock")
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
            route_id: str, launch_config: Mapping[str, object]) -> RouteCell:
        config_bytes = _canonical_bytes(launch_config)
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
        with self._transaction():
            row = self._conn.execute(
                "SELECT content FROM safety_launch_configs WHERE digest = ?",
                (config_digest,)).fetchone()
            if row is not None and bytes(row[0]) != config_bytes:
                raise SafetyRefused("launch config digest collision")
            self._conn.execute(
                "INSERT OR IGNORE INTO safety_launch_configs (digest, content) VALUES (?, ?)",
                (config_digest, config_bytes))
            stored_cell = self._conn.execute(
                "SELECT data FROM safety_route_cells WHERE digest = ?",
                (cell.digest,)).fetchone()
            if stored_cell is not None and stored_cell[0] != _canonical_text(body):
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
        return cell

    def resolve(self, repository: str, stage: str, provider: str,
                model: str, route_id: str) -> ResolvedLaunch:
        cell_key = _digest({
            "repository": repository, "stage": stage, "provider": provider,
            "model": model, "route_id": route_id,
        })
        with self._lock:
            row = self._conn.execute(
                "SELECT c.data, c.digest, c.launch_config_digest, l.content "
                "FROM safety_route_state s "
                "JOIN safety_route_cells c ON c.digest = s.active_digest "
                "JOIN safety_launch_configs l ON l.digest = c.launch_config_digest "
                "WHERE s.cell_key = ?", (cell_key,)).fetchone()
        if row is None:
            raise SafetyRefused("route cell is not active")
        body = json.loads(row[0])
        return ResolvedLaunch(RouteCell(**body, digest=row[1]), bytes(row[3]))

    def observe(self, observation: SafetyObservation) -> tuple[ActionIntent, ...]:
        declaration = self._validate_observation(observation)
        created: list[ActionIntent] = []
        with self._transaction():
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO safety_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (observation.identity, observation.scope_identity,
                 observation.route_cell_digest, observation.outcome,
                 int(observation.verified and observation.outcome != "unreadable"),
                 observation.evidence_ref, declaration.digest)).rowcount
            if not inserted:
                return self._actions_for_scope(observation.scope_identity)
            if observation.outcome == "pass":
                return ()
            rerun = self._claim_action(
                "rerun", observation.scope_identity, observation.route_cell_digest,
                declaration.digest, observation.evidence_ref,
                "matching pass or one bounded failure observation",
                {"scope_identity": observation.scope_identity,
                 "repository": observation.repository,
                 "subject": observation.subject,
                 "check_id": observation.check_id,
                 "check_version": observation.check_version,
                 "subject_revision": observation.subject_revision})
            created.append(rerun)
            if observation.outcome == "unreadable":
                self._alert(
                    "transport", "transport:" + observation.scope_identity,
                    observation.route_cell_digest, observation.evidence_ref)
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
        intent = self.action(action_id)
        if intent.kind != "rerun":
            result = self.action_result(action_id)
            if result is None:
                raise SafetyRefused("internal action has no committed result")
            return result
        existing = self.action_result(action_id)
        if existing is not None:
            return existing
        if self._rerun_effect is None:
            raise SafetyRefused("rerun effect adapter is unavailable")
        evidence = self._rerun_effect.evidence_for(action_id)
        if evidence is None:
            evidence = self._rerun_effect.apply(intent)
        if (not evidence.evidence_ref or not evidence.proof
                or action_id not in evidence.proof):
            raise SafetyRefused("rerun effect evidence does not bind the action ID")
        with self._transaction():
            self._complete_action(intent.action_id, evidence.evidence_ref, evidence.proof)
        return self.action_result(action_id)  # type: ignore[return-value]

    def action(self, action_id: str) -> ActionIntent:
        with self._lock:
            row = self._conn.execute(
                "SELECT action_id, idempotency_key, kind, route_cell_digest,"
                " declaration_digest, evidence_ref, exit_condition, payload"
                " FROM safety_actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            raise SafetyRefused("unknown action")
        return ActionIntent(*row[:-1], json.loads(row[-1]))

    def action_result(self, action_id: str) -> ActionResult | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT action_id, evidence_ref, proof FROM safety_action_results"
                " WHERE action_id = ?", (action_id,)).fetchone()
        return ActionResult(*row) if row is not None else None

    def alerts(self, route_cell_digest: str) -> tuple[SafetyAlert, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT alert_id, kind, route_cell_digest, evidence_ref"
                " FROM safety_alerts WHERE route_cell_digest = ? ORDER BY alert_id",
                (route_cell_digest,)).fetchall()
        return tuple(SafetyAlert(*row) for row in rows)

    def route_state(self, route_cell_digest: str) -> RouteSafetyState:
        cell = self._cell(route_cell_digest)
        with self._lock:
            row = self._conn.execute(
                "SELECT active_digest, quarantined_digest, safety_state_id, generation"
                " FROM safety_route_state WHERE cell_key = ?", (cell.key,)).fetchone()
        if row is None or row[0] != route_cell_digest:
            raise SafetyRefused("route cell is not active")
        return RouteSafetyState(route_cell_digest, row[1] == route_cell_digest, row[2], row[3])

    def reopen(self, route_cell_digest: str, expected_safety_state_id: str,
               proofs: tuple[ReopenProof, ...]) -> RouteSafetyState:
        required = {(item.identifier, item.version)
                    for item in DETERMINISTIC_CHECKS}
        supplied = {(proof.check_id, proof.check_version) for proof in proofs
                    if proof.passed and proof.route_cell_digest == route_cell_digest
                    and proof.safety_state_id == expected_safety_state_id
                    and _CHECKS.get((proof.check_id, proof.check_version)) is not None
                    and proof.declaration_digest
                    == _CHECKS[(proof.check_id, proof.check_version)].digest
                    and proof.evidence_ref}
        if supplied != required:
            raise SafetyRefused("fresh capability-parity and route-health proofs required")
        cell = self._cell(route_cell_digest)
        with self._transaction():
            row = self._conn.execute(
                "SELECT active_digest, quarantined_digest, generation FROM safety_route_state"
                " WHERE cell_key = ? AND safety_state_id = ?",
                (cell.key, expected_safety_state_id)).fetchone()
            if row is None or row[0] != route_cell_digest or row[1] != route_cell_digest:
                raise SafetyRefused("quarantine compare-and-swap refused")
            generation = row[2] + 1
            state_id = _state_id(cell.key, route_cell_digest, None, generation)
            self._conn.execute(
                "UPDATE safety_route_state SET quarantined_digest = NULL,"
                " quarantine_action_id = NULL, safety_state_id = ?, generation = ?"
                " WHERE cell_key = ? AND safety_state_id = ?",
                (state_id, generation, cell.key, expected_safety_state_id))
        return self.route_state(route_cell_digest)

    def approve_canary(self, approval: CanaryApproval) -> CanaryState:
        if not approval.receipt_id:
            raise SafetyRefused("canary approval receipt is required")
        bad = self._cell(approval.bad_route_cell_digest)
        predecessor = self._cell(approval.predecessor_route_cell_digest)
        if bad.key != predecessor.key or bad.digest == predecessor.digest:
            raise SafetyRefused("canary predecessor must be a different version of one cell")
        with self._transaction():
            row = self._conn.execute(
                "SELECT active_digest, disabled_generation, generation"
                " FROM safety_canary_state WHERE cell_key = ?", (bad.key,)).fetchone()
            if (row is None or row[0] != predecessor.digest
                    or row[1] != approval.approved_disabled_generation):
                raise SafetyRefused("canary approval is stale")
            generation = row[2] + 1
            self._conn.execute(
                "UPDATE safety_canary_state SET active_digest = ?, active_receipt_id = ?,"
                " active_receipt_digest = ?, predecessor_digest = ?, generation = ?"
                " WHERE cell_key = ?",
                (bad.digest, approval.receipt_id, approval.digest, predecessor.digest,
                 generation, bad.key))
            self._activate_pointer(bad.key, predecessor.digest, bad.digest)
        return self.canary_state(bad.digest)

    def rollback_canary(self, approval: CanaryApproval,
                        evidence_ref: str, proof: str) -> ActionResult:
        if not evidence_ref or not proof:
            raise SafetyRefused("rollback evidence and proof are required")
        bad = self._cell(approval.bad_route_cell_digest)
        predecessor = self._cell(approval.predecessor_route_cell_digest)
        if bad.key != predecessor.key:
            raise SafetyRefused("canary rollback crosses route cells")
        with self._transaction():
            row = self._conn.execute(
                "SELECT active_digest, active_receipt_id, active_receipt_digest,"
                " predecessor_digest, disabled_generation, generation FROM safety_canary_state"
                " WHERE cell_key = ?", (bad.key,)).fetchone()
            if (row is None or row[0] != bad.digest or row[1] != approval.receipt_id
                    or row[2] != approval.digest or row[3] != predecessor.digest
                    or row[4] != approval.approved_disabled_generation):
                raise SafetyRefused("canary rollback compare-and-swap refused")
            safety = self._conn.execute(
                "SELECT active_digest, quarantined_digest FROM safety_route_state"
                " WHERE cell_key = ?", (bad.key,)).fetchone()
            if safety != (bad.digest, bad.digest):
                raise SafetyRefused("canary rollback requires its committed quarantine")
            intent = self._claim_action(
                "rollback", _digest(asdict(approval)), bad.digest,
                approval.digest, evidence_ref,
                f"fresh human approval naming disabled generation {row[4] + 1}",
                {"receipt_id": approval.receipt_id,
                 "bad_route_cell_digest": bad.digest,
                 "predecessor_route_cell_digest": predecessor.digest,
                 "disabled_generation": row[4]})
            disabled = row[4] + 1
            generation = row[5] + 1
            self._conn.execute(
                "UPDATE safety_canary_state SET active_digest = ?, active_receipt_id = NULL,"
                " active_receipt_digest = NULL, predecessor_digest = NULL,"
                " disabled_generation = ?, generation = ?"
                " WHERE cell_key = ? AND active_digest = ? AND active_receipt_id = ?",
                (predecessor.digest, disabled, generation, bad.key, bad.digest,
                 approval.receipt_id))
            self._activate_pointer(bad.key, bad.digest, predecessor.digest)
            self._complete_action(intent.action_id, evidence_ref, proof)
        return self.action_result(intent.action_id)  # type: ignore[return-value]

    def canary_state(self, route_cell_digest: str) -> CanaryState:
        cell = self._cell(route_cell_digest)
        with self._lock:
            row = self._conn.execute(
                "SELECT cell_key, active_digest, active_receipt_id, active_receipt_digest,"
                " predecessor_digest, disabled_generation, generation FROM safety_canary_state"
                " WHERE cell_key = ?", (cell.key,)).fetchone()
        if row is None:
            raise SafetyRefused("unknown canary cell")
        return CanaryState(*row)

    def participate_in_admission(
            self, conn: sqlite3.Connection, route_cell_digest: str) -> str:
        """Validate one exact RouteCell inside Store's already-open transaction.

        This is the shared transaction seam declared for #627.  It does not resolve
        briefings, mutate records, reserve permits, or wire coordinator dispatch.
        """
        if conn is not self._conn:
            raise SafetyRefused("admission must use OperationalSafety's Store transaction")
        cell = self._cell(route_cell_digest, conn=conn)
        row = conn.execute(
            "SELECT active_digest, quarantined_digest, safety_state_id"
            " FROM safety_route_state WHERE cell_key = ?", (cell.key,)).fetchone()
        if row is None or row[0] != route_cell_digest or row[1] == route_cell_digest:
            raise SafetyRefused("route cell is not admissible")
        return row[2]

    def _validate_observation(self, observation: SafetyObservation) -> DeterministicCheck:
        declaration = _CHECKS.get((observation.check_id, observation.check_version))
        if declaration is None or not declaration.side_effect_free:
            raise SafetyRefused("check is not in the deterministic allowlist")
        if observation.outcome not in {"pass", "fail", "unreadable"}:
            raise SafetyRefused("unknown check outcome")
        if not observation.repository or not observation.subject or not observation.evidence_ref:
            raise SafetyRefused("observation identity and evidence are required")
        if declaration.subject_revision_required and not observation.subject_revision:
            raise SafetyRefused("subject revision is required")
        if declaration.route_cell_required:
            cell = self._cell(observation.route_cell_digest)
            if cell.repository != observation.repository:
                raise SafetyRefused("observation crosses repositories")
        return declaration

    def _quarantine(self, observation: SafetyObservation,
                    declaration: DeterministicCheck) -> ActionIntent:
        cell = self._cell(observation.route_cell_digest, conn=self._conn)
        intent = self._claim_action(
            "quarantine", observation.scope_identity, cell.digest, declaration.digest,
            observation.evidence_ref,
            "fresh capability-parity and route-health proof for this safety state",
            {"scope_identity": observation.scope_identity})
        row = self._conn.execute(
            "SELECT active_digest, quarantined_digest, generation FROM safety_route_state"
            " WHERE cell_key = ?", (cell.key,)).fetchone()
        if row is None or row[0] != cell.digest:
            raise SafetyRefused("stale RouteCell cannot quarantine the active route")
        if row[1] is None:
            generation = row[2] + 1
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
        key = _digest({"kind": kind, "scope": scope,
                       "route_cell_digest": route_cell_digest})
        action_id = "safety-" + key
        self._conn.execute(
            "INSERT OR IGNORE INTO safety_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (action_id, key, kind, route_cell_digest, declaration_digest, evidence_ref,
             exit_condition, _canonical_text(payload)))
        return self.action(action_id)

    def _actions_for_scope(self, scope: str) -> tuple[ActionIntent, ...]:
        rows = self._conn.execute(
            "SELECT action_id FROM safety_actions WHERE payload LIKE ? ORDER BY kind",
            (f'%"scope_identity":"{scope}"%',)).fetchall()
        return tuple(self.action(row[0]) for row in rows)

    def _complete_action(self, action_id: str, evidence_ref: str, proof: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO safety_action_results VALUES (?, ?, ?)",
            (action_id, evidence_ref, proof))

    def _alert(self, kind: str, key: str, route_cell_digest: str,
               evidence_ref: str) -> None:
        alert_id = "alert-" + _digest({"kind": kind, "key": key})
        self._conn.execute(
            "INSERT OR IGNORE INTO safety_alerts VALUES (?, ?, ?, ?, ?)",
            (alert_id, key, kind, route_cell_digest, evidence_ref))

    def _activate_pointer(self, cell_key: str, expected: str, replacement: str) -> None:
        row = self._conn.execute(
            "SELECT quarantined_digest, generation FROM safety_route_state"
            " WHERE cell_key = ? AND active_digest = ?", (cell_key, expected)).fetchone()
        if row is None:
            raise SafetyRefused("active RouteCell pointer changed")
        generation = row[1] + 1
        quarantined = row[0] if row[0] == replacement else None
        state_id = _state_id(cell_key, replacement, quarantined, generation)
        changed = self._conn.execute(
            "UPDATE safety_route_state SET active_digest = ?, quarantined_digest = ?,"
            " quarantine_action_id = NULL, safety_state_id = ?, generation = ?"
            " WHERE cell_key = ? AND active_digest = ?",
            (replacement, quarantined, state_id, generation, cell_key, expected)).rowcount
        if changed != 1:
            raise SafetyRefused("active RouteCell compare-and-swap refused")

    def _cell(self, digest: str, *, conn: sqlite3.Connection | None = None) -> RouteCell:
        connection = conn or self._conn
        row = connection.execute(
            "SELECT data FROM safety_route_cells WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            raise SafetyRefused("unknown RouteCell")
        body = json.loads(row[0])
        return RouteCell(**body, digest=digest)

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
