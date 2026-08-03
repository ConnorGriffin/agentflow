"""Per-stage session profiles: tool allowlist, empty MCP, per-cell ceilings, fail-closed (ADR 0044).

Every assertion here drives the public seam a real launch uses — ``provider_command(record)``
for the command a stage is launched with, ``LocalLauncher`` for the per-record wall ceiling,
and ``classify_claude`` for how a ceiling hit ends. Each would fail against the pre-#242 uniform
launch (one full surface, personal MCP, one two-hour timeout, no turn ceiling).

Fail-closed is delivered by *unreachability*, not by catching a denied call: the read-only launch
strips the edit tools from the loaded surface (allowlist + an independent deny strip), verified at
the command seam below. There is no "permission denied" event to key on because a stripped tool has
no schema for the model to call — so no test fabricates one (ADR 0044 pt 5).
"""

from __future__ import annotations

import json
from pathlib import Path

from agentflow import runner as runner_mod
from agentflow.coordinator.launcher import LocalLauncher
from agentflow.coordinator.providers import (ProviderCause, classify_claude,
                                             provider_command)
from agentflow.coordinator.record import Record

_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}


def _record(stage: str, source: str, **kw) -> Record:
    return Record(f"claude-{stage}", stage, "claude", 1,
                  model="opus", source=source, input_ptr="do the stage", **kw)


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def test_every_automated_stage_receives_the_canonical_charter(tmp_path):
    charter = (Path(__file__).parents[1] / "standards" / "CHARTER.md").read_text()
    stages = ("intake", "build", "review", "respond", "converse",
              "research", "mockup", "revise")

    for pool, model in (("claude", "opus"), ("codex", "gpt-5.6-sol")):
        for stage in stages:
            record = Record(
                f"{pool}-{stage}", stage, pool, 1,
                model=model, source=str(tmp_path), input_ptr="do the stage",
                complexity="deep", effort="high", builder_complexity="deep",
            )
            cmd = provider_command(record)
            prompt = _flag(cmd, "-p") if pool == "claude" else cmd[-1]
            assert charter in prompt


def test_read_only_intake_drops_edits_pins_mcp_and_caps_turns(monkeypatch, tmp_path):
    """Intake is launched with a read/search allowlist (edit tools absent from the
    loaded surface), an MCP set pinned to strict mode, a turn ceiling, and a deny backstop — none
    of which the old uniform full-surface launch carried."""
    # No Codebase Memory server → strict mode alone keeps personal connectors out (#244); the
    # code-graph re-supply is exercised in test_runner.py.
    monkeypatch.setattr(runner_mod, "_codebase_memory_mcp_servers", lambda: {})
    for stage, expected_tools in (
        ("intake", ("Read", "Bash", "Grep", "Glob", "ToolSearch", "WebFetch")),
    ):
        cmd = provider_command(_record(stage, str(tmp_path)))
        tools = _flag(cmd, "--tools").split(",")
        assert tuple(tools) == expected_tools
        assert not (_EDIT_TOOLS & set(tools))  # no edit tools in the loaded surface

        # Strict mode closes the personal-connector leak; with no code graph nothing is re-supplied.
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" not in cmd

        # A real turn ceiling replaces the absent one, set above intake's recorded maximum (#410).
        assert _flag(cmd, "--max-turns") == "80"

        # The settings deny independently strips the same edit tools — the fail-closed backstop
        # (ADR 0044 pt 5). Both the allowlist above and this deny remove the tools from the loaded
        # surface, so a read-only stage has no edit-tool schema to call: unreachable, not caught.
        deny = json.loads(_flag(cmd, "--settings"))["permissions"]["deny"]
        assert set(deny) == _EDIT_TOOLS


def test_read_only_stages_keep_codebase_memory(monkeypatch, tmp_path):
    """A read-only stage reaches the re-supplied Codebase Memory server:
    each supplied server is allowlisted alongside the §3a read/search tools, so an exploration
    stage keeps the same code-graph access Build has (#244) while still losing the edit tools."""
    monkeypatch.setattr(runner_mod, "_codebase_memory_mcp_servers",
                        lambda: {"codebase-memory-mcp": {"command": "/x/code-graph"}})
    cmd = provider_command(_record("intake", str(tmp_path)))

    tools = _flag(cmd, "--tools").split(",")
    assert "mcp__codebase-memory-mcp" in tools           # code-graph reachable in the read-only surface
    assert tools[:4] == ["Read", "Bash", "Grep", "Glob"]  # §3a read/search set, verbatim, still first
    assert not (_EDIT_TOOLS & set(tools))                # edit tools still absent

    # The server itself is re-supplied under strict mode (#244), not merely allowlisted.
    mcp = json.loads(_flag(cmd, "--mcp-config"))
    assert list(mcp["mcpServers"]) == ["codebase-memory-mcp"]


