"""Notifications for the needs-you set (ADR 0010) — ntfy, reaching the phone AFK.

Reuses the self-hosted ntfy path from triage-sweep. Best-effort and non-fatal: a
failed POST (box down, no network) never aborts the pipeline. Autonomous *merges*
are silent (audit after, ADR 0004); this fires only for what needs a human — parks
and build bails. Override the topic with AGENTFLOW_NTFY_URL.
"""

from __future__ import annotations

import os
import subprocess

NTFY_URL = os.environ.get("AGENTFLOW_NTFY_URL", "https://ntfy.home.connorcg.com/agentflow")


def _build_args(title: str, message: str, url: str, ntfy_url: str,
                sequence_id: str = "") -> list[str]:
    args = ["curl", "-fsS", "-m", "10", "-H", f"Title: {title}", "-H", "Tags: robot"]
    if url:
        args += ["-H", f"Click: {url}"]
    if sequence_id:
        args += ["-H", f"X-Sequence-ID: {sequence_id}"]
    args += ["-d", message or " ", ntfy_url]
    return args


def notify(title: str, message: str, url: str = "", sequence_id: str = "") -> bool:
    """Best-effort ntfy push. A stable sequence ID makes a retry replace the same client
    notification. Never raises; reports whether ntfy accepted the request."""
    try:
        completed = subprocess.run(_build_args(title, message, url, NTFY_URL, sequence_id),
                                   capture_output=True, timeout=15)
        return completed.returncode == 0
    except Exception:  # noqa: BLE001 — notifications must never break a run
        return False
