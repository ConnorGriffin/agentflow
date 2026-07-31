"""The manifest's bundled digests must match the files they describe.

Editing a bundled skill (or the screenshot harness) without re-recording its
digest here makes every enrolled repository report drifted, and the resulting
failure is far from `agentflow/enroll.py`'s readiness roll-up rather than at
the point of the mistake. This test recomputes each bundled digest from the
repository's own source tree, so the failure names the file and the fix.
"""

import hashlib
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
MANIFEST = REPO_ROOT / "agentflow" / "capabilities.toml"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundled_capabilities() -> list[dict]:
    manifest = tomllib.loads(MANIFEST.read_text())
    return [item for item in manifest["capabilities"] if item.get("source") == "bundled"]


def test_bundled_skill_digests_match_source_tree():
    for capability in _bundled_capabilities():
        skill = capability.get("skill")
        if skill is None or "files" not in capability:
            continue
        skill_dir = REPO_ROOT / "skills" / skill
        for entry in capability["files"]:
            path = skill_dir / entry["path"]
            actual = _sha256(path)
            assert actual == entry["sha256"], (
                f"{path.relative_to(REPO_ROOT)} does not match the digest recorded "
                f"for capability {capability['id']!r} in {MANIFEST.relative_to(REPO_ROOT)}. "
                f"Re-record it: sha256sum {path.relative_to(REPO_ROOT)}"
            )


def test_bundled_screenshot_harness_digest_matches_source_tree():
    for capability in _bundled_capabilities():
        if "sha256" not in capability:
            continue
        path = REPO_ROOT / "scripts" / "screenshots.mjs"
        actual = _sha256(path)
        assert actual == capability["sha256"], (
            f"{path.relative_to(REPO_ROOT)} does not match the digest recorded "
            f"for capability {capability['id']!r} in {MANIFEST.relative_to(REPO_ROOT)}. "
            f"Re-record it: sha256sum {path.relative_to(REPO_ROOT)}"
        )
