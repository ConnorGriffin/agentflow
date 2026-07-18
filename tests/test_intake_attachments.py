"""Browserless ingestion of issue screenshot attachments for intake (issue #191)."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from agentflow import coordinated_intake, intake_attachments
from agentflow.intake import intake_prompt
from agentflow.intake_attachments import (ATTACHMENTS_DIRNAME, extract_image_urls,
                                          fetch_image_bytes, stage_attachments)
from agentflow.runner import ClaudeRunner

_PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
_ASSET = "https://github.com/user-attachments/assets/b45f39fc-2aab-4f8c-9618-556b64a6f401"
_IMG_TAG = f'<img width="1080" height="2340" alt="Image" src="{_ASSET}" />'
_S3 = "https://github-production-user-asset-6210df.s3.amazonaws.com/signed?token=xyz"


class _Resp:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    def getcode(self):
        return self.status

    def read(self):
        return self._body

    def close(self):
        pass


class _Opener:
    """Records every URL opened and the headers sent, replaying a scripted response list."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.opened: list[tuple[str, dict]] = []

    def open(self, target, timeout=None):
        if isinstance(target, urllib.request.Request):
            self.opened.append((target.full_url, dict(target.headers)))
        else:
            self.opened.append((target, {}))
        return self._responses.pop(0)


# --- URL detection ---------------------------------------------------------------------

def test_extracts_new_asset_url_from_an_img_tag():
    assert extract_image_urls(_IMG_TAG) == [_ASSET]


def test_extracts_legacy_and_markdown_image_links():
    body = ("![shot](https://user-images.githubusercontent.com/1/a.png)\n"
            "![diagram](https://example.com/pic.jpg)")
    urls = extract_image_urls(body)
    assert "https://user-images.githubusercontent.com/1/a.png" in urls
    assert "https://example.com/pic.jpg" in urls


def test_plain_text_has_no_attachments():
    assert extract_image_urls("just a description, no images") == []
    assert extract_image_urls("") == []


# --- browserless fetch (auth + follow the 302) -----------------------------------------

def test_fetch_follows_the_302_to_the_signed_asset_url(monkeypatch):
    monkeypatch.setattr(intake_attachments, "_gh_token", lambda: "tok")
    opener = _Opener([_Resp(302, {"Location": _S3}), _Resp(200, body=_PNG)])
    monkeypatch.setattr(intake_attachments, "_build_opener", lambda: opener)

    data = fetch_image_bytes(_ASSET)

    assert data == _PNG
    # authenticated request to the asset URL, then the signed hop with NO auth header
    assert opener.opened[0][0] == _ASSET
    assert opener.opened[0][1].get("Authorization") == "token tok"
    assert opener.opened[1][0] == _S3
    assert "Authorization" not in opener.opened[1][1]


def test_fetch_fails_closed_without_a_token(monkeypatch):
    monkeypatch.setattr(intake_attachments, "_gh_token", lambda: None)
    called = []
    monkeypatch.setattr(intake_attachments, "_build_opener",
                        lambda: called.append(1) or _Opener([]))

    assert fetch_image_bytes(_ASSET) is None
    assert called == []  # never even opened a connection


def test_fetch_never_sends_the_token_to_a_non_github_host(monkeypatch):
    """A crafted `![x](https://attacker.com/x.png)` must not leak `gh auth token`."""
    monkeypatch.setattr(intake_attachments, "_gh_token",
                        lambda: (_ for _ in ()).throw(AssertionError("token requested")))
    opener = _Opener([_Resp(200, body=_PNG)])
    monkeypatch.setattr(intake_attachments, "_build_opener", lambda: opener)

    data = fetch_image_bytes("https://attacker.com/x.png")

    assert data == _PNG  # the off-GitHub image still downloads, just unauthenticated
    assert "Authorization" not in opener.opened[0][1]


def test_legacy_user_images_host_is_authenticated(monkeypatch):
    monkeypatch.setattr(intake_attachments, "_gh_token", lambda: "tok")
    legacy = "https://user-images.githubusercontent.com/1/a.png"
    opener = _Opener([_Resp(200, body=_PNG)])
    monkeypatch.setattr(intake_attachments, "_build_opener", lambda: opener)

    assert fetch_image_bytes(legacy) == _PNG
    assert opener.opened[0][1].get("Authorization") == "token tok"


def test_github_com_non_attachment_path_is_not_authenticated(monkeypatch):
    """Only `/user-attachments/` on github.com gets the token, not arbitrary repo URLs."""
    monkeypatch.setattr(intake_attachments, "_gh_token",
                        lambda: (_ for _ in ()).throw(AssertionError("token requested")))
    opener = _Opener([_Resp(200, body=_PNG)])
    monkeypatch.setattr(intake_attachments, "_build_opener", lambda: opener)

    data = fetch_image_bytes("https://github.com/owner/repo/raw/main/x.png")

    assert data == _PNG
    assert "Authorization" not in opener.opened[0][1]


# --- staging into the worktree ---------------------------------------------------------

def test_body_with_an_attachment_triggers_a_fetch_and_writes_the_image(monkeypatch, tmp_path):
    fetched = []
    monkeypatch.setattr(intake_attachments, "fetch_image_bytes",
                        lambda url, **_: fetched.append(url) or _PNG)

    written = stage_attachments(_IMG_TAG, tmp_path / ATTACHMENTS_DIRNAME)

    assert fetched == [_ASSET]
    assert len(written) == 1
    assert written[0].read_bytes() == _PNG
    assert written[0].suffix == ".png"


def test_body_without_attachments_never_fetches(monkeypatch, tmp_path):
    monkeypatch.setattr(intake_attachments, "fetch_image_bytes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))

    assert stage_attachments("plain text only", tmp_path / ATTACHMENTS_DIRNAME) == []


# --- prompt handoff (the screenshot-only regression) -----------------------------------

def test_screenshot_only_issue_points_the_model_at_the_downloaded_image():
    prompt = intake_prompt("o/r", {"number": 191, "title": "", "body": _IMG_TAG})
    assert ATTACHMENTS_DIRNAME in prompt
    # the guard against holding a picture as "unreadable" solely for lacking prose
    assert "Read" in prompt and "screenshot" in prompt.lower()


def test_text_only_issue_gets_no_image_handoff():
    prompt = intake_prompt("o/r", {"number": 1, "title": "t", "body": "just words"})
    assert ATTACHMENTS_DIRNAME not in prompt


# --- end-to-end wiring through reset_worktree ------------------------------------------

def test_reset_worktree_stages_screenshot_into_the_read_only_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(intake_attachments, "fetch_image_bytes", lambda url, **_: _PNG)
    monkeypatch.setattr(ClaudeRunner, "prepare_worktree_detached",
                        lambda self, workdir, ref, wt: Path(wt).mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(ClaudeRunner, "provision", lambda self, wt: None)

    source = str(tmp_path / ".agentflow" / "worktrees" / "claude-intake" / "issue-191")
    record = SimpleNamespace(
        pool="claude", subject="191", source=source,
        input_ptr=json.dumps({"snapshot": {"body": _IMG_TAG}, "source_ref": "abc123"}))

    assert coordinated_intake.reset_worktree(record) is True
    staged = list((Path(source) / ATTACHMENTS_DIRNAME).glob("attachment-*"))
    assert len(staged) == 1 and staged[0].read_bytes() == _PNG
