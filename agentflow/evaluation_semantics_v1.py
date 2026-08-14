"""Pure procedural authority for the Evaluation v1 semantic bundle."""

from fractions import Fraction as _Fraction
from hashlib import sha256 as _sha256


def _error(contract, operation_id, code_key, path):
    return {
        "code": contract["semantic_errors"][code_key],
        "operation_id": operation_id,
        "path": path,
        "status": "error",
    }


def _ok(operation_id, value):
    return {"operation_id": operation_id, "status": "ok", "value": value}


def _ratio(value):
    if isinstance(value, bool) or not isinstance(value, dict):
        raise ValueError("ratio")
    numerator = value["numerator"]
    denominator = value["denominator"]
    if isinstance(numerator, bool) or isinstance(denominator, bool):
        raise ValueError("ratio")
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        raise ValueError("ratio")
    if denominator <= 0:
        raise ValueError("ratio")
    result = _Fraction(numerator, denominator)
    if result.numerator != numerator or result.denominator != denominator:
        raise ValueError("ratio")
    return result


def _ratio_value(value):
    return {"denominator": value.denominator, "numerator": value.numerator}


def _canonical(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + escaped + '"'
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            parts.append(_canonical(key) + ":" + _canonical(value[key]))
        return "{" + ",".join(parts) + "}"
    raise TypeError("canonical")


def _digest_without(record, omitted_key):
    preimage = {}
    for key in record:
        if key != omitted_key:
            preimage[key] = record[key]
    return _sha256(_canonical(preimage).encode("ascii")).hexdigest()


def _initial_state(policy, partition, seed):
    seed_bytes = (
        policy["domain_separator"].encode("ascii")
        + bytes([policy["separator_byte"]])
        + partition.encode("ascii")
        + bytes([policy["separator_byte"]])
        + seed.to_bytes(policy["seed_bytes"], policy["byte_order"])
    )
    state = int.from_bytes(
        _sha256(seed_bytes).digest()[: policy["state_bytes"]], policy["byte_order"]
    )
    if state == 0:
        state = policy["zero_state"]
    return state


def _random_value(state, policy):
    modulus_mask = policy["modulus"] - 1
    state ^= state >> policy["right_shift_1"]
    state ^= (state << policy["left_shift"]) & modulus_mask
    state ^= state >> policy["right_shift_2"]
    result = (state * policy["multiplier"]) & modulus_mask
    return state, result


def _sample_index(state, bound, policy):
    limit = (policy["modulus"] // bound) * bound
    while True:
        state, result = _random_value(state, policy)
        if result < limit:
            return state, result % bound


def _median(rows):
    ordered = sorted(rows, key=lambda row: (row[0], row[1], row[2]))
    size = len(ordered)
    if size % 2 == 1:
        return ordered[size // 2][0]
    return (ordered[size // 2 - 1][0] + ordered[size // 2][0]) / 2


def _missing_names(contract, arm):
    policy = contract["missingness_policy"]
    if arm["status"] != policy["reported_status"]:
        expected = policy["unavailable_metric_names"]
        for name in expected:
            if arm[name]["state"] != policy["missing_state"] or arm[name]["value"] != policy["missing_integer_sentinel"]:
                return None
        return expected
    for name in policy["required_reported_metrics"]:
        if arm[name]["state"] != policy["present_state"]:
            return None
    names = []
    for name in policy["optional_reported_metrics"]:
        if arm[name]["state"] == policy["missing_state"]:
            if arm[name]["value"] != policy["missing_integer_sentinel"]:
                return None
            names.append(name)
        elif arm[name]["state"] != policy["present_state"]:
            return None
    return sorted(names)


def _rate(arms, status):
    count = 0
    for arm in arms:
        if arm["status"] == status:
            count += 1
    return _Fraction(count, len(arms))


def _metric_mean(arms, name, present_state):
    total = 0
    for arm in arms:
        metric = arm[name]
        if metric["state"] == present_state:
            total += metric["value"]
    return _Fraction(total, len(arms))


def _metric_sum(arms, name, present_state):
    total = 0
    present = 0
    for arm in arms:
        metric = arm[name]
        if metric["state"] == present_state:
            total += metric["value"]
            present += 1
    return {"missing_count": len(arms) - present, "present_count": present, "sum": total}


def _operation_index_lineage(contract, operation_id, input_value):
    records = []
    for group in input_value["record_groups"]:
        for page in group["pages"]:
            records.extend(page["records"])
    by_key = {}
    for index, record in enumerate(records):
        key = record["key"]
        if key in by_key:
            return _error(contract, operation_id, "lineage", "/records/" + str(index) + "/key")
        by_key[key] = record
    incoming = {}
    followers = {}
    for key in sorted(by_key):
        incoming[key] = 0
        followers[key] = []
    for key in sorted(by_key):
        record = by_key[key]
        for predecessor in record["predecessor_keys"]:
            if predecessor not in by_key or predecessor == key:
                return _error(contract, operation_id, "lineage", "/records/" + key + "/predecessor_keys")
            incoming[key] += 1
            followers[predecessor].append(key)
    ready = sorted([key for key in incoming if incoming[key] == 0])
    emitted = []
    while ready:
        key = ready[0]
        ready = ready[1:]
        emitted.append(key)
        for follower in sorted(followers[key]):
            incoming[follower] -= 1
            if incoming[follower] == 0:
                ready.append(follower)
                ready = sorted(ready)
    if len(emitted) != len(records):
        return _error(contract, operation_id, "lineage", "/records")
    corpus_policy = contract["corpus_policy"]
    corpus_ids = {}
    case_ids = {}
    for index, record in enumerate(records):
        kind = record["kind"]
        if kind == corpus_policy["corpus_manifest_kind"]:
            partition = record["partition"]
            if partition in corpus_ids or partition not in corpus_policy["partitions"]:
                return _error(contract, operation_id, "lineage", "/records/" + str(index) + "/partition")
            corpus_ids[partition] = record["id"]
        if kind == corpus_policy["case_manifest_kind"]:
            if record["case_id"] in case_ids:
                return _error(contract, operation_id, "lineage", "/records/" + str(index) + "/case_id")
            case_ids[record["case_id"]] = record
    if sorted(corpus_ids) != sorted(corpus_policy["partitions"]):
        return _error(contract, operation_id, "lineage", "/records")
    if corpus_ids[corpus_policy["partitions"][0]] == corpus_ids[corpus_policy["partitions"][1]]:
        return _error(contract, operation_id, "lineage", "/records")
    receipts = []
    for page in input_value["adjudication_receipt_pages"]:
        receipts.extend(page["receipts"])
    for index, receipt in enumerate(receipts):
        case_id = receipt["case_id"]
        if case_id not in case_ids:
            return _error(contract, operation_id, "lineage", "/adjudication_receipts/" + str(index) + "/case_id")
        case = case_ids[case_id]
        if receipt["case_manifest_digest"] != case["digest"]:
            return _error(contract, operation_id, "lineage", "/adjudication_receipts/" + str(index) + "/case_manifest_digest")
        if receipt["answer_key_digest"] != case["answer_key_digest"]:
            return _error(contract, operation_id, "lineage", "/adjudication_receipts/" + str(index) + "/answer_key_digest")
        if receipt["digest"] != _digest_without(receipt, contract["digest_policy"]["self_member"]):
            return _error(contract, operation_id, "lineage", "/adjudication_receipts/" + str(index) + "/digest")
    holdout = corpus_policy["holdout_partition"]
    incidents = []
    for page in input_value["incident_pages"]:
        incidents.extend(page["incidents"])
    for index, incident in enumerate(incidents):
        if incident["partition"] == holdout:
            return _error(contract, operation_id, "lineage", "/incidents/" + str(index) + "/partition")
        if incident["candidate_materialization_digest"] in input_value["holdout_materialization_digests"]:
            return _error(contract, operation_id, "lineage", "/incidents/" + str(index) + "/candidate_materialization_digest")
    return _ok(operation_id, {"lineage_valid": True, "topological_keys": emitted})


def _operation_schedule(contract, operation_id, input_value):
    policy = contract["schedule_policy"]
    partition = input_value["partition"]
    seed = input_value["seed"]
    if partition not in contract["corpus_policy"]["partitions"]:
        return _error(contract, operation_id, "pairing", "/partition")
    if seed < policy["seed_minimum"] or seed > policy["seed_maximum"]:
        return _error(contract, operation_id, "pairing", "/seed")
    case_ids = []
    for page in input_value["case_id_pages"]:
        case_ids.extend(page["case_ids"])
    unique_case_ids = set(case_ids)
    if not case_ids or len(case_ids) != len(unique_case_ids):
        return _error(contract, operation_id, "pairing", "/case_ids")
    if len(unique_case_ids) > contract["corpus_policy"]["max_cases_per_partition"]:
        return _error(contract, operation_id, "pairing", "/case_ids")
    repetitions = contract["schedule_policy"]["repetition_ids"]
    units = []
    for case_id in sorted(case_ids):
        for repetition in repetitions:
            units.append({"arms": policy["arm_order"], "case_id": case_id, "repetition": repetition})
    state = _initial_state(policy["prng"], partition, seed)
    for index in range(len(units) - 1, 0, -1):
        state, swap_index = _sample_index(state, index + 1, policy["prng"])
        units[index], units[swap_index] = units[swap_index], units[index]
    return _ok(operation_id, {"units": units})


def _operation_lifecycle(contract, operation_id, input_value):
    policy = contract["lifecycle_policy"]
    method = input_value["method"]
    if not method["clean"]:
        return _error(contract, operation_id, "lineage", "/method/clean")
    if not method["resolved"]:
        return _error(contract, operation_id, "lineage", "/method/resolved")
    if len(method["revision"]) != policy["revision_length"]:
        return _error(contract, operation_id, "lineage", "/method/revision")
    if method["materialization_digest"] != method["expected_materialization_digest"]:
        return _error(contract, operation_id, "lineage", "/method/materialization_digest")
    terminal_results = []
    chains = []
    for page in input_value["chain_pages"]:
        chains.extend(page["chains"])
    for chain_index, chain in enumerate(chains):
        chain_key = (chain["case_id"], chain["repetition"], chain["arm"])
        if chain_key in {(row["case_id"], row["repetition"], row["arm"]) for row in terminal_results}:
            return _error(contract, operation_id, "lifecycle", "/chains/" + str(chain_index))
        edges = chain["edges"]
        if len(edges) != len(policy["ordinals"]):
            return _error(contract, operation_id, "lifecycle", "/chains/" + str(chain_index) + "/edges")
        for edge_index, edge in enumerate(edges):
            if edge["ordinal"] != policy["ordinals"][edge_index]:
                return _error(contract, operation_id, "lifecycle", "/chains/" + str(chain_index) + "/edges/" + str(edge_index) + "/ordinal")
            if edge_index == 0:
                if edge["state"] != policy["planned_state"] or edge["predecessor_ordinal"] != policy["no_predecessor_ordinal"]:
                    return _error(contract, operation_id, "lifecycle", "/chains/" + str(chain_index) + "/edges/0")
            elif edge_index == 1:
                if edge["state"] != policy["running_state"] or edge["predecessor_ordinal"] != policy["ordinals"][0]:
                    return _error(contract, operation_id, "lifecycle", "/chains/" + str(chain_index) + "/edges/1")
            else:
                if edge["state"] not in policy["terminal_statuses"] or edge["predecessor_ordinal"] != policy["ordinals"][1]:
                    return _error(contract, operation_id, "lifecycle", "/chains/" + str(chain_index) + "/edges/2")
        terminal_results.append({
            "arm": chain["arm"],
            "case_id": chain["case_id"],
            "repetition": chain["repetition"],
            "status": edges[-1]["state"],
        })
    terminal_results = sorted(terminal_results, key=lambda row: (row["case_id"], row["repetition"], row["arm"]))
    return _ok(operation_id, {"terminal_results": terminal_results})


def _authority_matches(policy, authority, action, producer_subject_id, run_id):
    if authority["action"] != action or authority["run_id"] != run_id:
        return False
    if authority["producer_subject_id"] != producer_subject_id:
        return False
    if authority["normalized_actor_id"] == producer_subject_id:
        return False
    if authority["outcome"] != policy["approved_outcome"]:
        return False
    pointer = authority["pointer"]
    for field in policy["pointer_fields"]:
        if field not in pointer:
            return False
    return pointer["scope"] == policy["scope_prefix"] + run_id + "/" + action


def _operation_authority_blinding(contract, operation_id, input_value):
    policy = contract["authority_policy"]
    package = input_value["package"]
    run_id = input_value["run_id"]
    authorities = {}
    for authority in input_value["authorities"]:
        authorities[authority["action"]] = authority
    for action in policy["blinding_actions"]:
        if action not in authorities or not _authority_matches(policy, authorities[action], action, package["producer_subject_id"], run_id):
            return _error(contract, operation_id, "authority", "/authorities/" + action)
    attestation = input_value["attestation"]
    key_access = input_value["key_access"]
    release = input_value["release"]
    reveal = input_value["reveal"]
    if attestation["package_digest"] != package["digest"]:
        return _error(contract, operation_id, "blinding", "/attestation/package_digest")
    if key_access["attestation_digest"] != attestation["digest"]:
        return _error(contract, operation_id, "blinding", "/key_access/attestation_digest")
    if key_access["mapping_digest"] != package["mapping_digest"]:
        return _error(contract, operation_id, "blinding", "/key_access/mapping_digest")
    if key_access["trust_root"] != policy["trust_root"]:
        return _error(contract, operation_id, "authority", "/key_access/trust_root")
    if key_access["first_access_ordinal"] != policy["first_access_ordinal"]:
        return _error(contract, operation_id, "blinding", "/key_access/first_access_ordinal")
    if key_access["prior_access_count"] != policy["prior_access_count"]:
        return _error(contract, operation_id, "blinding", "/key_access/prior_access_count")
    if not key_access["accessed_after_attestation"]:
        return _error(contract, operation_id, "blinding", "/key_access/accessed_after_attestation")
    if release["key_access_digest"] != key_access["digest"] or release["attestation_digest"] != attestation["digest"]:
        return _error(contract, operation_id, "blinding", "/release")
    if reveal["mapping_digest"] != package["mapping_digest"] or reveal["nonce"] != package["nonce"]:
        return _error(contract, operation_id, "blinding", "/reveal")
    mapping = reveal["mapping"]
    subjects = sorted([mapping[policy["blind_labels"][0]], mapping[policy["blind_labels"][1]]])
    if subjects != sorted(policy["comparison_subjects"]):
        return _error(contract, operation_id, "blinding", "/reveal/mapping")
    return _ok(operation_id, {"human_gate": True, "revealed_mapping": mapping})


def _operation_bootstrap(contract, operation_id, input_value):
    policy = contract["bootstrap_policy"]
    partition = input_value["partition"]
    seed = input_value["seed"]
    rows = []
    for page in input_value["case_mean_pages"]:
        rows.extend(page["case_means"])
    if partition not in contract["corpus_policy"]["partitions"]:
        return _error(contract, operation_id, "bootstrap", "/partition")
    if seed < policy["seed_minimum"] or seed > policy["seed_maximum"]:
        return _error(contract, operation_id, "bootstrap", "/seed")
    if not rows or len(rows) != len({row["case_id"] for row in rows}):
        return _error(contract, operation_id, "bootstrap", "/case_means")
    ordered = sorted(rows, key=lambda row: row["case_id"])
    values = []
    try:
        for row in ordered:
            values.append(_ratio(row["value"]))
    except (KeyError, TypeError, ValueError):
        return _error(contract, operation_id, "bootstrap", "/case_means")
    state = _initial_state(policy["prng"], partition, seed)
    draws = []
    for ordinal in range(policy["draw_count"]):
        total = _Fraction(0, 1)
        for unused in range(len(values)):
            state, index = _sample_index(state, len(values), policy["prng"])
            total += values[index]
        draws.append((total / len(values), ordinal))
    draws = sorted(draws, key=lambda row: (row[0], row[1]))
    low = draws[policy["quantile"]["low_index"]][0]
    high = draws[policy["quantile"]["high_index"]][0]
    result = (
        low * policy["quantile"]["low_weight"]
        + high * policy["quantile"]["high_weight"]
    ) / policy["quantile"]["denominator"]
    return _ok(operation_id, {"lower_bound": _ratio_value(result)})


def _operation_score(contract, operation_id, input_value):
    policy = contract["gate_policy"]
    metric_policy = contract["metric_policy"]
    attempts = []
    for page in input_value["attempt_pages"]:
        attempts.extend(page["attempts"])
    if not attempts:
        return _error(contract, operation_id, "eligibility", "/attempts")
    expected_repetitions = contract["schedule_policy"]["repetition_ids"]
    seen = set()
    for index, attempt in enumerate(attempts):
        key = (attempt["case_id"], attempt["repetition"])
        if key in seen or attempt["repetition"] not in expected_repetitions:
            return _error(contract, operation_id, "pairing", "/attempts/" + str(index))
        seen.add(key)
    case_ids = sorted(set(attempt["case_id"] for attempt in attempts))
    if len(attempts) != len(case_ids) * len(expected_repetitions):
        return _error(contract, operation_id, "pairing", "/attempts")
    baseline = []
    candidate = []
    missing_counts = {}
    for name in contract["missingness_policy"]["unavailable_metric_names"]:
        missing_counts[name] = {"baseline": 0, "candidate": 0}
    for index, attempt in enumerate(attempts):
        for arm_name in contract["schedule_policy"]["arm_order"]:
            arm = attempt[arm_name]
            quality = arm["quality_micros"]
            if quality["value"] < metric_policy["quality_minimum"] or quality["value"] > metric_policy["quality_maximum"]:
                return _error(contract, operation_id, "metric", "/attempts/" + str(index) + "/" + arm_name + "/quality_micros/value")
            names = _missing_names(contract, arm)
            if names is None or names != arm["missing_metric_names"]:
                return _error(contract, operation_id, "missingness", "/attempts/" + str(index) + "/" + arm_name + "/missing_metric_names")
            for name in names:
                missing_counts[name][arm_name] += 1
        baseline.append(attempt["baseline"])
        candidate.append(attempt["candidate"])
    present_state = contract["missingness_policy"]["present_state"]
    quality_differences = []
    token_savings = []
    round_savings = []
    token_complete = True
    round_complete = True
    new_critical_misses = 0
    blocked = False
    hard_by_case = {}
    for attempt in attempts:
        base = attempt["baseline"]
        cand = attempt["candidate"]
        base_quality = base["quality_micros"]["value"] if base["quality_micros"]["state"] == contract["missingness_policy"]["present_state"] else metric_policy["eligibility_missing_quality_value"]
        cand_quality = cand["quality_micros"]["value"] if cand["quality_micros"]["state"] == contract["missingness_policy"]["present_state"] else metric_policy["eligibility_missing_quality_value"]
        quality_differences.append(_Fraction(cand_quality - base_quality, metric_policy["quality_scale"]))
        if cand["critical_miss"]["state"] == present_state and cand["critical_miss"]["value"] is True and not (base["critical_miss"]["state"] == present_state and base["critical_miss"]["value"] is True):
            new_critical_misses += 1
        if base["tokens"]["state"] != present_state or cand["tokens"]["state"] != present_state or base["tokens"]["value"] <= 0:
            token_complete = False
        else:
            token_savings.append((_Fraction(base["tokens"]["value"] - cand["tokens"]["value"], base["tokens"]["value"]), attempt["case_id"], attempt["repetition"]))
        if base["review_rounds"]["state"] != present_state or cand["review_rounds"]["state"] != present_state:
            round_complete = False
        else:
            round_savings.append((_Fraction(base["review_rounds"]["value"] - cand["review_rounds"]["value"], 1), attempt["case_id"], attempt["repetition"]))
        if attempt["hard_case"]:
            if attempt["case_id"] not in hard_by_case:
                hard_by_case[attempt["case_id"]] = {"passes": 0, "new_miss": False}
            if cand["semantic_pass"]["state"] != present_state:
                blocked = True
            elif cand["semantic_pass"]["value"]:
                hard_by_case[attempt["case_id"]]["passes"] += 1
            if cand["critical_miss"]["state"] == present_state and cand["critical_miss"]["value"] is True and not (base["critical_miss"]["state"] == present_state and base["critical_miss"]["value"] is True):
                hard_by_case[attempt["case_id"]]["new_miss"] = True
    hard_gate = True
    for case_id in sorted(hard_by_case):
        row = hard_by_case[case_id]
        if row["passes"] < policy["hard_pass_minimum"] or row["new_miss"]:
            hard_gate = False
    try:
        lower_bound = _ratio(input_value["bootstrap_lower_bound"])
    except (KeyError, TypeError, ValueError):
        return _error(contract, operation_id, "metric", "/bootstrap_lower_bound")
    fp_baseline = _metric_mean(baseline, "grounded_false_positive_count", present_state)
    fp_candidate = _metric_mean(candidate, "grounded_false_positive_count", present_state)
    quality_gate = lower_bound >= _ratio(policy["quality_lower_bound_minimum"]) and fp_candidate <= fp_baseline
    quality_gain = sum(quality_differences, _Fraction(0, 1)) / len(quality_differences)
    token_median = _median(token_savings) if token_complete else None
    round_median = _median(round_savings) if round_complete else None
    improvement_gate = quality_gain >= _ratio(policy["quality_gain_minimum"])
    if token_median is not None and token_median >= _ratio(policy["token_reduction_minimum"]):
        improvement_gate = True
    if round_median is not None and round_median >= _ratio(policy["round_reduction_minimum"]):
        improvement_gate = True
    statuses = contract["lifecycle_policy"]["terminal_statuses"]
    rates = {"baseline": {}, "candidate": {}}
    for status in statuses:
        rates["baseline"][status] = _rate(baseline, status)
        rates["candidate"][status] = _rate(candidate, status)
    verification_failed = {"baseline": 0, "candidate": 0}
    unjudged = {"baseline": 0, "candidate": 0}
    for arm_name, arms in [("baseline", baseline), ("candidate", candidate)]:
        for arm in arms:
            if arm["status"] == metric_policy["completed_status"] and arm["verification_state"] == metric_policy["verification_failed_state"]:
                verification_failed[arm_name] += 1
            if arm["status"] == metric_policy["completed_status"] and arm["quality_micros"]["state"] == contract["missingness_policy"]["missing_state"]:
                unjudged[arm_name] += 1
    completion_floor = rates["baseline"][metric_policy["completed_status"]] - _ratio(policy["completion_drop_maximum"])
    reliability_gate = rates["candidate"][metric_policy["completed_status"]] >= completion_floor
    for status in metric_policy["failure_statuses"]:
        if rates["candidate"][status] > rates["baseline"][status]:
            reliability_gate = False
    if verification_failed["candidate"] > verification_failed["baseline"]:
        reliability_gate = False
    if unjudged["candidate"] > unjudged["baseline"]:
        reliability_gate = False
    gates = {
        "hard": hard_gate,
        "human": input_value["human_gate"],
        "improvement": improvement_gate,
        "quality": quality_gate,
        "reliability": reliability_gate,
    }
    status = policy["eligible_status"]
    if blocked:
        status = policy["blocked_status"]
    elif not all(gates.values()):
        status = policy["ineligible_status"]
    metric_value = {
        "attempt_count": len(attempts),
        "bootstrap_lower_bound": _ratio_value(lower_bound),
        "completion_rates": {arm: _ratio_value(rates[arm][metric_policy["completed_status"]]) for arm in sorted(rates)},
        "duration_means": {"baseline": _ratio_value(_metric_mean(baseline, "duration_ms", present_state)), "candidate": _ratio_value(_metric_mean(candidate, "duration_ms", present_state))},
        "failure_rates": {arm: {name: _ratio_value(rates[arm][name]) for name in metric_policy["failure_statuses"]} for arm in sorted(rates)},
        "fix_introduced_defect_means": {"baseline": _ratio_value(_metric_mean(baseline, "fix_introduced_defect_count", present_state)), "candidate": _ratio_value(_metric_mean(candidate, "fix_introduced_defect_count", present_state))},
        "grounded_false_positive_means": {"baseline": _ratio_value(fp_baseline), "candidate": _ratio_value(fp_candidate)},
        "missing_counts": missing_counts,
        "new_critical_miss_count": new_critical_misses,
        "paired_quality_gain": _ratio_value(quality_gain),
        "provider_dollars": {"baseline": _metric_sum(baseline, "provider_dollars_micros", present_state), "candidate": _metric_sum(candidate, "provider_dollars_micros", present_state)},
        "round_saving_median": {"state": metric_policy["present_median_state"] if round_median is not None else metric_policy["unavailable_median_state"], "value": _ratio_value(round_median) if round_median is not None else metric_policy["unavailable_median_sentinel"]},
        "token_saving_median": {"state": metric_policy["present_median_state"] if token_median is not None else metric_policy["unavailable_median_state"], "value": _ratio_value(token_median) if token_median is not None else metric_policy["unavailable_median_sentinel"]},
        "unjudged_rates": {arm: _ratio_value(_Fraction(unjudged[arm], len(attempts))) for arm in sorted(unjudged)},
        "verification_failed_rates": {arm: _ratio_value(_Fraction(verification_failed[arm], len(attempts))) for arm in sorted(verification_failed)},
    }
    return _ok(operation_id, {"scorecard": {"gates": gates, "metrics": metric_value, "status": status}})


def _operation_evidence(contract, operation_id, input_value):
    policy = contract["evidence_policy"]
    run = input_value["run"]
    aggregate = input_value["aggregate"]
    authority = input_value["authority"]
    if not _authority_matches(contract["authority_policy"], authority, policy["nominate_action"], aggregate["producer_subject_id"], run["run_id"]):
        return _error(contract, operation_id, "publication", "/authority")
    operations = input_value["operation_results"]
    if [row["operation"] for row in operations] != policy["operation_order"]:
        return _error(contract, operation_id, "publication", "/operation_results")
    if len({row["operation"] for row in operations}) != len(policy["operation_order"]):
        return _error(contract, operation_id, "publication", "/operation_results")
    for index, row in enumerate(operations):
        if row["fact_digest"] != aggregate["digest"]:
            return _error(contract, operation_id, "publication", "/operation_results/" + str(index) + "/fact_digest")
    event_id = operations[0]["returned_id"]
    if operations[1]["input_id"] != event_id:
        return _error(contract, operation_id, "publication", "/operation_results/1/input_id")
    preimage = {policy["lesson_preimage_digest_field"]: aggregate["digest"], policy["lesson_preimage_events_field"]: [event_id]}
    lesson_id = policy["lesson_id_prefix"] + _sha256(_canonical(preimage).encode("ascii")).hexdigest()[: policy["lesson_id_hex_length"]]
    locator = aggregate["evidence_locator"]
    digest_revision = policy["digest_revision_prefix"] + aggregate["digest"]
    subject = {
        "content_digest": aggregate["digest"],
        "locator": locator,
        "revision": digest_revision,
        "subject": policy["subject_prefix"] + run["run_id"],
        "subject_kind": policy["subject_kind"],
    }
    producer = {
        "fact_digest": aggregate["digest"],
        "normalizer_version": policy["normalizer_version"],
        "producer_kind": policy["producer_kind"],
        "review_action": None,
        "validation_state": policy["validation_state"],
    }
    envelope = {
        "envelope_kind": policy["envelope_kind"],
        "failure": None,
        "links": [],
        "observation_id": policy["observation_prefix"] + aggregate["aggregate_id"],
        "observed_at": run["nominated_at"],
        "producer": producer,
        "source": authority["pointer"],
        "subject": subject,
    }
    evaluation = {
        "evaluated_at": run["nominated_at"],
        "evaluation_id": policy["evaluation_prefix"] + aggregate["aggregate_id"],
        "event_id": event_id,
        "validation_state": policy["validation_state"],
    }
    lesson = {
        "candidate_id": lesson_id,
        "event_ids": [event_id],
        "nominated_at": run["nominated_at"],
        "policy_version": run["policy_version"],
        "proposal_digest": aggregate["digest"],
    }
    return _ok(operation_id, {"evaluation": evaluation, "evidence_envelope": envelope, "lesson_candidate": lesson, "publication_valid": True})


def evaluate_v1(contract, operation_id, input_value):
    operation_ids = contract["operation_ids"]
    if operation_id == operation_ids["index_lineage"]:
        return _operation_index_lineage(contract, operation_id, input_value)
    if operation_id == operation_ids["schedule"]:
        return _operation_schedule(contract, operation_id, input_value)
    if operation_id == operation_ids["lifecycle_method"]:
        return _operation_lifecycle(contract, operation_id, input_value)
    if operation_id == operation_ids["authority_blinding"]:
        return _operation_authority_blinding(contract, operation_id, input_value)
    if operation_id == operation_ids["score_gates"]:
        return _operation_score(contract, operation_id, input_value)
    if operation_id == operation_ids["bootstrap_lower_bound"]:
        return _operation_bootstrap(contract, operation_id, input_value)
    if operation_id == operation_ids["evidence_publication"]:
        return _operation_evidence(contract, operation_id, input_value)
    return _error(contract, operation_id, "operation", "/operation_id")