def test_build_keeps_the_full_edit_surface(tmp_path):
    """A Build session retains its full edit/test surface: no allowlist narrows it and no deny
    block withholds edit tools, while it still gets the empty MCP pin and a turn ceiling."""
    cmd = provider_command(_record("build", str(tmp_path), complexity="deep", effort="extra"))
    assert "--tools" not in cmd                       # full surface, unrestricted
    assert "permissions" not in json.loads(_flag(cmd, "--settings"))
    assert "--strict-mcp-config" in cmd               # MCP still pinned empty
    assert _flag(cmd, "--max-turns") == "300"         # Build deep/extra ceiling (§3b)


def test_review_keeps_the_full_edit_surface_for_bounded_fixes(tmp_path):
    """Review is no longer report-only: the independent reviewer can edit, test, commit, and push
    clear fixes while retaining Review's tighter wall/turn ceiling."""
    cmd = provider_command(_record("review", str(tmp_path)))
    assert "--tools" not in cmd
    assert "permissions" not in json.loads(_flag(cmd, "--settings"))
    assert "--strict-mcp-config" in cmd
    assert _flag(cmd, "--max-turns") == "120"


def test_revise_inherits_the_original_builders_build_ceiling(tmp_path):
    """Revise carries the builder's exact Build ceiling through both retained dials."""
    from agentflow.coordinator.profiles import profile_for

    expected = {
        ("deep", "low"): (45 * 60, 200),
        ("deep", "medium"): (45 * 60, 200),
        ("deep", "high"): (45 * 60, 200),
        ("deep", "extra"): (60 * 60, 300),
        ("standard", "low"): (45 * 60, 160),
        ("standard", "medium"): (45 * 60, 160),
        ("standard", "high"): (45 * 60, 160),
        ("standard", "extra"): (45 * 60, 160),
    }
    for (complexity, effort), ceiling in expected.items():
        record = _record(
            "revise", str(tmp_path), builder_complexity=complexity, effort=effort)
        profile = profile_for(record)
        assert (profile.wall_ceiling_s, profile.turn_ceiling) == ceiling
        command = provider_command(record)
        assert _flag(command, "--max-turns") == str(ceiling[1])
        assert "--tools" not in command               # code-writing surface, not read-only


def test_wall_ceiling_is_threaded_per_record_from_the_profile(tmp_path):
    """The wall-clock ceiling is a per-record value the stage profile supplies — no longer one
    launcher-wide two-hour constant. An explicit constructor override still wins (tests/ops)."""
    launcher = LocalLauncher()
    assert launcher._session_timeout_for(_record("intake", str(tmp_path))) == 20 * 60
    assert launcher._session_timeout_for(_record("review", str(tmp_path))) == 30 * 60
    assert launcher._session_timeout_for(
        _record("build", str(tmp_path), complexity="deep", effort="extra")) == 60 * 60
    # The standard tier carries the deep tier's 45-minute wall: its drafted 25 minutes was never
    # measured, and at recorded pace the raised 160-call allowance straddles it (§3b‴, #421).
    assert launcher._session_timeout_for(
        _record("build", str(tmp_path), complexity="standard", effort="low")) == 45 * 60

    pinned = LocalLauncher(session_timeout=0.1)
    assert pinned._session_timeout_for(_record("build", str(tmp_path))) == 0.1


def test_profile_maps_each_effort_rung_for_build_and_revise_only(tmp_path):
    """The profile is the one source of truth for the effort→reasoning mapping: each dial rung
    resolves to its reasoning rung for build and revise, and every other stage stays ``None``
    (provider default)."""
    from agentflow.coordinator.profiles import profile_for

    mapping = {"low": "low", "medium": "medium", "high": "high", "extra": "xhigh"}
    for dial, rung in mapping.items():
        assert profile_for(
            _record("build", str(tmp_path), effort=dial)).reasoning_effort == rung
        assert profile_for(
            _record("revise", str(tmp_path), builder_complexity="deep", effort=dial)
        ).reasoning_effort == rung
    for stage in ("intake", "review", "research", "respond", "converse", "mockup"):
        assert profile_for(_record(stage, str(tmp_path), effort="extra")).reasoning_effort is None


def test_out_of_ladder_reasoning_rung_clamps_to_the_provider_top(tmp_path):
    """A rung above a provider's ladder clamps to its top rung rather than failing the launch
    (ADR 0046) — inert today (both providers accept every mapped rung), defensive for a future
    model with a shorter ladder."""
    from agentflow.runner import _clamp_reasoning

    assert _clamp_reasoning("xhigh", ("low", "medium", "high", "xhigh", "max")) == "xhigh"
    assert _clamp_reasoning("xhigh", ("low", "medium", "high")) == "high"  # clamped to top


def _codex_record(stage: str, source: str, **kw) -> Record:
    return Record(f"codex-{stage}", stage, "codex", 1,
                  model="sol", source=source, input_ptr="do the stage", **kw)


def test_build_launches_at_the_mapped_reasoning_rung_on_both_providers(tmp_path):
    """The effort dial reaches each provider's reasoning knob at launch (ADR 0046): extra→xhigh,
    low→low. Claude via its first-class ``--effort`` flag; Codex via the ``model_reasoning_effort``
    config override — Codex has no ``--effort`` flag, so it never appears."""
    for effort, rung in (("extra", "xhigh"), ("low", "low")):
        claude = provider_command(_record("build", str(tmp_path), complexity="deep", effort=effort))
        assert _flag(claude, "--effort") == rung

        codex = provider_command(
            _codex_record("build", str(tmp_path), complexity="deep", effort=effort))
        codex_config = [codex[i + 1] for i, arg in enumerate(codex[:-1]) if arg == "-c"]
        assert f"model_reasoning_effort={rung}" in codex_config
        assert "--effort" not in codex                 # Codex has no such flag


