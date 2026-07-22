"""The provider-authored quota facts — the Claude dispatch authority (#305, #315).

Each window (five-hour and seven-day) is persisted as its own fact per pool beside the records
database, validated against its own window length before use, and read reset-aware: once the
observed window's reset has passed it reports no usage. Updating one window never erases the
other. A missing, malformed, or window-inconsistent fact fails closed (``None``) — it is never
coerced into a fabricated zero-usage reading.
"""

from __future__ import annotations

import json

from agentflow.coordinator import quota
from agentflow.coordinator.quota import (FIVE_HOUR_SECONDS, SEVEN_DAY, SEVEN_DAY_SECONDS,
                                         QuotaFact, build_fact)


def _fresh(pool="claude", used=30.0, *, now=1_000_000):
    return QuotaFact(pool=pool, used_percent=used, resets_at=now + 4 * 3600,
                     observed_at=now, provenance="claude:rate_limit_event")


def _fresh_weekly(pool="claude", used=10.0, *, now=1_000_000):
    return QuotaFact(pool=pool, used_percent=used, resets_at=now + 3 * SEVEN_DAY_SECONDS // 7,
                     observed_at=now, provenance="oauth:seven_day", window=SEVEN_DAY)


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
    path = quota.quota_dir(store) / "claude-five_hour.json"
    data = json.loads(path.read_text())
    data["used_percent"] = "lots"
    path.write_text(json.dumps(data))
    assert quota.read_quota(store, "claude") is None


def test_the_freshest_write_replaces_the_pool_fact_in_place(tmp_path):
    store = tmp_path / "records.db"
    quota.record_quota(store, _fresh(used=20.0))
    quota.record_quota(store, _fresh(used=70.0))
    files = list(quota.quota_dir(store).iterdir())
    assert len(files) == 1                                  # one fact per window, replaced in place
    assert quota.read_quota(store, "claude").used_percent == 70.0


def test_the_two_windows_are_independent_durable_facts(tmp_path):
    """Each window is its own fact: writing the seven-day fact never erases the five-hour one, and
    each round-trips independently (#315)."""
    store = tmp_path / "records.db"
    quota.record_quota(store, _fresh(used=42.0))
    quota.record_quota(store, _fresh_weekly(used=8.0))
    assert {p.name for p in quota.quota_dir(store).iterdir()} == {
        "claude-five_hour.json", "claude-seven_day.json"}
    assert quota.read_quota(store, "claude").used_percent == 42.0                # untouched
    assert quota.read_quota(store, "claude", SEVEN_DAY).used_percent == 8.0
    # Refreshing one window leaves the other in place.
    quota.record_quota(store, _fresh(used=55.0))
    assert quota.read_quota(store, "claude").used_percent == 55.0
    assert quota.read_quota(store, "claude", SEVEN_DAY).used_percent == 8.0


def test_a_seven_day_fact_is_validated_against_its_own_window_length(tmp_path):
    """The seven-day window is validated with its own length: a reset a few days out (temporally
    fine for seven days but impossible for five hours) is a trustworthy weekly fact and reads back
    reset-aware."""
    store = tmp_path / "records.db"
    now = 1_000_000
    weekly = _fresh_weekly(used=12.0, now=now)
    quota.record_quota(store, weekly)
    assert quota.read_quota(store, "claude", SEVEN_DAY) == weekly
    assert quota.effective_usage(weekly, now) == 12.0
    # The same reset time would be temporally impossible for a five-hour window.
    assert build_fact("claude", 12.0, weekly.resets_at, now, "p") is None


def test_an_unknown_window_fails_closed(tmp_path):
    """A fact naming a window with no defined length cannot be trusted, so it never builds or
    reads back (fail closed)."""
    now = 1_000_000
    assert build_fact("claude", 10.0, now + 100, now, "p", window="fortnight") is None
    store = tmp_path / "records.db"
    quota.record_quota(store, QuotaFact("claude", 10.0, now + 100, now, "p", window="fortnight"))
    assert quota.read_quota(store, "claude", "fortnight") is None
