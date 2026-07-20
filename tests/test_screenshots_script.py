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


def test_uses_playwright_global(src):
    """The harness must use Playwright's globally installed module, not a local node_modules.

    The global install has its own managed Chromium — short profile dirs, no crashpad
    path issues, and no --user-data-dir pointing into the deep worktree tree.
    """
    assert "/opt/homebrew/lib/node_modules/playwright" in src, (
        "harness does not reference the globally installed playwright path — "
        "sessions may try a local install or raw chrome, both of which fail in the sandbox."
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


# --- the prompts reference the script ----------------------------------------


def test_build_prompt_references_harness():
    """BUILD_PROMPT must point sessions at the canonical script, not just 'headless Playwright'."""
    from agentflow.loop import BUILD_PROMPT
    assert HARNESS_REF in BUILD_PROMPT, (
        "BUILD_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "build sessions will hand-roll their own harness and rediscover the sandbox walls."
    )


def test_revise_prompt_references_harness():
    """REVISE_PROMPT must point revise sessions at the canonical script."""
    from agentflow.loop import REVISE_PROMPT
    assert HARNESS_REF in REVISE_PROMPT, (
        "REVISE_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "revise sessions may write a new harness when refreshing stale screenshots."
    )


def test_respond_prompt_references_harness():
    """RESPOND_PROMPT must point respond sessions at the canonical script."""
    from agentflow.loop import RESPOND_PROMPT
    assert HARNESS_REF in RESPOND_PROMPT, (
        "RESPOND_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "respond sessions may write a new harness when evidence is requested."
    )


def test_produce_prompt_references_harness():
    """PRODUCE_PROMPT must point mockup sessions at the canonical script."""
    from agentflow.loop import PRODUCE_PROMPT
    assert HARNESS_REF in PRODUCE_PROMPT, (
        "PRODUCE_PROMPT does not reference 'node scripts/screenshots.mjs' — "
        "mockup sessions will hand-roll their own harness for variant screenshots."
    )
