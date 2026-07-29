from types import SimpleNamespace

from agentflow import notify as notify_module
from agentflow.notify import _build_args


def test_args_carry_title_and_message():
    a = _build_args("agentflow needs you", "#2 parked", "", "https://ntfy/x")
    assert "Title: agentflow needs you" in a
    assert a[-2:] == ["#2 parked", "https://ntfy/x"]
    assert "Click: " not in " ".join(a)


def test_click_header_added_only_with_url():
    a = _build_args("t", "m", "https://gh/pr/2", "https://ntfy/x")
    assert "Click: https://gh/pr/2" in a


def test_sequence_id_makes_retries_update_one_notification():
    a = _build_args("t", "m", "", "https://ntfy/x", "abc123")
    assert "X-Sequence-ID: abc123" in a


def test_empty_message_becomes_space():
    a = _build_args("t", "", "", "https://ntfy/x")
    assert a[-2] == " "  # ntfy rejects an empty body


def test_notify_reports_delivery_result(monkeypatch):
    monkeypatch.setattr(notify_module, "NTFY_URL", "https://ntfy/x")
    monkeypatch.setattr(notify_module.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert notify_module.notify("t", "m") is True

    monkeypatch.setattr(notify_module.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=22))
    assert notify_module.notify("t", "m") is False


def test_notify_is_disabled_without_an_explicit_url(monkeypatch):
    monkeypatch.setattr(notify_module, "NTFY_URL", "")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("disabled notifications must not make a network request")

    monkeypatch.setattr(notify_module.subprocess, "run", unexpected_run)
    assert notify_module.notify("t", "m") is False


def _delivering(calls, code=0):
    def run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=code)
    return run


def test_one_handoffs_ping_is_not_repeated_every_heartbeat(monkeypatch, tmp_path):
    """A stage whose bookkeeping keeps failing re-proves its handoff every cycle, so the ping is
    re-sent every cycle too (it is the crash-safe direction — ADR 0042). Bound it: the operator
    should hear about a stuck hold daily, not every few minutes."""
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setattr(notify_module, "NTFY_URL", "https://ntfy/x")
    calls = []
    monkeypatch.setattr(notify_module.subprocess, "run", _delivering(calls))

    for _ in range(5):
        assert notify_module.notify("agentflow needs you", "#2 held", sequence_id="abc123")
    assert len(calls) == 1


def test_the_repeat_window_expires_so_a_stuck_hold_keeps_nagging(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setattr(notify_module, "NTFY_URL", "https://ntfy/x")
    calls = []
    monkeypatch.setattr(notify_module.subprocess, "run", _delivering(calls))

    notify_module.notify("agentflow needs you", "#2 held", sequence_id="abc123")
    stale = notify_module._sent_marker("abc123")
    stale.write_text(str(float(stale.read_text()) - notify_module._REPEAT_WINDOW_SECONDS - 1))
    notify_module.notify("agentflow needs you", "#2 held", sequence_id="abc123")
    assert len(calls) == 2


def test_a_ping_that_was_never_delivered_does_not_start_the_window(monkeypatch, tmp_path):
    """Only a delivered ping counts as sent — a failed POST must not silence the retry."""
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setattr(notify_module, "NTFY_URL", "https://ntfy/x")
    calls = []
    monkeypatch.setattr(notify_module.subprocess, "run", _delivering(calls, code=1))

    assert notify_module.notify("agentflow needs you", "#2 held", sequence_id="abc123") is False
    assert notify_module.notify("agentflow needs you", "#2 held", sequence_id="abc123") is False
    assert len(calls) == 2


def test_distinct_handoffs_are_never_collapsed_into_one_ping(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setattr(notify_module, "NTFY_URL", "https://ntfy/x")
    calls = []
    monkeypatch.setattr(notify_module.subprocess, "run", _delivering(calls))

    notify_module.notify("agentflow needs you", "#2 held", sequence_id="aaa")
    notify_module.notify("agentflow needs you", "#3 held", sequence_id="bbb")
    assert len(calls) == 2


def test_a_key_this_module_did_not_mint_never_names_a_file(monkeypatch, tmp_path):
    """The recorded name is bounded to the hex alphabet the handoff keys use, so no caller can
    steer it at a path of its choosing. An unrecognized key costs a repeat ping, never a write."""
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setattr(notify_module, "NTFY_URL", "https://ntfy/x")
    calls = []
    monkeypatch.setattr(notify_module.subprocess, "run", _delivering(calls))

    assert notify_module._sent_marker("../../../etc/passwd") is None
    for _ in range(3):
        assert notify_module.notify("t", "m", sequence_id="../../etc/passwd")
    assert len(calls) == 3                      # never recorded, so never suppressed
    assert list(tmp_path.rglob("*passwd*")) == []
