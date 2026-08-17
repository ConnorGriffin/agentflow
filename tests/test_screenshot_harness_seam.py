"""The pinned screenshot harness is a callable seam, not just a CLI.

A repo that needs a local special-case (a vendor bundle to stub, a `dark` class the default
theme toggle doesn't set) used to fork the whole harness — which then drifts from the pinned
bytes and stops re-pinning on enrollment. These tests exercise the seam that replaces the fork:
the config-level `serveRoot`/`defaults`, the per-shot conveniences, and the `captureShots`/
`runConfig`/`main` exports a thin wrapper imports.

Every assertion runs the real `node` harness as a subprocess and inspects the PNG it produces,
so a regression that keeps the source strings intact still fails here. They skip cleanly where
Playwright/Chromium can't run, mirroring tests/test_screenshots_script.py.
"""

from __future__ import annotations

import json
import hashlib
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "screenshots.mjs"
EXTENSION_FIXTURE = Path(__file__).parent / "fixtures" / "screenshots-local-extension.mjs"

# Nonzero exits that mean "this environment can't run the harness" (Playwright or its managed
# Chromium absent) rather than "the harness is broken" — we skip on these, as the sibling suite does.
_ENV_UNAVAILABLE = (
    "Cannot load playwright",
    "Executable doesn't exist",
    "playwright install",
    "Host system is missing",
    "browserType.launch",
)


def _run_node(args, cwd, extra_env=None):
    import os

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["node", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def _skip_if_env_unavailable(result):
    output = result.stdout + result.stderr
    if result.returncode != 0 and any(m in output for m in _ENV_UNAVAILABLE):
        pytest.skip("Playwright/Chromium not available in this environment")
    return output


def _png_size(path: Path):
    """(width, height) from a PNG's IHDR — enough to compare a crop against the viewport."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available to run the harness"
)


def test_serve_root_with_strip_prefix_produces_png(tmp_path):
    """An http(s) shot whose assets live on disk under a rewritten prefix must render.

    This is the behavior the eight forked ciq configs depend on: serveRoot maps a request
    pathname to disk, stripping a prefix first, and an unmatched request aborts (so a missing
    asset can't silently produce a "passing" screenshot).
    """
    root = tmp_path / "dist"
    root.mkdir()
    (root / "app.css").write_text("body { background: #123456; }")
    (root / "index.html").write_text(
        "<!DOCTYPE html><html><head>"
        '<link rel="stylesheet" href="/static/app.css">'
        "</head><body><h1>served from disk</h1></body></html>"
    )
    config = {
        # stripPrefix removes /static before the on-disk lookup, so /static/app.css → dist/app.css.
        "serveRoot": [{"dir": str(root), "stripPrefix": "/static"}],
        "shots": [
            {
                "url": "http://localhost/static/index.html",
                "out": str(tmp_path / "served.png"),
                "assert": {"noConsoleErrors": True},
            }
        ],
    }
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))

    result = _run_node([str(SCRIPT), str(cfg)], cwd=tmp_path)
    output = _skip_if_env_unavailable(result)

    assert result.returncode == 0, f"serveRoot run failed:\n{output}"
    png = tmp_path / "served.png"
    assert png.exists() and png.stat().st_size > 0, f"no PNG written:\n{output}"


def test_serve_root_aborts_unmatched_request(tmp_path):
    """A referenced asset that isn't on disk must fail the run, not screenshot a broken page."""
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text(
        "<!DOCTYPE html><html><head>"
        '<script src="/missing.js"></script>'
        "</head><body>x</body></html>"
    )
    config = {
        "serveRoot": str(root),
        "shots": [
            {
                "url": "http://localhost/index.html",
                "out": str(tmp_path / "broken.png"),
                # A console error is emitted for the aborted subresource; the assert fails the run.
                "assert": {"noConsoleErrors": True},
            }
        ],
    }
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))

    result = _run_node([str(SCRIPT), str(cfg)], cwd=tmp_path)
    output = _skip_if_env_unavailable(result)

    assert result.returncode != 0, f"missing asset should fail the run, but it passed:\n{output}"
    assert "assertion" in output or "console" in output, output


