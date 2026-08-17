"""The canonical screenshot harness ships with agentflow and bakes in the sandbox recipe.

Before this script existed, every session that needed UI proof-of-match rediscovered the
same sandbox walls (crashpad, sockaddr_un path limit, npm cache, page.route vs addInitScript,
context.close() killing single-process browsers) — 40+ hard failures in the corpus.

These tests assert the static properties that prevent regression to any of those failure modes.
A test fails at the *code pattern* level so that a wrong edit fails immediately with a clear
message, not at browser-launch time with an opaque Chrome error.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "screenshots.mjs"

HARNESS_REF = "node scripts/screenshots.mjs"  # the exact reference sessions are told to use


def _strip_js_comments(src: str) -> str:
    """Remove JS comments so tests only match live code, not documentation strings."""
    # Block comments first (/* ... */), then line comments (//).
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    return src


# --- the script itself -------------------------------------------------------


def test_script_exists():
    """The harness must be checked in — if it's absent, every other test is vacuous."""
    assert SCRIPT.exists(), (
        f"scripts/screenshots.mjs is missing — sessions have no canonical harness to call "
        f"and will hand-roll their own, rediscovering the same sandbox failures."
    )


@pytest.fixture(scope="module")
def src() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def code(src) -> str:
    """Source with comments stripped — tests that check for forbidden patterns use this."""
    return _strip_js_comments(src)


def test_no_session_time_npm(code):
    """The harness must not install packages at session time.

    The agent sandbox blocks npm installs (root-owned cache, no network in most runs).
    Playwright must be pre-installed globally; the script imports it directly.
    """
    for forbidden in ("npm install", "npm ci", "npm i "):
        assert forbidden not in code, (
            f"harness contains '{forbidden}' — sessions cannot install npm packages; "
            f"playwright must be imported from the globally installed path."
        )


def test_uses_playwright_global(code):
    """The harness must resolve Playwright's globally installed module, not a local node_modules.

    The global install has its own managed Chromium — short profile dirs, no crashpad
    path issues, and no --user-data-dir pointing into the deep worktree tree. Resolution
    is dynamic (bare import, then `npm root -g`) with the Homebrew path as a last fallback,
    so the harness stays portable across machines with different install locations.
    """
    assert "npm root -g" in code, (
        "harness does not resolve the global playwright path dynamically — a single "
        "hardcoded path breaks on any machine whose install location differs."
    )
    assert "/opt/homebrew/lib/node_modules/playwright" in code, (
        "harness dropped the Homebrew global path fallback — the macOS dev machines "
        "rely on it when npm is not on PATH."
    )


def test_no_sandbox_flag_present(src):
    """--no-sandbox is required — without it the OS blocks the Chrome process (SIGTRAP)."""
    assert "--no-sandbox" in src, (
        "harness is missing --no-sandbox — Chromium will SIGTRAP in the agent sandbox."
    )


def test_single_process_flag_present(src):
    """--single-process is required for the agent sandbox (port-bind is EPERM there)."""
    assert "--single-process" in src, (
        "harness is missing --single-process — Chromium crashes or hangs in the sandbox."
    )


def test_no_context_close(code):
    """context.close() kills a --single-process browser before screenshot writes flush.

    The correct pattern is browser.close() at the end, with the context left open.
    This test prevents a well-meaning cleanup from breaking the harness.
    """
    assert "context.close()" not in code, (
        "harness calls context.close() — with --single-process this kills the browser "
        "process before the screenshot write flushes, producing a zero-byte PNG."
    )


def test_fetch_stubbed_via_init_script(src):
    """page.route does not intercept file:// fetches; addInitScript is required instead."""
    assert "addInitScript" in src, (
        "harness does not use addInitScript for fetch stubbing — page.route silently "
        "skips file:// requests, so SPA data calls return nothing and the page renders empty."
    )


