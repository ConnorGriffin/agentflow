"""The one way to name a file inside agentflow's own state directory."""

import pytest

from agentflow.state import OutsideStateDirectory, state_dir, state_path


def test_the_state_directory_follows_the_operator_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    assert state_dir() == tmp_path.resolve()


def test_an_unset_setting_falls_back_to_the_home_directory(monkeypatch):
    monkeypatch.delenv("AGENTFLOW_STATE", raising=False)
    assert state_dir().name == ".agentflow"


def test_segments_name_a_file_inside_the_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    assert state_path("notifications", "abc.sent") == tmp_path.resolve() / "notifications/abc.sent"


def test_a_segment_that_climbs_out_is_refused(monkeypatch, tmp_path):
    """Nothing composes such a segment today. The point is that it could not be followed if it
    did — containment is this module's property, not a promise about every call site."""
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))
    with pytest.raises(OutsideStateDirectory):
        state_path("..", "..", "etc", "passwd")


def test_an_absolute_segment_cannot_ignore_the_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))
    with pytest.raises(OutsideStateDirectory):
        state_path("/etc/passwd")
