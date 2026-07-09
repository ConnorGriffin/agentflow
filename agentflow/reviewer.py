"""Cross-tool review → a machine-readable verdict (ADR 0003, 0004, 0014).

A *different* tool than the builder inspects a PR against correctness/security and
the engineering charter, and writes a strict JSON verdict the auto-merge gate can
act on deterministically. A green build with a confident-but-wrong diff is the
failure this exists to catch — so parsing is **fail-safe**: anything we cannot read
as a clean PASS is treated as not-clean, and never auto-merges.

Deep module: `Reviewer(other_tool).review(...) -> Verdict`. Hidden behind it: a
detached worktree on the PR head, the review prompt (the charter rubric is inherited
from the repo's instruction file), the tool launch at the floored review tier, and
reading + validating the verdict file. `parse_verdict` is pure — the test surface.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentflow.runner import Tier, _WorktreeRunner

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str   # "blocking" | "nit"
    summary: str
    file: str = ""
    line: int = 0


@dataclass(frozen=True, slots=True)
class Verdict:
    clean: bool
    findings: tuple[Finding, ...] = ()
    parsed: bool = True
    detail: str = ""

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]


def _unparseable(detail: str) -> Verdict:
    # Fail safe: a review we cannot read as a clean PASS must never auto-merge.
    return Verdict(clean=False,
                   findings=(Finding("blocking", f"no usable review verdict: {detail}"),),
                   parsed=False, detail=detail)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text


def parse_verdict(payload: str) -> Verdict:
    """Parse a reviewer's JSON verdict. Pure and defensive (the test surface)."""
    text = _strip_fences(payload).strip()
    if not text:
        return _unparseable("empty verdict")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return _unparseable(f"invalid JSON ({e})")
    if not isinstance(data, dict) or "verdict" not in data:
        return _unparseable("missing 'verdict' field")
    findings = []
    for f in (data.get("findings") or []):
        if not isinstance(f, dict):
            continue
        sev = "blocking" if str(f.get("severity", "")).lower() == "blocking" else "nit"
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        findings.append(Finding(sev, str(f.get("summary", "")), str(f.get("file", "")), line))
    said_pass = str(data.get("verdict", "")).upper() == "PASS"
    has_blocking = any(f.severity == "blocking" for f in findings)
    # Defensive: an agent that says PASS but lists a blocking finding is still BLOCK.
    return Verdict(clean=said_pass and not has_blocking, findings=tuple(findings), parsed=True)


def review_tier(issue_tier: Tier) -> Tier:
    """ADR 0014(b): review tracks the issue tier but never drops below `standard`."""
    return Tier.DEEP if issue_tier is Tier.DEEP else Tier.STANDARD


REVIEW_PROMPT = """You are the independent cross-tool reviewer for PR #{pr} in this repo.
A different agent built it. Decide whether it is safe to merge unattended.

Judge the diff against, in order:
- correctness and security — any real bug or vulnerability is BLOCKING;
- the engineering charter in your instructions — a shallow module, an unmocked UI
  surface, or an interface you cannot test through is BLOCKING.
Everything else (style, naming, minor perf) is a nit, not blocking.

Inspect it: run `gh pr diff {pr}` and read the surrounding code in this worktree.

Then WRITE your verdict as STRICT JSON to `{path}` (create parent dirs). Schema:
{{"verdict": "PASS" | "BLOCK",
  "findings": [{{"severity": "blocking" | "nit", "file": "path", "line": 0, "summary": "..."}}]}}
"verdict" is PASS only if there are zero blocking findings. Writing that file is the task."""


class Reviewer:
    """Runs the OTHER tool as an independent reviewer and returns its verdict."""

    def __init__(self, runner: _WorktreeRunner):
        self.runner = runner  # the tool that did NOT build this PR

    def review(self, repo: str, workdir: str, pr_number: int, pr_head_branch: str,
               slug: str, issue_tier: Tier) -> Verdict:
        wt = Path(workdir) / ".agentflow" / "worktrees" / f"{self.runner.tool}-review" / f"pr-{pr_number}-{slug}"
        try:
            self.runner.prepare_worktree_detached(workdir, f"origin/{pr_head_branch}", wt)
            self.runner.provision(wt)
        except subprocess.CalledProcessError as e:
            return _unparseable(f"review worktree/provision failed: {e}")
        verdict_path = wt / ".agentflow" / "review-verdict.json"
        prompt = REVIEW_PROMPT.format(pr=pr_number, path=verdict_path)
        model = self.runner.model_for(review_tier(issue_tier))
        self.runner.launch(prompt, cwd=str(wt), model=model)
        if not verdict_path.exists():
            return _unparseable("reviewer wrote no verdict file")
        try:
            return parse_verdict(verdict_path.read_text())
        except OSError as e:
            return _unparseable(f"could not read verdict file ({e})")