def test_self_check_mode_present(src):
    """--self-check mode lets a session verify the harness works before a full capture run."""
    assert "--self-check" in src, (
        "harness has no --self-check mode — sessions cannot verify the recipe works "
        "before committing to a full screenshot run."
    )


def test_no_user_data_dir_deep_path(code):
    """The harness must not set --user-data-dir to a path inside the worktree.

    Deep worktree paths bust the 104-char sockaddr_un limit and abort the browser.
    Playwright's default managed tmpdir is short; we don't override it.
    """
    assert "--user-data-dir" not in code, (
        "harness sets --user-data-dir — if that path is inside the worktree it will "
        "exceed the 104-char sockaddr_un limit and crash the browser at socket bind."
    )


# --- through the harness's own interface -------------------------------------

# Nonzero exits that mean "this environment can't run the harness" (Playwright or its
# managed Chromium absent) rather than "the harness is broken" — we skip on these.
_ENV_UNAVAILABLE = (
    "Cannot load playwright",
    "Executable doesn't exist",
    "playwright install",
    "Host system is missing",
    "browserType.launch",
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available to run the harness")
def test_self_check_produces_png_through_interface(tmp_path):
    """Drive the harness through its real --self-check interface and confirm a PNG lands.

    The other tests assert source patterns; this one actually runs the recipe, so a runtime
    regression that keeps the strings intact still fails here. It also uses a *relative*
    --out-dir (run from tmp_path) to exercise the path that used to abort with an invalid
    file:// URL. Skips cleanly where Playwright/Chromium isn't installed.
    """
    result = subprocess.run(
        ["node", str(SCRIPT), "--self-check", "--out-dir", "sc_out"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0 and any(marker in output for marker in _ENV_UNAVAILABLE):
        pytest.skip("Playwright/Chromium not available in this environment")

    assert result.returncode == 0, f"self-check exited nonzero:\n{output}"
    pngs = list((tmp_path / "sc_out").glob("*.png"))
    assert pngs, f"self-check wrote no PNG under a relative --out-dir:\n{output}"
    assert pngs[0].stat().st_size > 0, "self-check produced a zero-byte PNG"


# --- the prompts reference the script ----------------------------------------


def test_build_prompt_references_harness():
    """BUILD_PROMPT must point sessions at the canonical script, not just 'headless Playwright'."""
    from agentflow.prompts import BUILD_PROMPT
    assert HARNESS_REF in BUILD_PROMPT, (
        "BUILD_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "build sessions will hand-roll their own harness and rediscover the sandbox walls."
    )


def test_revise_prompt_references_harness():
    """REVISE_PROMPT must point revise sessions at the canonical script."""
    from agentflow.prompts import REVISE_PROMPT
    assert HARNESS_REF in REVISE_PROMPT, (
        "REVISE_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "revise sessions may write a new harness when refreshing stale screenshots."
    )


def test_respond_prompt_references_harness():
    """RESPOND_PROMPT must point respond sessions at the canonical script."""
    from agentflow.prompts import RESPOND_PROMPT
    assert HARNESS_REF in RESPOND_PROMPT, (
        "RESPOND_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "respond sessions may write a new harness when evidence is requested."
    )


def test_produce_prompt_references_harness():
    """PRODUCE_PROMPT must point mockup sessions at the canonical script."""
    from agentflow.prompts import PRODUCE_PROMPT
    assert HARNESS_REF in PRODUCE_PROMPT, (
        "PRODUCE_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "mockup sessions will hand-roll their own harness for variant screenshots."
    )


def test_review_prompt_references_harness():
    """REVIEW_PROMPT must too — a reviewer that changes user-facing output refreshes the evidence."""
    from agentflow.reviewer import REVIEW_PROMPT
    assert HARNESS_REF in REVIEW_PROMPT, (
        "REVIEW_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "review sessions will hand-roll their own harness when refreshing evidence."
    )


def _prompts_that_demand_screenshots():
    """Every spawned-session prompt that can require UI evidence, by name."""
    from agentflow import prompts
    from agentflow.reviewer import REVIEW_PROMPT
    return {
        "BUILD_PROMPT": prompts.BUILD_PROMPT,
        "REVISE_PROMPT": prompts.REVISE_PROMPT,
        "RESPOND_PROMPT": prompts.RESPOND_PROMPT,
        "PRODUCE_PROMPT": prompts.PRODUCE_PROMPT,
        "REVIEW_PROMPT": REVIEW_PROMPT,
    }


def test_every_prompt_carries_the_one_owned_clause():
    """All five must carry the shared clause verbatim, not a hand-copied paraphrase.

    They drifted apart once already — the reviewer's copy had no escape hatch at all — so the
    clause lives in one module and each prompt splices it in.
    """
    from agentflow.screenshot_crib import SCREENSHOT_HARNESS
    for name, prompt in _prompts_that_demand_screenshots().items():
        assert SCREENSHOT_HARNESS in prompt, (
            f"{name} does not carry the shared SCREENSHOT_HARNESS clause — a local paraphrase "
            f"drifts, and drift is what stranded PR #475."
        )


def test_prompts_tell_a_repo_without_the_harness_what_to_do():
    """The instruction must not be a dead end in a repo that has no harness yet.

    Three review attempts on one PR hit exactly this: told to run a script the repo did not
    carry, and forbidden to write one, each spent its whole turn budget improvising Playwright
    and returned no verdict. Naming the script is not enough — the absent case needs an answer.
    """
    from agentflow.screenshot_crib import SCREENSHOT_HARNESS
    assert "has no `scripts/screenshots.mjs`" in SCREENSHOT_HARNESS, (
        "the shared clause no longer addresses a repo without the harness — sessions there are "
        "ordered to run a script that does not exist, with no way out."
    )
    assert "port agentflow's" in SCREENSHOT_HARNESS, (
        "the shared clause no longer tells a session to port the canonical harness in — without "
        "that, the repo never acquires one and every future session pays the same cost."
    )


def test_shared_clause_has_only_its_one_deliberate_placeholder():
    """The prompts carrying it are str.format-rendered, so the only braces allowed are the one
    deliberate ``{screenshot_entry_note}`` placeholder every render site fills (#735); any other
    brace would break every render."""
    from agentflow.screenshot_crib import SCREENSHOT_HARNESS
    assert "{screenshot_entry_note}" in SCREENSHOT_HARNESS, (
        "SCREENSHOT_HARNESS lost its repo-local capture-entry placeholder — render sites thread a "
        "note through it."
    )
    residue = SCREENSHOT_HARNESS.replace("{screenshot_entry_note}", "")
    assert "{" not in residue and "}" not in residue, (
        "SCREENSHOT_HARNESS contains a stray format brace beyond its one placeholder — every "
        "prompt that splices it in will raise at render time."
    )


def test_build_prompt_names_the_repo_local_capture_entry_only_when_declared(tmp_path):
    """A rendered BUILD prompt redirects to the declared wrapper when the repo declares one, and
    carries no local-entry sentence otherwise — the canonical harness stands alone (#735)."""
    from agentflow.prompts import stage_prompt_spec
    from agentflow.repo_facts import screenshot_entry
    from agentflow.screenshot_crib import screenshot_entry_note

    def render(workdir):
        return stage_prompt_spec("build").render(
            repo="o/r", n=1, title="t", body="b", effort="deep", surfaces="`frontend/`",
            screenshot_entry_note=screenshot_entry_note(screenshot_entry(str(workdir))))

    (tmp_path / "AGENTS.md").write_text(
        "# repo\n\nui-surfaces: frontend/\nscreenshot-entry: scripts/screenshots.local.mjs\n")
    declared = render(tmp_path)
    assert "scripts/screenshots.local.mjs" in declared
    assert "local capture entry point" in declared

    (tmp_path / "AGENTS.md").write_text("# repo\n\nui-surfaces: frontend/\n")
    assert "local capture entry point" not in render(tmp_path)