def test_defaults_merge_and_storage_boot_state(tmp_path):
    """Per-shot defaults merge in, and a seeded localStorage value is observable at boot.

    The page reads a localStorage key at load and writes it to data-theme; a theme assert
    (also supplied via config-level defaults) confirms both the merge and the seed took.
    """
    page = tmp_path / "boot.html"
    page.write_text(
        "<!DOCTYPE html><html><head></head><body>"
        "<script>"
        "document.documentElement.dataset.theme = localStorage.getItem('mode') || 'unset';"
        "</script>"
        "seeded</body></html>"
    )
    config = {
        # defaults supply the assert for every shot; the shot supplies only its own storage seed.
        "defaults": {
            "assert": {"theme": {"selector": "html", "value": "dark"}},
        },
        "shots": [
            {
                "url": str(page),  # checkout-relative path — resolved to file:// by runConfig
                "storage": {"mode": "dark"},
                "out": str(tmp_path / "boot.png"),
            }
        ],
    }
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))

    result = _run_node([str(SCRIPT), str(cfg)], cwd=tmp_path)
    output = _skip_if_env_unavailable(result)

    assert result.returncode == 0, f"defaults+storage run failed (theme assert):\n{output}"
    assert (tmp_path / "boot.png").exists()


def test_selector_crop_is_smaller_than_viewport(tmp_path):
    """A per-shot `selector` crops to one element — its PNG is smaller than the full viewport."""
    page = tmp_path / "crop.html"
    page.write_text(
        "<!DOCTYPE html><html><body style='margin:0'>"
        "<div id='card' style='width:120px;height:80px;background:#4488cc'>card</div>"
        "</body></html>"
    )
    config = {
        "shots": [
            {"url": str(page), "out": str(tmp_path / "full.png")},
            {"url": str(page), "selector": "#card", "out": str(tmp_path / "crop.png")},
        ]
    }
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))

    result = _run_node([str(SCRIPT), str(cfg)], cwd=tmp_path)
    output = _skip_if_env_unavailable(result)

    assert result.returncode == 0, f"crop run failed:\n{output}"
    full_w, full_h = _png_size(tmp_path / "full.png")
    crop_w, crop_h = _png_size(tmp_path / "crop.png")
    assert crop_w < full_w and crop_h < full_h, (
        f"crop ({crop_w}x{crop_h}) is not smaller than the viewport ({full_w}x{full_h})"
    )


def test_js_clicks_and_focus_presses_change_captured_state(tmp_path):
    """Per-shot synthetic clicks and keyboard steps capture the states they reveal."""
    page = tmp_path / "interactions.html"
    page.write_text(
        "<!DOCTYPE html><html><body style='margin:0;background:white;height:100vh'>"
        "<button id='js' style='width:0;height:0;padding:0;border:0' "
        "onclick=\"document.body.style.background='lime'\"></button>"
        "<input id='keys' onkeydown=\"if(event.key==='ArrowRight') "
        "document.body.style.background='blue'\">"
        "</body></html>"
    )
    config = {
        "shots": [
            {"url": str(page), "out": str(tmp_path / "initial.png")},
            {
                "url": str(page),
                "jsClicks": ["#js"],
                "out": str(tmp_path / "js-click.png"),
            },
            {
                "url": str(page),
                "focus": "#keys",
                "presses": ["ArrowRight"],
                "out": str(tmp_path / "key-press.png"),
            },
        ]
    }
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))

    result = _run_node([str(SCRIPT), str(cfg)], cwd=tmp_path)
    output = _skip_if_env_unavailable(result)

    assert result.returncode == 0, f"interaction run failed:\n{output}"
    digests = {
        hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("initial.png", "js-click.png", "key-press.png")
    }
    assert len(digests) == 3, "the interaction steps did not produce distinct captured states"


