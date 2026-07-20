"""Assemble the pinned Intake replay corpus from real issues (Wayfinder #2, #228).

Reproducible + auditable: it reads every labeled issue's applied route/complexity/effort (the
real outcome that becomes the scoring reference), stratifies a fixed sample, and writes it
pinned to one repository SHA so a rerun is byte-stable. Run:

    uv run python -m agentflow.experiments.build_corpus <repo> <pinned-sha> [out.json]

It shells out to ``gh`` read-only; it never writes to GitHub. The sample and every truncation
are recorded in the corpus ``notes`` — silent truncation would read as full coverage (#228).
"""

from __future__ import annotations

import json
import sys

from agentflow.runner import _run

_ROUTE_LABEL = {"ready-for-agent": "ready", "agentflow:needs-grilling": "grill",
                "agentflow:needs-mockup": "mockup"}

# Per-stratum caps. Deep-ready is over-represented in the population (57 vs 20 standard), so we
# cap it to keep both complexity strata usable for a within-cell comparison (#227). All holds
# are taken — they are scarce and the route mix must include them.
_MAX_READY_DEEP = 20
_MAX_READY_STANDARD = 20


def _dial(names: set[str], prefix: str) -> str:
    for n in names:
        if n.startswith(prefix):
            return n[len(prefix):]
    return ""


def build(repo: str, pinned_sha: str) -> dict:
    result = _run(
        ["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "400",
         "--json", "number,title,body,labels"], timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue list failed: {result.stderr.strip()}")
    issues = json.loads(result.stdout)
    entries: list[dict] = []
    ready_deep = ready_standard = 0
    truncated = {"ready-deep": 0, "ready-standard": 0}
    for it in sorted(issues, key=lambda x: x["number"]):
        names = {lbl["name"] for lbl in it["labels"]}
        if any(n.startswith("wayfinder:") for n in names):
            continue
        route = next((r for lbl, r in _ROUTE_LABEL.items() if lbl in names), "")
        if not route:
            continue
        complexity = _dial(names, "agentflow:complexity:")
        effort = _dial(names, "agentflow:effort:")
        if route == "ready" and complexity == "deep":
            if ready_deep >= _MAX_READY_DEEP:
                truncated["ready-deep"] += 1
                continue
            ready_deep += 1
        elif route == "ready" and complexity == "standard":
            if ready_standard >= _MAX_READY_STANDARD:
                truncated["ready-standard"] += 1
                continue
            ready_standard += 1
        strata = tuple(t for t in (f"route:{route}",
                                   f"complexity:{complexity}" if complexity else "",
                                   f"effort:{effort}" if effort else "") if t)
        entries.append({
            "repo": repo, "repo_ref": pinned_sha, "number": it["number"],
            "title": it.get("title", ""), "body": it.get("body") or "",
            "labels": sorted(names),
            "reference_route": route, "reference_complexity": complexity,
            "reference_effort": effort, "strata": list(strata)})
    notes = [
        f"Assembled from {repo} issues pinned at {pinned_sha}.",
        f"{len(entries)} issues selected: {ready_deep} ready/deep, {ready_standard} "
        f"ready/standard, plus all holds (grill/mockup).",
        f"Explicit truncation to keep strata balanced: dropped {truncated['ready-deep']} "
        f"extra ready/deep and {truncated['ready-standard']} extra ready/standard issues "
        f"beyond the {_MAX_READY_DEEP}/{_MAX_READY_STANDARD} caps (not full coverage of the "
        "population).",
        "Reference route/complexity/effort are the labels actually applied to each issue by "
        "the historical all-deep Intake — the scoring ground truth.",
    ]
    return {"notes": notes, "entries": entries}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    repo, pinned_sha = argv[1], argv[2]
    out = argv[3] if len(argv) > 3 else "-"
    corpus = build(repo, pinned_sha)
    text = json.dumps(corpus, indent=2, sort_keys=False)
    if out == "-":
        print(text)
    else:
        with open(out, "w") as fh:
            fh.write(text + "\n")
        print(f"wrote {len(corpus['entries'])} entries to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
