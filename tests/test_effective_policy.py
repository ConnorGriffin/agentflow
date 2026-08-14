"""Public and adversarial vectors for the #628 effective-policy resolver."""
from __future__ import annotations

from dataclasses import fields, replace
import ast
from collections.abc import Mapping
from hashlib import sha256
import inspect
import json

import pytest

from agentflow.effective_policy import (
    EFFECTIVE_POLICY_CONTRACT,
    EFFECTIVE_POLICY_CONTRACT_DIGEST,
    HOLD_CODES,
    PINNED_EVALUATION_POLICY,
    STAGES,
    ApplicabilityFacts,
    Bound,
    BriefingAuthority,
    BriefingReceipt,
    CapabilityRequirement,
    EffectivePolicyResolver,
    FleetPolicyV1,
    HoldBriefing,
    NarrowBound,
    NotApplicableBriefing,
    OverlayV1,
    PolicyValidationError,
    ReadyBriefing,
)
from agentflow.evidence import ApprovedAuthority, AuthorityPointer, PromotionReceipt


REPOSITORY = "octo/repo"
REVISION = "a" * 40


def _canonical(value):
    def plain(item):
        if isinstance(item, Mapping):
            return {key: plain(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [plain(child) for child in item]
        return item
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def _overlay_value(**changes):
    value = {
        "schema": "briefing-overlay-v1",
        "repository": REPOSITORY,
        "policy_version": 1,
        "remove_receipt_ids": [],
        "remove_capability_contract_ids": [],
        "narrow_bounds": [],
        "holds": [],
        "not_applicable_stages": [],
    }
    value.update(changes)
    value["overlay_digest"] = sha256(_canonical(value)).hexdigest()
    return value


def _overlay(**changes):
    return OverlayV1.parse(_canonical(_overlay_value(**changes)))


class OverlaySource:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = []

    def read(self, repository, subject_revision):
        self.calls.append((repository, subject_revision))
        if self.error:
            raise self.error
        return self.value


class ReceiptReader:
    def __init__(self, receipts=(), error=None):
        self.receipts = {item.receipt_id: item for item in receipts}
        self.error = error
        self.calls = []

    def read(self, receipt_id):
        self.calls.append(receipt_id)
        if self.error:
            raise self.error
        return self.receipts[receipt_id]


def _actual(expected):
    authority = expected.authority
    pointer = AuthorityPointer(
        authority.authority_kind, authority.repository, authority.locator, authority.revision,
        authority.content_hash_algorithm, authority.content_hash, authority.scope)
    approved = ApprovedAuthority(
        pointer, authority.approval_id, authority.approved_revision, authority.approved_hash,
        authority.approved_scope, authority.verifier_id, authority.verifier_version,
        authority.outcome)
    return PromotionReceipt(expected.receipt_id, expected.candidate_id, expected.approval_id,
                            expected.policy_version, approved, expected.authoritative)


def _resolver(*, policy=PINNED_EVALUATION_POLICY, overlay=None, overlay_error=None,
              receipts=None, receipt_error=None):
    receipts = tuple(_actual(item) for item in policy.receipts) if receipts is None \
        and isinstance(policy, FleetPolicyV1) else (receipts or ())
    return EffectivePolicyResolver(
        promotion_receipts=ReceiptReader(receipts, receipt_error),
        overlay_source=OverlaySource(overlay, overlay_error),
        fleet_policy=policy,
    )


def _assert_self_digest(result):
    value = result.value()
    digest = value.pop("briefing_digest")
    identity = value.pop("briefing_id")
    assert digest == sha256(_canonical(value)).hexdigest()
    assert identity == f"briefing-v1:{digest}"
    assert result.canonical_bytes() == _canonical(result.value())


def test_ready_public_result_is_closed_immutable_canonical_and_pinned():
    result = _resolver().brief_for(REPOSITORY, "review", REVISION)
    assert isinstance(result, ReadyBriefing)
    assert result.status == "ready"
    assert result.schema == "briefing-v1"
    assert result.policy_version == 1
    assert result.receipts == PINNED_EVALUATION_POLICY.receipts
    assert result.capabilities == PINNED_EVALUATION_POLICY.capabilities
    assert result.applicability == ApplicabilityFacts("fleet-policy/0-to-1", "review", REVISION)
    assert tuple(result.value()) == (
        "applicability", "briefing_digest", "briefing_id", "capabilities", "policy_version",
        "receipts", "repository", "schema", "stage", "status", "subject_revision")
    _assert_self_digest(result)
    with pytest.raises((AttributeError, TypeError)):
        result.stage = "build"


def test_pins_contract_and_only_read_only_authorities_are_imported_or_called():
    assert EFFECTIVE_POLICY_CONTRACT_DIGEST == sha256(
        _canonical(EFFECTIVE_POLICY_CONTRACT)).hexdigest()
    assert set(STAGES) == {
        "intake", "attack", "research", "build", "review", "revise", "mockup", "respond"}
    assert set(HOLD_CODES) == {
        "missing_policy", "incompatible_policy", "invalid_overlay", "missing_receipt",
        "invalid_receipt", "invalid_briefing", "briefing_overflow"}
    module = ast.parse(inspect.getsource(__import__(
        "agentflow.effective_policy", fromlist=["effective_policy"])))
    imports = {
        alias.name for node in ast.walk(module) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "PromotionReceiptReader" in imports
    assert not {"EvidenceStore", "Record", "Submission", "AttemptTelemetry"} & imports
    calls = {node.func.attr for node in ast.walk(module) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)}
    assert not {"observe", "evaluate", "nominate", "promote", "brief_for"} & calls
    with pytest.raises(TypeError):
        EFFECTIVE_POLICY_CONTRACT["schema"] = "mutable"
    with pytest.raises(TypeError):
        EFFECTIVE_POLICY_CONTRACT["canonical_encoder"]["sort_keys"] = False


@pytest.mark.parametrize("stage", STAGES)
def test_exact_eight_stage_values_are_accepted(stage):
    assert _resolver().brief_for(REPOSITORY, stage, REVISION).status == "ready"


def test_stage_selection_follows_overlay_and_returns_exact_not_applicable_shape():
    result = _resolver(overlay=_overlay(not_applicable_stages=["review"])).brief_for(
        REPOSITORY, "review", REVISION)
    assert isinstance(result, NotApplicableBriefing)
    assert set(result.value()) == {
        "schema", "status", "repository", "stage", "subject_revision", "briefing_digest",
        "briefing_id", "reason"}
    assert result.reason == "stage_not_applicable"
    _assert_self_digest(result)


@pytest.mark.parametrize("hold_code", HOLD_CODES)
def test_overlay_can_add_each_closed_hold_code(hold_code):
    result = _resolver(overlay=_overlay(holds=[hold_code])).brief_for(
        REPOSITORY, "review", REVISION)
    assert isinstance(result, HoldBriefing)
    assert result.hold_code == hold_code
    assert len(result.references) == 1
    _assert_self_digest(result)


def test_each_native_failure_path_maps_to_the_closed_vocabulary():
    assert _resolver(policy=None).brief_for(REPOSITORY, "review", REVISION).hold_code \
        == "missing_policy"
    assert _resolver(policy=object()).brief_for(REPOSITORY, "review", REVISION).hold_code \
        == "incompatible_policy"
    assert _resolver(overlay_error=OSError("secret")).brief_for(
        REPOSITORY, "review", REVISION).hold_code == "invalid_overlay"
    assert _resolver(receipt_error=OSError("secret")).brief_for(
        REPOSITORY, "review", REVISION).hold_code == "missing_receipt"
    expected = PINNED_EVALUATION_POLICY.receipts[0]
    wrong = replace(_actual(expected), candidate_id="wrong-candidate")
    assert _resolver(receipts=(wrong,)).brief_for(
        REPOSITORY, "review", REVISION).hold_code == "invalid_receipt"
    assert _resolver().brief_for(REPOSITORY, "unknown", REVISION).hold_code == "invalid_briefing"


def test_overlay_fold_only_removes_narrows_holds_or_marks_stage_not_applicable():
    authority = PINNED_EVALUATION_POLICY.receipts[0]
    capability = CapabilityRequirement(
        "tool-v1", "v1", "b" * 64, True, (Bound("calls", 10), Bound("tokens", 20)))
    capabilities = tuple(sorted((PINNED_EVALUATION_POLICY.capabilities[0], capability),
                                key=lambda item: _canonical(item.value())))
    policy = FleetPolicyV1(1, (authority,), capabilities)
    overlay = _overlay(
        remove_receipt_ids=[authority.receipt_id],
        remove_capability_contract_ids=["evaluation-semantics-v1"],
        narrow_bounds=[{"bound_name": "calls", "contract_id": "tool-v1", "maximum": 4}],
    )
    result = _resolver(policy=policy, overlay=overlay).brief_for(REPOSITORY, "build", REVISION)
    assert isinstance(result, ReadyBriefing)
    assert result.receipts == ()
    assert result.capabilities == (
        CapabilityRequirement("tool-v1", "v1", "b" * 64, True,
                              (Bound("tokens", 20), Bound("calls", 4))),)


@pytest.mark.parametrize("changes", [
    {"remove_receipt_ids": ["unknown"]},
    {"remove_capability_contract_ids": ["unknown"]},
    {"narrow_bounds": [{"bound_name": "calls", "contract_id": "unknown", "maximum": 1}]},
    {"narrow_bounds": [{"bound_name": "new", "contract_id": "evaluation-semantics-v1",
                         "maximum": 1}]},
    {"holds": ["missing_policy"], "not_applicable_stages": ["review"]},
])
def test_new_targets_and_conflicting_restrictions_are_invalid_overlay(changes):
    assert _resolver(overlay=_overlay(**changes)).brief_for(
        REPOSITORY, "review", REVISION).hold_code == "invalid_overlay"


def test_widened_bound_wrong_repository_or_version_is_invalid_overlay():
    capability = CapabilityRequirement("tool-v1", "v1", "b" * 64, True, (Bound("calls", 10),))
    policy = FleetPolicyV1(1, PINNED_EVALUATION_POLICY.receipts,
                           tuple(sorted((PINNED_EVALUATION_POLICY.capabilities[0], capability),
                                        key=lambda item: _canonical(item.value()))))
    widened = _overlay(narrow_bounds=[
        {"bound_name": "calls", "contract_id": "tool-v1", "maximum": 11}])
    assert _resolver(policy=policy, overlay=widened).brief_for(
        REPOSITORY, "review", REVISION).hold_code == "invalid_overlay"
    for overlay in (_overlay(repository="other/repo"), _overlay(policy_version=2)):
        assert _resolver(overlay=overlay).brief_for(
            REPOSITORY, "review", REVISION).hold_code == "invalid_overlay"


def test_overlay_rejects_unknown_duplicate_recursive_duplicate_unsorted_and_bad_digest():
    valid = _overlay_value()
    bad = dict(valid, unknown=True)
    with pytest.raises(PolicyValidationError):
        OverlayV1.parse(_canonical(bad))
    top_duplicate = _canonical(valid)[:-1] + b',"schema":"briefing-overlay-v1"}'
    with pytest.raises(PolicyValidationError):
        OverlayV1.parse(top_duplicate)
    recursive = _canonical(valid).replace(b'"narrow_bounds":[]',
        b'"narrow_bounds":[{"bound_name":"x","bound_name":"x","contract_id":"c","maximum":1}]')
    with pytest.raises(PolicyValidationError):
        OverlayV1.parse(recursive)
    with pytest.raises(PolicyValidationError):
        OverlayV1.parse(_canonical(_overlay_value(holds=["missing_receipt", "invalid_receipt"])))
    changed = dict(valid, overlay_digest="0" * 64)
    with pytest.raises(PolicyValidationError):
        OverlayV1.parse(_canonical(changed))
    with pytest.raises(PolicyValidationError):
        OverlayV1.parse(json.dumps(valid).encode())


def test_forged_typed_overlay_is_revalidated_before_any_field_is_applied():
    valid = _overlay(holds=["missing_policy"])
    forged = object.__new__(OverlayV1)
    for field in fields(OverlayV1):
        object.__setattr__(forged, field.name, getattr(valid, field.name))
    object.__setattr__(forged, "overlay_digest", "0" * 64)
    result = _resolver(overlay=forged).brief_for(REPOSITORY, "review", REVISION)
    assert result.hold_code == "invalid_overlay"


@pytest.mark.parametrize("field,value", [
    ("repository", "a/b/c"), ("repository", "a/.."), ("repository", "é/repo"),
    ("policy_version", True), ("policy_version", 0), ("policy_version", 2**63),
    ("remove_receipt_ids", ["bad?"]), ("remove_capability_contract_ids", ["bad#"]),
    ("holds", ["secret_content"]), ("not_applicable_stages", ["deploy"]),
])
def test_overlay_named_validator_and_type_boundaries(field, value):
    with pytest.raises(PolicyValidationError):
        _overlay(**{field: value})


def _sized_overlay(size):
    values = [f"r{index:02d}" for index in range(64)]
    base = _overlay_value(remove_receipt_ids=values)
    difference = size - len(_canonical(base))
    for index in range(64):
        room = 128 - len(values[index])
        added = min(room, max(0, difference))
        values[index] += "x" * added
        difference -= added
    assert difference == 0
    return _canonical(_overlay_value(remove_receipt_ids=values))


def test_overlay_exact_8192_is_accepted_and_8193_is_rejected():
    exact = _sized_overlay(8192)
    assert len(exact) == 8192
    assert len(OverlayV1.parse(exact).canonical_bytes) == 8192
    over = _sized_overlay(8193)
    assert len(over) == 8193
    with pytest.raises(PolicyValidationError):
        OverlayV1.parse(over)


def test_every_overlay_array_accepts_64_and_rejects_65():
    tokens64 = [f"r{index:02d}" for index in range(64)]
    assert len(_overlay(remove_receipt_ids=tokens64).remove_receipt_ids) == 64
    with pytest.raises(PolicyValidationError):
        _overlay(remove_receipt_ids=tokens64 + ["r64"])
    capability_overlay = _overlay(remove_capability_contract_ids=tokens64)
    assert len(capability_overlay.remove_capability_contract_ids) == 64
    with pytest.raises(PolicyValidationError):
        _overlay(remove_capability_contract_ids=tokens64 + ["r64"])
    bounds64 = sorted(
        ({"bound_name": f"b{index:02d}", "contract_id": "c", "maximum": index}
         for index in range(64)), key=_canonical)
    assert len(_overlay(narrow_bounds=bounds64).narrow_bounds) == 64
    with pytest.raises(PolicyValidationError):
        _overlay(narrow_bounds=sorted(bounds64 + [
            {"bound_name": "b64", "contract_id": "c", "maximum": 64}], key=_canonical))
    holds = sorted(HOLD_CODES, key=lambda item: _canonical(item))
    assert _overlay(holds=holds).holds == tuple(holds)
    with pytest.raises(PolicyValidationError):
        _overlay(holds=sorted(holds + [holds[0]], key=lambda item: _canonical(item)))
    stages = sorted(STAGES, key=lambda item: _canonical(item))
    assert _overlay(not_applicable_stages=stages).not_applicable_stages == tuple(stages)
    with pytest.raises(PolicyValidationError):
        _overlay(not_applicable_stages=sorted(stages + [stages[0]],
                                              key=lambda item: _canonical(item)))


def test_capability_bounds_accept_32_and_reject_33_and_policy_arrays_64_65():
    bounds = tuple(sorted((Bound(f"b{index:02d}", index) for index in range(32)),
                          key=lambda item: _canonical(item.value())))
    assert len(CapabilityRequirement("c", "v1", "a" * 64, True, bounds).bounds) == 32
    with pytest.raises(PolicyValidationError):
        CapabilityRequirement("c", "v1", "a" * 64, True,
                              bounds + (Bound("b32", 32),))
    capabilities = tuple(CapabilityRequirement(f"c{index:02d}", "v1", f"{index:064x}")
                         for index in range(64))
    assert len(FleetPolicyV1(1, (), capabilities).capabilities) == 64
    result = _resolver(policy=FleetPolicyV1(1, (), capabilities)).brief_for(
        REPOSITORY, "review", REVISION)
    assert isinstance(result, ReadyBriefing)
    assert len(result.capabilities) == 64
    with pytest.raises(PolicyValidationError):
        FleetPolicyV1(1, (), capabilities + (
            CapabilityRequirement("c64", "v1", "f" * 64),))


def _receipt(index, candidate_padding):
    digest = f"{index + 1:064x}"
    approval_id = f"a{index:02d}"
    authority = BriefingAuthority(
        "github", "ConnorGriffin/agentflow",
        f"pulls/{index + 1}/files/p{index:02d}", "a" * 40, "sha256", digest,
        "fleet-policy/0-to-1", approval_id, "a" * 40, digest,
        "fleet-policy/0-to-1", "github-authority", "v1", "verified")
    return BriefingReceipt(f"r{index:02d}", f"c{index:02d}" + "x" * candidate_padding,
                           approval_id, 1, True, authority)


def test_receipt_array_accepts_64_and_rejects_65_then_obeys_final_size_limit():
    receipts = tuple(sorted((_receipt(index, 0) for index in range(64)),
                            key=lambda item: _canonical(item.value())))
    policy = FleetPolicyV1(1, receipts, ())
    assert len(policy.receipts) == 64
    result = _resolver(policy=policy).brief_for(REPOSITORY, "review", REVISION)
    assert isinstance(result, HoldBriefing)
    assert result.hold_code == "briefing_overflow"
    extra = _receipt(64, 0)
    with pytest.raises(PolicyValidationError):
        FleetPolicyV1(1, tuple(sorted(receipts + (extra,),
                                      key=lambda item: _canonical(item.value()))), ())


def _sized_policy(target):
    count = 20
    minimum = [_receipt(index, 0) for index in range(count)]
    minimum.sort(key=lambda item: _canonical(item.value()))
    value = {
        "applicability": {"repository_scope": "fleet-policy/0-to-1", "stage": "review",
                          "subject_revision": REVISION},
        "briefing_digest": "0" * 64, "briefing_id": "briefing-v1:" + "0" * 64,
        "capabilities": [], "policy_version": 1,
        "receipts": [item.value() for item in minimum], "repository": REPOSITORY,
        "schema": "briefing-v1", "stage": "review", "status": "ready",
        "subject_revision": REVISION,
    }
    difference = target - len(_canonical(value))
    paddings = [0] * count
    for index in range(count):
        room = 128 - len(f"c{index:02d}")
        added = min(room, max(0, difference))
        paddings[index] = added
        difference -= added
    assert difference == 0
    receipts = [_receipt(index, paddings[index]) for index in range(count)]
    receipts.sort(key=lambda item: _canonical(item.value()))
    return FleetPolicyV1(1, tuple(receipts), ())


def test_briefing_exact_16384_is_accepted_and_16385_is_overflow():
    exact_policy = _sized_policy(16384)
    exact = _resolver(policy=exact_policy).brief_for(REPOSITORY, "review", REVISION)
    assert isinstance(exact, ReadyBriefing)
    assert len(exact.canonical_bytes()) == 16384
    over_policy = _sized_policy(16385)
    over = _resolver(policy=over_policy).brief_for(REPOSITORY, "review", REVISION)
    assert isinstance(over, HoldBriefing)
    assert over.hold_code == "briefing_overflow"


def test_authority_pointer_approval_scope_and_cross_repository_are_exactly_bound():
    expected = PINNED_EVALUATION_POLICY.receipts[0]
    mutations = (
        {"locator": "pulls/639/files/../candidate.json"},
        {"revision": "A" * 40},
        {"content_hash": "a" * 63},
        {"approved_hash": "a" * 64},
        {"verifier_id": "other"},
    )
    for changes in mutations:
        with pytest.raises(PolicyValidationError):
            replace(expected.authority, **changes)
    cross_authority = replace(
        expected.authority, scope="repository-policy/other/repo/0-to-1",
        approved_scope="repository-policy/other/repo/0-to-1")
    cross = replace(expected, authority=cross_authority)
    policy = FleetPolicyV1(1, (cross,), PINNED_EVALUATION_POLICY.capabilities)
    assert _resolver(policy=policy).brief_for(
        REPOSITORY, "review", REVISION).hold_code == "invalid_receipt"


def test_token_repository_locator_and_bound_name_exact_byte_boundaries():
    capability = CapabilityRequirement("a" * 128, "v1", "a" * 64)
    assert len(capability.contract_id.encode()) == 128
    with pytest.raises(PolicyValidationError):
        CapabilityRequirement("a" * 129, "v1", "a" * 64)
    with pytest.raises(PolicyValidationError):
        CapabilityRequirement("a", "v1", "a" * 64, bounds=(Bound("name", True),))

    authority = PINNED_EVALUATION_POLICY.receipts[0].authority
    minimum_repository = replace(authority, repository="a/b")
    assert len(minimum_repository.repository) == 3
    maximum_repository = "a" * 100 + "/" + "b" * 99
    assert len(replace(authority, repository=maximum_repository).repository) == 200
    for repository in ("a", "a" * 100 + "/" + "b" * 100):
        with pytest.raises(PolicyValidationError):
            replace(authority, repository=repository)

    assert len(replace(authority, locator="pulls/1/files/a").locator) == 15
    maximum_locator = "pulls/1/files/" + "a" * 114
    assert len(replace(authority, locator=maximum_locator).locator) == 128
    for locator in ("pulls/1/file/a", "pulls/1/files/" + "a" * 115,
                    "pulls/1/files/a/../b", "pulls/01/files/a"):
        with pytest.raises(PolicyValidationError):
            replace(authority, locator=locator)

    assert Bound("a" + "b" * 63, 0).name == "a" + "b" * 63
    with pytest.raises(PolicyValidationError):
        Bound("a" + "b" * 64, 0)


def test_scope_revision_digest_subject_and_integer_exact_boundaries():
    authority = PINNED_EVALUATION_POLICY.receipts[0].authority
    for scope in ("fleet-policy/0-to-1", "repository-policy/o/r/0-to-1"):
        assert replace(authority, scope=scope, approved_scope=scope).scope == scope
    owner = "a" * 100
    fixed = len("repository-policy//" + "/0-to-1") + len(owner)
    maximum_scope = f"repository-policy/{owner}/{'b' * (128 - fixed)}/0-to-1"
    assert len(maximum_scope.encode()) == 128
    assert replace(authority, scope=maximum_scope,
                   approved_scope=maximum_scope).scope == maximum_scope
    for scope in (maximum_scope + "b", "fleet-policy/01-to-2", "fleet-policy/1-to-1",
                  "repository-policy/../r/0-to-1"):
        with pytest.raises(PolicyValidationError):
            replace(authority, scope=scope, approved_scope=scope)

    for revision in ("b" * 40, "b" * 64,
                     "sha256:" + authority.content_hash):
        assert replace(authority, revision=revision,
                       approved_revision=revision).revision == revision
    repository_revision = "sha256:" + authority.content_hash
    assert replace(authority, authority_kind="repository", revision=repository_revision,
                   approved_revision=repository_revision).authority_kind == "repository"
    for revision in ("b" * 39, "b" * 65, "B" * 40,
                     "sha256:" + "b" * 64):
        with pytest.raises(PolicyValidationError):
            replace(authority, revision=revision, approved_revision=revision)

    with pytest.raises(PolicyValidationError):
        replace(authority, content_hash="a" * 63, approved_hash="a" * 63)
    for maximum in (0, 2**63 - 1):
        assert Bound("limit", maximum).maximum == maximum
    for maximum in (-1, 2**63, True, 1.0):
        with pytest.raises(PolicyValidationError):
            Bound("limit", maximum)

    assert _resolver().brief_for(REPOSITORY, "review", "b" * 40).status == "ready"
    for revision in ("b" * 39, "b" * 41, "B" * 40):
        assert _resolver().brief_for(
            REPOSITORY, "review", revision).hold_code == "invalid_briefing"
    assert FleetPolicyV1(2**63 - 1, (), ()).policy_version == 2**63 - 1
    for version in (0, 2**63, True, 1.0):
        with pytest.raises(PolicyValidationError):
            FleetPolicyV1(version, (), ())


@pytest.mark.parametrize("changes", [
    {"authority_kind": "gitlab"},
    {"content_hash_algorithm": "sha512"},
    {"approved_revision": "b" * 40},
    {"approved_scope": "fleet-policy/0-to-2"},
    {"verifier_version": "v2"},
    {"outcome": "approved"},
])
def test_every_authority_literal_and_approved_binding_rejects_drift(changes):
    with pytest.raises(PolicyValidationError):
        replace(PINNED_EVALUATION_POLICY.receipts[0].authority, **changes)


def test_receipt_approval_id_must_equal_its_nested_authority():
    receipt = PINNED_EVALUATION_POLICY.receipts[0]
    changed = replace(receipt.authority, approval_id="other")
    with pytest.raises(PolicyValidationError):
        replace(receipt, authority=changed)


def test_hold_references_are_closed_sorted_unique_and_never_expose_rejected_content():
    result = _resolver(receipt_error=RuntimeError("provider transcript secret")).brief_for(
        REPOSITORY, "review", REVISION)
    assert result.references == (PINNED_EVALUATION_POLICY.receipts[0].receipt_id,)
    assert b"secret" not in result.canonical_bytes()
    with pytest.raises((AttributeError, TypeError)):
        result.references += ("mutable",)


def _direct_hold(references=(), **changes):
    value = {
        "hold_code": "invalid_overlay", "references": list(references),
        "repository": REPOSITORY, "schema": "briefing-v1", "stage": "review",
        "status": "hold", "subject_revision": REVISION,
    }
    value.update(changes)
    digest = sha256(_canonical(value)).hexdigest()
    return HoldBriefing(
        value["repository"], value["stage"], value["subject_revision"], digest,
        f"briefing-v1:{digest}", value["hold_code"], tuple(references),
        value["schema"], value["status"])


def test_hold_references_accept_64_reject_65_and_public_results_validate_themselves():
    references = tuple(f"r{index:02d}" for index in range(64))
    assert len(_direct_hold(references).references) == 64
    with pytest.raises(PolicyValidationError):
        _direct_hold(references + ("r64",))
    valid = _resolver().brief_for(REPOSITORY, "review", REVISION)
    with pytest.raises(PolicyValidationError):
        replace(valid, schema="unknown")
    with pytest.raises(PolicyValidationError):
        replace(valid, status="hold")
    with pytest.raises(PolicyValidationError):
        replace(valid, briefing_digest="0" * 64,
                briefing_id="briefing-v1:" + "0" * 64)
    with pytest.raises(PolicyValidationError):
        _direct_hold(hold_code="unknown")
    with pytest.raises(PolicyValidationError):
        _direct_hold(("bad?",))
