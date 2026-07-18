"""Browserless ingestion of an issue's image attachments for intake (issue #191).

A screenshot-only issue reaches the model as an `<img>`/markdown link — text the model
can't see through. This module detects image attachments in an issue body, fetches their
bytes *without a browser* (authenticate with `gh auth token`, follow GitHub's single 302
to the short-lived signed asset URL, download the bytes), and stages them into the intake
session's read-only worktree so the vision-capable model can `Read` the pixels and route
on the screenshot's actual content.

Fail closed everywhere (mirroring `_issue_body`'s `None` discipline, ADR 0016): any auth,
network, or write failure yields no staged file and intake falls back to today's text-only
routing — it never crashes intake and never touches the original body.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from agentflow.runner import _run

# The worktree directory intake stages fetched screenshots into. The intake prompt points
# the model here; `stage_attachments` writes here. Untracked scratch — the worktree is a
# throwaway read-only checkout that gets force-removed each cycle, so these never commit.
ATTACHMENTS_DIRNAME = ".agentflow-intake-attachments"

# New GitHub CDN form: https://github.com/user-attachments/assets/<uuid>
_ASSET_RE = re.compile(r"https://github\.com/user-attachments/assets/[0-9a-fA-F-]+")
# Legacy form: https://user-images.githubusercontent.com/...
_LEGACY_RE = re.compile(r"https://user-images\.githubusercontent\.com/[^\s\"')>]+")
# Markdown image links: ![alt](url)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
_REDIRECTS = (301, 302, 303, 307, 308)

def _should_authenticate(url: str) -> bool:
    """True only for `github.com/user-attachments/...` and the legacy user-images CDN.

    `extract_image_urls` also matches markdown `![](url)` links on arbitrary hosts, so a
    crafted body like `![x](https://attacker.com/x.png)` would otherwise leak the daemon's
    `gh auth token` to an attacker-controlled server. The token authorizes GitHub asset
    access; sending it anywhere else exfiltrates it. The signed-asset hop (S3) is fetched
    with no auth header regardless."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    if host == "github.com":
        return parts.path.startswith("/user-attachments/")
    return host == "user-images.githubusercontent.com"


def extract_image_urls(body: str) -> list[str]:
    """Every image-attachment URL in an issue body, in order, de-duplicated. Pure.

    Covers the new `user-attachments/assets/<uuid>` CDN links (as bare URLs or inside an
    `<img src=...>` / markdown link), the legacy `user-images.githubusercontent.com` host,
    and markdown `![](url)` links whose target looks like an image."""
    body = body or ""
    urls: list[str] = []
    urls += _ASSET_RE.findall(body)
    urls += _LEGACY_RE.findall(body)
    for url in _MD_IMAGE_RE.findall(body):
        base = url.split("?", 1)[0].lower()
        if (base.endswith(_IMAGE_EXTS) or "user-attachments/assets/" in url
                or "user-images.githubusercontent.com" in url):
            urls.append(url)
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _gh_token() -> str | None:
    r = _run(["gh", "auth", "token"])
    if r.returncode != 0:
        return None
    token = (r.stdout or "").strip()
    return token or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the redirect response instead of following it, so the caller can read the
    signed `Location` and re-request it *without* the GitHub auth header (S3 rejects it)."""

    def http_error_301(self, req, fp, code, msg, headers):
        return fp

    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


def fetch_image_bytes(url: str, *, timeout: float = 30.0) -> bytes | None:
    """Fetch one issue image attachment browserlessly, or ``None`` on any failure.

    Authenticates the asset URL with `gh auth token` — but only for GitHub attachment
    hosts (`_should_authenticate`); any other host is fetched with no credential so a
    crafted off-GitHub image link can't exfiltrate the token. Then follows GitHub's single
    302 to the short-lived (~300s) signed asset URL and downloads the bytes; the signed hop
    is fetched *without* the auth header. No browser, no OCR — just auth + redirect + bytes.
    Fetch immediately; the signed URL is not cached."""
    headers = {"Accept": "application/octet-stream"}
    if _should_authenticate(url):
        token = _gh_token()
        if token is None:
            return None
        headers["Authorization"] = f"token {token}"
    opener = _build_opener()
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = opener.open(req, timeout=timeout)
        try:
            status = getattr(resp, "status", None) or resp.getcode()
            location = resp.headers.get("Location") if status in _REDIRECTS else None
            data = resp.read()
        finally:
            resp.close()
        if location:
            follow = opener.open(location, timeout=timeout)  # signed URL — no auth header
            try:
                data = follow.read()
            finally:
                follow.close()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data or None


def _extension_for(data: bytes) -> str:
    """A file extension inferred from the image's magic bytes (default `.png`). The model
    reads by content, but a real extension keeps the `Read` tool's image detection happy."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def stage_attachments(body: str, dest_dir: Path | str) -> list[Path]:
    """Fetch every image attachment in ``body`` into ``dest_dir`` for the model to Read.

    Returns the paths written (possibly fewer than the URLs found — a failed fetch is
    skipped, not fatal). A body with no attachments does nothing and returns ``[]``. Never
    raises: any error falls back to text-only intake."""
    urls = extract_image_urls(body)
    if not urls:
        return []
    dest = Path(dest_dir)
    written: list[Path] = []
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    for index, url in enumerate(urls, start=1):
        data = fetch_image_bytes(url)
        if not data:
            continue
        path = dest / f"attachment-{index}{_extension_for(data)}"
        try:
            path.write_bytes(data)
        except OSError:
            continue
        written.append(path)
    return written
