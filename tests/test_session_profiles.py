"""Per-stage session profiles: tool allowlist, empty MCP, per-cell ceilings, fail-closed (ADR 0044).

Every assertion here drives the public seam a real launch uses — ``provider_command(record)``
for the command a stage is launched with, ``LocalLauncher`` for the per-record wall ceiling,
and ``classify_claude`` / the coordinator ``cycle`` for how a withheld-tool reach and a ceiling
hit end. Each would fail against the pre-#242 uniform launch (one full surface, personal MCP,
one two-hour timeout, no turn ceiling).
"""

from __future__ import annotations

import json

from agentflow.coordinator.launcher import LocalLauncher
from agentflow.coordinator.providers import (ProviderCause, ProviderObservation,
                                             classify_claude, provider_command)
from agentflow.coordinator import Submission
from agentflow.coordinator.record import Record
from tests.conftest import FakeSession, record_of

_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}


def _record(stage: str, source: str, **kw) -> Record:
    return Record(f"claude-{stage}", stage, "claude", 1,
                  model="opus", source=source, input_ptr="do the stage", **kw)


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def test_read_only_intake_and_review_drop_edits_pin_mcp_and_cap_turns(tmp_path):
    """An Intake or Review is launched with a read/search allowlist (edit tools absent from the
    loaded surface), an empty MCP set, a turn ceiling, and a deny backstop — none of which the
    old uniform full-surface launch carried."""
    for stage, expected_tools in (
        ("intake", ("Read", "Bash", "Grep", "Glob", "ToolSearch", "WebFetch")),
        ("review", ("Read", "Bash", "Grep", "Glob", "ToolSearch")),
    ):
        cmd = provider_command(_record(stage, str(tmp_path)))
        tools = _flag(cmd, "--tools").split(",")
        assert tuple(tools) == expected_tools
        assert not (_EDIT_TOOLS & set(tools))  # no edit tools in the loaded surface

        # MCP pinned empty (the personal-connector leak closes here), not merely project-scoped.
        assert "--strict-mcp-config" in cmd
        assert json.loads(_flag(cmd, "--mcp-config")) == {"mcpServers": {}}

        # A real turn ceiling replaces the absent one; both read-only stages cap at 40 turns.
        assert _flag(cmd, "--max-turns") == "40"

        # The settings deny is the fail-closed backstop for the withheld edit tools.
        deny = json.loads(_flag(cmd, "--settings"))["permissions"]["deny"]
        assert set(deny) == _EDIT_TOOLS


def test_build_keeps_the_full_edit_surface(tmp_path):
    """A Build session retains its full edit/test surface: no allowlist narrows it and no deny
    block withholds edit tools, while it still gets the empty MCP pin and a turn ceiling."""
    cmd = provider_command(_record("build", str(tmp_path), complexity="deep", effort="extra"))
    assert "--tools" not in cmd                       # full surface, unrestricted
    assert "permissions" not in json.loads(_flag(cmd, "--settings"))
    assert "--strict-mcp-config" in cmd               # MCP still pinned empty
    assert _flag(cmd, "--max-turns") == "300"         # Build deep/extra ceiling (§3b)


def test_revise_inherits_the_original_builders_build_ceiling(tmp_path):
    """Revise carries the builder's Build ceiling via ``builder_complexity`` (ADR 0041), not a
    read-only stage's, so a deep builder's revise gets the deep Build turn ceiling."""
    deep = provider_command(_record(
        "revise", str(tmp_path), builder_complexity="deep", effort="extra"))
    assert _flag(deep, "--max-turns") == "300"        # = Build deep/extra
    assert "--tools" not in deep                       # code-writing surface, not read-only

    standard = provider_command(_record(
        "revise", str(tmp_path), builder_complexity="standard", effort="low"))
    assert _flag(standard, "--max-turns") == "80"     # = Build standard/low


def test_wall_ceiling_is_threaded_per_record_from_the_profile(tmp_path):
    """The wall-clock ceiling is a per-record value the stage profile supplies — no longer one
    launcher-wide two-hour constant. An explicit constructor override still wins (tests/ops)."""
    launcher = LocalLauncher()
    assert launcher._session_timeout_for(_record("intake", str(tmp_path))) == 20 * 60
    assert launcher._session_timeout_for(_record("review", str(tmp_path))) == 15 * 60
    assert launcher._session_timeout_for(
        _record("build", str(tmp_path), complexity="deep", effort="extra")) == 60 * 60

    pinned = LocalLauncher(session_timeout=0.1)
    assert pinned._session_timeout_for(_record("build", str(tmp_path))) == 0.1


def test_a_ceiling_hit_ends_as_a_recoverable_timeout():
    """Hitting the turn ceiling is a recoverable TIMEOUT-class end (like the wall deadline), not
    an incomplete PROCESS end — so a legitimate ceiling kill continues within budget (ADR 0044)."""
    obs = classify_claude([{"type": "result", "subtype": "error_max_turns"}])
    assert obs.cause is ProviderCause.TIMEOUT
    assert obs.classification() == "recoverable"


def test_withheld_tool_reach_is_classified_a_permanent_hold_naming_the_capability():
    """A read-only session that reaches for a withheld capability gets a denial tool_result; the
    classifier fails it closed to a PERMANENT hold whose detail names the tool — and that wins
    even over a following clean result, so a stray edit is never silently swallowed (§3c)."""
    obs = classify_claude([
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Write",
             "input": {"file_path": "x", "content": "y"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "Permission to use Write has been denied."}]}},
        {"type": "result", "subtype": "success", "result": "done anyway"},
    ])
    assert obs.cause is ProviderCause.PERMANENT
    assert obs.classification() == "permanent"
    assert "Write" in obs.detail


def test_withheld_tool_reach_holds_for_a_human_through_the_coordinator(make_coord):
    """Through the public cycle: a read-only stage whose provider reached for a withheld tool
    ends held for a human, with the hold reason naming the withheld capability — never a silent
    success or degradation."""
    fake = FakeSession()

    class _WithheldEdit:
        def observe(self, record):
            return ProviderObservation(
                cause=ProviderCause.PERMANENT, has_end_fact=True,
                detail="withheld capability: Write")

        def verify(self, record, obs):
            return False

    coord = make_coord(fake, adapter=_WithheldEdit())
    identity = coord.submit_stage(Submission(
        repo="o/r", subject="stray-edit", stage="review", pool="claude"))
    coord.cycle("claude")          # launches the attempt
    fake.kill(identity)            # its family ends (the scripted observation supplies the cause)
    coord.cycle("claude")          # observed and settled

    durable = record_of(coord, identity)
    assert durable.state == "held"
    assert "Write" in durable.hold_reason and "permanent" in durable.hold_reason
