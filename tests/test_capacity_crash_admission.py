"""A crashed capacity check must refuse admission *and* say what broke (#365).

Every refusal reason the fleet prints lives on a pool status object, but when the capacity
observation raises, admission bails out before any status exists — pre-#365 that refusal was
a bare False, indistinguishable in the log from ordinary pacing. These tests drive the real
production gate through the public coordinator seam and assert the crash now lands in the
same per-record deferral line healthy refusals use (#436): named against the record's own
pool, in the daemon's ``{type}: {message}`` error shape, once per pool per cycle — and that
the admission decision itself is byte-identical (still refused, still waiting).
"""

from __future__ import annotations

from conftest import record_of

from agentflow import balancer, pipeline
from agentflow.coordinator import Coordinator, Submission, tracer


def _crashing_query(monkeypatch):
    """Make every pool's capacity observation raise the way a malformed structured fact does.

    ``_query_pool`` already converts its own ``OSError``/timeout into the named "capacity
    helper unavailable" refusal; the gate's outer catch exists for everything *else*, so the
    crash here must be one of those — a bad output shape reading a quota field."""
    def _boom(_pool, *_args, **_kwargs):
        raise KeyError("used_percent")
    monkeypatch.setattr(balancer, "_query_pool", _boom)


def test_a_crashed_capacity_check_still_refuses_but_names_the_crash(coord_state, monkeypatch):
    """The surviving gap from #365: the observation raises, the record must stay waiting
    (fail safe — a crash never admits), and exactly one log line must name the record's pool,
    the exception type, and its message. Pre-#365 the exception vanished without trace."""
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [])
    _crashing_query(monkeypatch)

    lines: list[str] = []
    coord = Coordinator(gate=pipeline._production_gate(), log=lines.append)
    identity = coord.submit_stage(
        Submission(repo="o/r", subject="365", stage="review", pool="codex"))

    coord.cycle("codex", now=1_000_000)

    assert record_of(coord, identity).state == "waiting"    # still refused, still retried
    crashes = [l for l in lines if "capacity check failed" in l]
    assert len(crashes) == 1
    line = crashes[0]
    assert "codex" in line
    assert "KeyError: 'used_percent'" in line               # the daemon's {type}: {message} shape


def test_two_waiting_records_on_a_crashing_pool_emit_one_line(coord_state, monkeypatch):
    """One line per pool per cycle, never one per waiting record: a check that raises for one
    record on a pool raises for every record on that pool in that cycle, so a second line
    carries no new fact. The next cycle reports again — the crash is still the live cause.

    Silencing the *line* must not silence the *fact*, though (#405): both records keep the crash
    as their durable reason and both appear on the refusal board, or the second record would sit
    waiting with nothing anywhere saying why."""
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [])
    _crashing_query(monkeypatch)

    lines: list[str] = []
    coord = Coordinator(gate=pipeline._production_gate(), log=lines.append)
    coord.submit_stage(Submission(repo="o/r", subject="365", stage="review", pool="codex"))
    coord.submit_stage(Submission(repo="o/r", subject="366", stage="review", pool="codex"))

    coord.cycle("codex", now=1_000_000)
    assert len([l for l in lines if "capacity check failed" in l]) == 1

    published = tracer.refusal_projection(coord._store.load().values())
    assert len(published) == 2
    assert {row["refusal"] for row in published} == {
        "capacity check failed: KeyError: 'used_percent'"}

    coord.cycle("codex", now=1_000_000)                     # a fresh cycle reports afresh
    assert len([l for l in lines if "capacity check failed" in l]) == 2


def test_a_recovered_capacity_check_leaves_no_crash_on_a_later_paced_refusal(
        coord_state, monkeypatch):
    """A crash is only the live cause until the pool answers again. The first record's check
    raises; the next record retries, gets a real answer, and starts; a third is then held back by
    ordinary pacing. That third record must say nothing about a capacity-check failure — the
    reason on a record is what refused it *this* cycle, and publishing a crash that has already
    recovered invents a fleet problem that is not there."""
    from conftest import FakeSession

    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [])
    calls = [0]

    def _query(pool, *_args, **_kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise KeyError("used_percent")
        return balancer.PoolStatus(tool=pool, clear=True, spent_pct=10.0, active=True)

    monkeypatch.setattr(balancer, "_query_pool", _query)
    monkeypatch.setattr(balancer, "_codex_dispatch_status",
                        lambda status, *_a, **_k: status)

    fake = FakeSession()
    lines: list[str] = []
    coord = Coordinator(gate=pipeline._production_gate(), launcher=fake, adapter=fake,
                        log=lines.append)
    first = coord.submit_stage(Submission(repo="o/r", subject="365", stage="review", pool="codex"))
    second = coord.submit_stage(Submission(repo="o/r", subject="366", stage="review", pool="codex"))
    third = coord.submit_stage(Submission(repo="o/r", subject="367", stage="review", pool="codex"))

    coord.cycle("codex", now=1_000_000)

    records = {r.identity: r for r in coord._store.load().values()}
    assert "capacity check failed" in records[first].refusal
    assert records[second].state == "running"        # the retry answered, so this one launched
    assert records[third].state == "waiting"         # and this one is paced, not crashed
    assert "capacity check failed" not in records[third].refusal
    assert len([l for l in lines if "capacity check failed" in l]) == 1
