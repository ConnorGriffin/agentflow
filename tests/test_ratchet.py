from agentflow import ratchet


def test_correction_rate():
    assert ratchet.correction_rate([]) == 0.0
    assert ratchet.correction_rate(["clean_merge"] * 4) == 0.0
    assert ratchet.correction_rate(["clean_merge", "parked", "merge_after_revise", "clean_merge"]) == 0.5


def test_ready_to_loosen_needs_a_track_record():
    # thin history never loosens, however clean
    assert ratchet.ready_to_loosen(["clean_merge"] * 5) is False


def test_ready_to_loosen_on_sustained_clean_run():
    assert ratchet.ready_to_loosen(["clean_merge"] * 12) is True


def test_a_recent_regression_tightens():
    # 10-sample window with 2 corrections = 20% > 10% threshold
    outcomes = ["clean_merge"] * 8 + ["parked", "merge_after_revise"]
    assert ratchet.ready_to_loosen(outcomes) is False


def test_record_and_status_roundtrip(tmp_path):
    p = tmp_path / "ratchet.json"
    for _ in range(11):
        ratchet.record("owner/r", "clean_merge", path=p)
    ratchet.record("owner/r", "parked", path=p)
    st = ratchet.status("owner/r", path=p)
    assert st["repo"] == "owner/r" and st["samples"] == 12
    assert 0.0 < st["correction_rate"] < 0.2


def test_record_once_is_idempotent_across_crash_replay(tmp_path):
    path = tmp_path / "ratchet.json"
    ratchet.record_once("owner/r", "clean_merge", "review-7", path=path)
    ratchet.record_once("owner/r", "clean_merge", "review-7", path=path)

    assert ratchet.status("owner/r", path=path)["samples"] == 1
