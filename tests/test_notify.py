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


def test_empty_message_becomes_space():
    a = _build_args("t", "", "", "https://ntfy/x")
    assert a[-2] == " "  # ntfy rejects an empty body


def test_notify_reports_delivery_result(monkeypatch):
    monkeypatch.setattr(notify_module.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert notify_module.notify("t", "m") is True

    monkeypatch.setattr(notify_module.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=22))
    assert notify_module.notify("t", "m") is False