def test_revise_launches_at_the_same_reasoning_rung_as_its_builder(tmp_path):
    """A Revise carries the same ``effort`` dial as its builder (ADR 0041), so it launches at the
    same reasoning rung — an extra-effort builder's revise reasons at ``xhigh`` on both providers."""
    claude = provider_command(
        _record("revise", str(tmp_path), builder_complexity="deep", effort="extra"))
    assert _flag(claude, "--effort") == "xhigh"

    codex = provider_command(
        _codex_record("revise", str(tmp_path), builder_complexity="deep", effort="extra"))
    codex_config = [codex[i + 1] for i, arg in enumerate(codex[:-1]) if arg == "-c"]
    assert "model_reasoning_effort=xhigh" in codex_config


def test_non_build_stages_set_no_reasoning_flag(tmp_path):
    """Intake and Review keep the provider default — no reasoning flag reaches either CLI (ADR
    0044/0046: their reasoning cells stay unset/tunable)."""
    for stage in ("intake", "review"):
        claude = provider_command(_record(stage, str(tmp_path)))
        assert "--effort" not in claude

        codex = provider_command(_codex_record(stage, str(tmp_path)))
        codex_config = [codex[i + 1] for i, arg in enumerate(codex[:-1]) if arg == "-c"]
        assert not any(v.startswith("model_reasoning_effort=") for v in codex_config)


def _records_a_reading_covers(stage, complexity, effort, source, build_cells):
    """Every record one recorded reading has to hold against.

    A build reading is a reading of one cell, so it holds against that cell. A revise reading is a
    reading of the whole build tier revise inherits (ADR 0041), so it holds against every effort
    that tier can resolve to — the named cells and the unnamed fallback alike. Everything else
    resolves per stage, with no cell to vary."""
    if stage == "revise":
        efforts = [e for (c, e) in build_cells if c == complexity] + [None]
        return [_record(stage, source, builder_complexity=complexity, effort=e) for e in efforts]
    if complexity is None:
        return [_record(stage, source)]
    return [_record(stage, source, complexity=complexity, effort=effort)]


def test_every_ceiling_stays_clear_of_the_work_its_cell_actually_does(tmp_path):
    """A ceiling exists to kill a runaway, so it has to sit clear of the work the cell it applies to
    is recorded needing. Review's drifted under it: drawn at 40 from an n=70 sample, it was below the
    p90 (55 tool calls) by the time the sample reached 210, and 31 reviews were killed at it having
    already read the diff and run the suite (#410). Build's standard tier had the same fault hidden
    by a pooled figure: its cap sat on the 80 tool calls a standard/low build does, and six standard
    builds and revisions were killed at it (#416). And the wall is held to the same rule as the
    turns: the standard tier's wall had never been measured when its turn ceiling doubled, and the
    work the raise admits straddled it (#421). This pins every cell — both dials — against the
    recorded distribution it was set from, so the next drift fails here instead of parking a pull
    request.

    The bar is the p95 with headroom, not the maximum: the tail is unbounded (one review ran 335
    tool calls) and no ceiling that still kills a runaway could clear it."""
    from agentflow.coordinator.profiles import (_BUILD_CEILINGS, _OBSERVED_P95,
                                                profile_for)

    for (stage, complexity, effort), (p95_wall_s, p95_tool_calls) in _OBSERVED_P95.items():
        cell = "/".join(p for p in (stage, complexity, effort) if p)
        for record in _records_a_reading_covers(
                stage, complexity, effort, str(tmp_path), _BUILD_CEILINGS):
            profile = profile_for(record)
            assert profile.turn_ceiling > p95_tool_calls, (
                f"{cell}: {profile.turn_ceiling}-turn ceiling is not clear of the "
                f"{p95_tool_calls} tool calls its 95th-percentile session needs — ordinary long "
                f"sessions die at it")
            # Every reading carries both dials now (§3b‴ filled the build/revise walls), so the
            # wall is held unconditionally — a tool-calls-only pass may no longer arrive silently.
            assert profile.wall_ceiling_s > p95_wall_s, (
                f"{cell}: {profile.wall_ceiling_s}s wall is not clear of the {p95_wall_s}s "
                f"its 95th-percentile session needs — ordinary long sessions die at it")


def test_a_ceiling_hit_ends_as_a_recoverable_timeout():
    """Hitting the turn ceiling is a recoverable TIMEOUT-class end (like the wall deadline), not
    an incomplete PROCESS end — so a legitimate ceiling kill continues within budget (ADR 0044)."""
    obs = classify_claude([{"type": "result", "subtype": "error_max_turns"}])
    assert obs.cause is ProviderCause.TIMEOUT
    assert obs.classification() == "recoverable"
