"""The provider-authored five-hour quota fact — the Claude dispatch authority (#305).

The fact is persisted per pool beside the records database, validated before use, and read
reset-aware: once the observed window's reset has passed it reports no usage. A missing,
malformed, or window-inconsistent fact fails closed (``None``) — it is never coerced into a
fabricated zero-usage reading.
"""

from __future__ import annotations

import json

from agentflow.coordinator import quota
from agentflow.coordinator.quota import FIVE_HOUR_SECONDS, QuotaFact, build_fact


def _fresh(pool="claude", used=30.0, *, now=1_000_000):
    return QuotaFact(pool=pool, used_percent=used, resets_at=now + 4 * 3600,
                     observed_at=now, provenance="claude:rate_limit_event")


def test_a_persisted_fact_survives_a_fresh_read(tmp_path):
    """The fact round-trips through disk, so the daemon reads the same reading after a restart."""
    store = tmp_path / "records.db"
    quota.record_quota(store, _fresh(used=42.5))
    again = quota.read_quota(store, "claude")
    assert again is not None
    assert again.used_percent == 42.5
    assert again.provenance == "claude:rate_limit_event"
    assert again.pool == "claude"


def test_a_missing_fact_reads_as_none_not_zero(tmp_path):
    """No fact is 'no trustworthy reading', never 'zero usage'."""
    assert quota.read_quota(tmp_path / "records.db", "claude") is None


def test_effective_usage_reports_the_utilization_inside_the_window():
    now = 1_000_000
    assert quota.effective_usage(_fresh(used=30.0, now=now), now) == 30.0


def test_effective_usage_is_zero_once_the_reset_has_passed():
    """Reset-aware: past the observed window's reset the reading no longer applies, so the pool
    reads as fresh — the retired transcript proxy cannot keep it blocked after the reset (#305)."""
    now = 1_000_000
    reset_passed = QuotaFact("claude", 95.0, resets_at=now - 60,
                             observed_at=now - 3600, provenance="claude:rate_limit_event")
    assert quota.effective_usage(reset_passed, now) == 0.0


def test_a_window_that_opens_too_far_in_the_future_fails_closed():
    now = 1_000_000
    impossible = QuotaFact("claude", 10.0, resets_at=now + FIVE_HOUR_SECONDS + 10_000,
                           observed_at=now, provenance="claude:rate_limit_event")
    assert quota.effective_usage(impossible, now) is None


def test_a_fact_observed_before_its_own_window_fails_closed(tmp_path):
    store = tmp_path / "records.db"
    now = 1_000_000
    # observed_at sits before the window [resets_at - 5h, resets_at] — inconsistent, so it never
    # persists and a hand-written copy is rejected on read.
    stale = QuotaFact("claude", 10.0, resets_at=now, observed_at=now - FIVE_HOUR_SECONDS - 5000,
                      provenance="claude:rate_limit_event")
    quota.record_quota(store, stale)
    assert quota.read_quota(store, "claude") is None
    assert quota.effective_usage(stale, now) is None


def test_build_fact_rejects_out_of_range_or_non_finite_inputs():
    now = 1_000_000
    assert build_fact("claude", 150.0, now + 100, now, "p") is None          # >100 utilization
    assert build_fact("claude", -1.0, now + 100, now, "p") is None           # negative utilization
    assert build_fact("claude", float("nan"), now + 100, now, "p") is None   # non-finite
    assert build_fact("claude", 10.0, "soon", now, "p") is None              # non-numeric reset
    assert build_fact("claude", 10.0, now + 100, now, "p") is not None       # a clean reading


def test_a_malformed_persisted_fact_reads_as_none(tmp_path):
    store = tmp_path / "records.db"
    quota.record_quota(store, _fresh())
    # Corrupt the on-disk fact: a bad utilization must fail closed, not admit at a bogus reading.
    path = quota.quota_dir(store) / "claude.json"
    data = json.loads(path.read_text())
    data["used_percent"] = "lots"
    path.write_text(json.dumps(data))
    assert quota.read_quota(store, "claude") is None


def test_the_freshest_write_replaces_the_pool_fact_in_place(tmp_path):
    store = tmp_path / "records.db"
    quota.record_quota(store, _fresh(used=20.0))
    quota.record_quota(store, _fresh(used=70.0))
    files = list(quota.quota_dir(store).iterdir())
    assert len(files) == 1                                  # one fact per pool, replaced in place
    assert quota.read_quota(store, "claude").used_percent == 70.0
