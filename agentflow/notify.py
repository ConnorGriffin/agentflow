"""Optional notifications for the needs-you set (ADR 0010).

Set ``AGENTFLOW_NTFY_URL`` to an ntfy topic URL to enable delivery. Notifications
are disabled by default. A failed POST never aborts the pipeline. Autonomous
*merges* are silent (audit after, ADR 0004); this fires only for what needs a
human — parks and build bails.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

NTFY_URL = os.environ.get("AGENTFLOW_NTFY_URL", "")

# How long one handoff's ping counts as already sent. A handoff pings on every cycle that can
# prove its comment, so the ping is never lost when a crash falls between posting that comment and
# sending it (ADR 0042). The cost is that a stage whose post-handoff bookkeeping fails
# *persistently* — an archived repo, a locked issue, a token that lost write scope — re-proves and
# re-pings every heartbeat. This window turns that into a daily reminder rather than one every few
# minutes: the operator still hears about a stuck hold, repeatedly, without being buried by it.
_REPEAT_WINDOW_SECONDS = 24 * 3600

# The keys this module records are hex digests (:mod:`agentflow.handoff` derives them with
# sha256), so the recorded name is bounded to that alphabet rather than trusted to be safe. A key
# of any other shape simply is not recorded: it costs a repeated ping, and it means no caller can
# ever steer this at a path of its choosing.
_SEQUENCE_ID = re.compile(r"^[0-9a-f]{1,64}$")


def _sent_marker(sequence_id: str) -> Path | None:
    if not _SEQUENCE_ID.match(sequence_id):
        return None   # not a key this module minted — never let it name a file
    root = Path(os.environ.get("AGENTFLOW_STATE", os.path.expanduser("~/.agentflow")))
    return root / "notifications" / f"{sequence_id}.sent"


def _sent_within_window(sequence_id: str, now: float) -> bool:
    """Whether this exact handoff was pinged inside the repeat window. Absent or unreadable state
    reads as *not sent*: an unknown delivery history must never be what silences a handoff."""
    marker = _sent_marker(sequence_id)
    if marker is None:
        return False
    try:
        return now - float(marker.read_text().strip()) < _REPEAT_WINDOW_SECONDS
    except (OSError, ValueError):
        return False


def _record_sent(sequence_id: str, now: float) -> None:
    marker = _sent_marker(sequence_id)
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(now))
    except OSError:
        pass   # an unwritable state directory costs a repeated ping, never a lost one


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
    """Best-effort ntfy push. Never raises; reports whether ntfy accepted the request.

    ``sequence_id`` is a stable per-handoff key. It is sent as a header so a repeat is
    recognizable in the request log, and it is what this module records locally to bound repeats:
    the same key is pushed at most once per :data:`_REPEAT_WINDOW_SECONDS`, so a handoff whose
    stage cannot finish its bookkeeping nags daily instead of every heartbeat. The header itself
    deduplicates nothing at the client — ntfy has no header for that; its own sequence ids are a
    URL path segment that only updates a *scheduled* message before delivery, never one that has
    already arrived. Nothing may lean on a repeat being collapsed for the reader (ADR 0042)."""
    if not NTFY_URL:
        return False
    now = time.time()
    if sequence_id and _sent_within_window(sequence_id, now):
        return True   # already delivered inside the window; the handoff stands
    try:
        completed = subprocess.run(_build_args(title, message, url, NTFY_URL, sequence_id),
                                   capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001 — notifications must never break a run
        return False
    if completed.returncode == 0 and sequence_id:
        _record_sent(sequence_id, now)   # only a delivered ping starts the window
    return completed.returncode == 0
