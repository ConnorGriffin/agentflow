"""The cheap cross-fleet change-probe (issue #80) — the fast clock's O(1) 'did anything
move?' check, so a full dispatch pass runs only when there is actually new work to react to.

A full pass is dozens of serial `gh` calls across the fleet; running it every ~15s would
burn the GraphQL budget for nothing when the fleet is quiet. Instead the daemon asks the
probe on every fast tick — ONE cross-repo search — and only pays for a full pass when the
probe reports change. The heartbeat full pass is the backstop for what the probe can miss
(GitHub's search index lags a labelling/close by seconds-to-minutes), so the probe never
has to be perfect — only cheap.

The probe watches a single fact: the most recent update time across every open OR recently
closed issue/PR in the fleet. Anything the full pass reacts to — a new issue, a freshly
labelled `ready-for-agent`, a just-closed blocker, a maintainer's comment on a parked PR —
bumps that issue/PR's update time, so a watermark over update times catches them all in one
query. When the newest update is past the last one the probe saw, something changed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agentflow.runner import _run


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gh_search(repos: list[str], since: str) -> list[dict] | None:
    """One cross-repo search for every issue/PR (open or closed) updated after `since`. This is
    the whole per-tick API cost: a single REST search call for the entire fleet, in the search
    endpoint's own rate bucket (separate from the GraphQL budget the full pass spends). Returns
    the matched items, or None when the search itself failed — unknown is not 'no change'."""
    cmd = ["gh", "search", "issues", "--include-prs", "--limit", "100",
           "--json", "number,updatedAt"]
    for repo in repos:
        cmd += ["--repo", repo]
    cmd += ["--updated", f">{since}"]
    r = _run(cmd)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None


class ChangeProbe:
    """The fast clock's change detector. `changed()` costs one search call for the whole fleet
    and answers 'has any actionable state moved since I last looked?'. It advances its own
    watermark as it goes, so a change is reported once and the daemon converges back to quiet.

    Deliberately best-effort: a search-index lag can hide a just-made change for a tick or two,
    which is exactly what the daemon's heartbeat full pass exists to catch. `search` is injected
    so the decision is exercised without touching GitHub."""

    def __init__(self, repos, *, search=_gh_search, now=_now_iso) -> None:
        self._repos = [c.repo if hasattr(c, "repo") else c for c in repos]
        self._search = search
        # Only updates AFTER construction count as change; the daemon's startup full pass
        # already handles whatever was pending when it came up.
        self._watermark = now()

    def changed(self) -> bool:
        """True when something in the fleet was updated since the last look. One search call.
        An empty fleet (no repos) or a failed search reports no change — the heartbeat backstops
        both, and treating a `gh` blip as 'change' would run a full pass every tick during an
        outage, the opposite of the point."""
        if not self._repos:
            return False
        items = self._search(self._repos, self._watermark)
        if not items:
            return False
        newest = max((i.get("updatedAt", "") for i in items), default="")
        if newest > self._watermark:
            self._watermark = newest
            return True
        return False