def test_scroll_top_false_preserves_a_page_scroll_position(tmp_path):
    """The default shot resets to its red top, while `scrollTop: false` keeps its blue lower view."""
    page = tmp_path / "scroll.html"
    page.write_text(
        "<!DOCTYPE html><html><body style='margin:0'>"
        "<div style='height:900px;background:red'></div>"
        "<div style='height:900px;background:blue'></div>"
        "<script>window.scrollTo(0, 900)</script>"
        "</body></html>"
    )
    config = {
        "shots": [
            {"url": str(page), "out": str(tmp_path / "reset.png")},
            {
                "url": str(page),
                "scrollTop": False,
                "out": str(tmp_path / "preserved.png"),
            },
        ]
    }
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))

    result = _run_node([str(SCRIPT), str(cfg)], cwd=tmp_path)
    output = _skip_if_env_unavailable(result)

    assert result.returncode == 0, f"scroll run failed:\n{output}"
    assert hashlib.sha256((tmp_path / "reset.png").read_bytes()).hexdigest() != hashlib.sha256(
        (tmp_path / "preserved.png").read_bytes()
    ).hexdigest(), "scrollTop: false did not preserve a distinct lower-page capture"


def test_thin_extension_fixture_runs_end_to_end(tmp_path):
    """The ~30-line wrapper fixture reproduces the forked behavior without touching the pinned file.

    Its `serve` hook stubs a vendor bundle (which serveRoot would otherwise abort on) and its
    `applyTheme` toggles a `dark` class; the capture succeeds, proving the seam replaces the fork.
    """
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text(
        "<!DOCTYPE html><html><head>"
        '<script src="https://cdn.example.com/vendor.js"></script>'
        "</head><body><h1 class='x'>with vendor</h1></body></html>"
    )
    config = {
        "serveRoot": str(root),
        "shots": [
            {
                "url": "http://localhost/index.html",
                "theme": "dark",
                "out": str(tmp_path / "ext.png"),
                # The vendor global proves the serve hook ran; the dark class proves applyTheme did.
                "assert": {
                    "noConsoleErrors": True,
                    "theme": {"selector": "html", "value": "dark"},
                },
            }
        ],
    }
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))

    result = _run_node(
        [str(EXTENSION_FIXTURE), str(cfg)],
        cwd=tmp_path,
        extra_env={"AF_HARNESS": str(SCRIPT)},
    )
    output = _skip_if_env_unavailable(result)

    assert result.returncode == 0, f"thin extension run failed:\n{output}"
    assert (tmp_path / "ext.png").exists()
    # The whole point of the seam: the repo-local behavior ran without the pinned file moving.
    import tomllib

    manifest = tomllib.loads(
        (Path(__file__).parent.parent / "agentflow" / "capabilities.toml").read_text())
    spec = next(item for item in manifest["capabilities"] if item["id"] == "screenshot-harness")
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == spec["sha256"], (
        "the extension run required editing the pinned harness — the seam has regressed to "
        "the fork-or-brick choice #735 removed."
    )


def test_importing_the_module_does_not_run_the_cli():
    """`import()`-ing the harness must not trigger a usage error or capture — only the exports."""
    probe = (
        "import(process.env.AF_HARNESS_URL).then(m => {"
        "  const names = ['captureShots','runConfig','main'].filter(n => typeof m[n] === 'function');"
        "  if (names.length !== 3) { console.error('missing exports: ' + names.join(',')); process.exit(2); }"
        "  console.log('exports-ok');"
        "});"
    )
    import os

    env = dict(os.environ)
    env["AF_HARNESS_URL"] = SCRIPT.resolve().as_uri()
    result = subprocess.run(
        ["node", "-e", probe],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    output = result.stdout + result.stderr
    # A CLI that fired on import would print the "Usage:" banner or exit 1 with no exports line.
    assert result.returncode == 0, f"importing the module errored or ran the CLI:\n{output}"
    assert "exports-ok" in output, output
    assert "Usage:" not in output, f"CLI ran on import:\n{output}"
