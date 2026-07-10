"""Cross-tool review → a machine-readable verdict (ADR 0003, 0004, 0014).

A *different* tool than the builder inspects a PR against correctness/security and
the engineering charter, and writes a strict JSON verdict the auto-merge gate can
act on deterministically. A green build with a confident-but-wrong diff is the
failure this exists to catch — so parsing is **fail-safe**: anything we cannot read
as a clean PASS is treated as not-clean, and never auto-merges.

Hardened after an adversarial pass (see git history) that found several routes to a
false `clean=True`. The invariants now enforced:

- The verdict is written to a fresh temp file **outside** the PR checkout, so a
  builder cannot commit a forged `review-verdict.json` into its own PR tree.
- The reviewer session must actually run (`launch()` must return True) — a
  rate-limited/crashed reviewer yields not-clean, never a stale/leftover PASS.
- The verdict must carry the PR head SHA we're about to merge (proof it reviewed
  *this* diff), or it's not-clean.
- Severity is fail-safe: any finding whose severity is not an explicit nit counts
  as blocking (so "critical"/"BLOCKER"/"" don't leak through as nits).
- Malformed containers, duplicate keys, and any parse exception → not-clean.

Deep module: `Reviewer(other_tool).review(...) -> Verdict`. `parse_verdict` is pure
— the test surface — and every adversarial case above is a regression test.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentflow.runner import Complexity, _WorktreeRunner, _run

# Severities we accept as non-blocking. ANYTHING else (incl. "", "critical",
# "blocker", "high", unknown) is treated as blocking — fail safe.
_NIT_SEVERITIES = {"nit", "nits", "info", "minor", "low", "style", "note",
                   "suggestion", "trivial", "cosmetic"}
_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)\n```", re.DOTALL)


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
    reviewer_tool: str = ""

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]


def _unparseable(detail: str) -> Verdict:
    # Fail safe: a review we cannot read as a clean PASS must never auto-merge.
    return Verdict(clean=False,
                   findings=(Finding("blocking", f"no usable review verdict: {detail}"),),
                   parsed=False, detail=detail)


def _severity(raw: object) -> str:
    return "nit" if str(raw).strip().lower() in _NIT_SEVERITIES else "blocking"


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: dict = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate key {k!r}")
        seen[k] = v
    return seen


def _verdict_dicts(text: str):
    """Yield candidate verdict dicts best-first: fenced ```json blocks (last first),
    the whole message, then every balanced {...} object scanning from the end. EVERY
    parse uses the duplicate-key hook, so `{"verdict":"BLOCK","verdict":"PASS"}` still
    fails. Robust to a verdict that trails the reviewer's reasoning prose — 'STRICT
    JSON only' is not reliable, so we recover the object rather than drop the review."""
    text = text.strip()

    def _load(s: str):
        try:
            obj = json.loads(s, object_pairs_hook=_no_duplicate_keys)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    for b in reversed(_FENCE_RE.findall(text)):
        d = _load(b.strip())
        if d is not None:
            yield d
    d = _load(text)
    if d is not None:
        yield d
    dec = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys)
    for i in reversed([j for j, c in enumerate(text) if c == "{"]):
        try:
            obj, _end = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def parse_verdict(payload: str, expected_sha: str | None = None) -> Verdict:
    """Parse a reviewer's JSON verdict. Pure, defensive, fail-safe (test surface).

    `clean` requires: a JSON dict carrying `verdict`, verdict == PASS, no blocking
    finding, and — when `expected_sha` is given — a matching `reviewed_sha` (proof it
    reviewed the head we're about to merge). Recovers the verdict even when the reviewer
    prefixes it with reasoning prose; any deviation returns not-clean.
    """
    try:
        data = next((d for d in _verdict_dicts(payload) if "verdict" in d), None)
        if data is None:
            return _unparseable("no usable verdict object")

        raw_findings = data.get("findings", [])
        if not isinstance(raw_findings, list):
            return _unparseable("'findings' is not a list")
        findings = []
        for f in raw_findings:
            if not isinstance(f, dict):
                return _unparseable("a finding is not an object")
            line = f.get("line") or 0
            findings.append(Finding(_severity(f.get("severity")), str(f.get("summary", "")),
                                    str(f.get("file", "")), int(line) if str(line).isdigit() else 0))

        if expected_sha is not None and str(data.get("reviewed_sha", "")) != expected_sha:
            return _unparseable("reviewed_sha missing or does not match the PR head")

        said_pass = str(data.get("verdict", "")).upper() == "PASS"
        has_blocking = any(f.severity == "blocking" for f in findings)
        return Verdict(clean=said_pass and not has_blocking, findings=tuple(findings), parsed=True)
    except Exception as e:  # noqa: BLE001 — the whole point is to never propagate
        return _unparseable(f"parse error: {type(e).__name__}")


REVIEW_PROMPT = """You are the independent cross-tool reviewer for PR #{pr} in this repo.
A different agent built it to satisfy a specific issue. Decide if it is safe to merge.

The issue's acceptance criteria — judge against THIS, not your own wishlist:
---
{acceptance}
---

First, prove you looked: run `gh pr view {pr} --json headRefOid,files,body` and
`gh pr diff {pr}`. If any changed file is under a user-facing surface (e.g. `frontend/`),
also run `gh pr view {pr} --json comments` to check for attached screenshots. Judge the
PR as a merge-ready artifact — its body and evidence, not only its diff.

BLOCKING (only these):
- a real bug or security hole that breaks a stated acceptance criterion, or
- a violation of the engineering charter in your instructions:
  - a shallow module, an unmocked UI surface, or an interface you cannot test through;
  - a **PR body not framed for the human who merges** — it leans on file / function /
    test names or CSS / API specifics instead of plain app behavior (ADR 0018);
  - a **user-facing change with no screenshot** — any PR whose files touch a
    user-facing surface (e.g. `frontend/`) must attach before/after screenshots, in
    the body or a comment; none is a blocking gap (ADR 0018). Backend-only PRs need none.
A correctness gap BEYOND the stated acceptance — an unhandled case the issue did not
ask for — is a NIT, not blocking; note it so it can be filed as a follow-up.
Style, naming, and minor perf are nits.

END your message with the verdict as ONE JSON object. Your reasoning may come first, but
the JSON must be the LAST thing in your message and must parse:
{{"verdict": "PASS" | "BLOCK",
  "reviewed_sha": "<the headRefOid you fetched above>",
  "findings": [{{"severity": "blocking" | "nit", "file": "path", "line": 0, "summary": "..."}}]}}
"verdict" is PASS only if there are zero blocking findings."""


class Reviewer:
    """Runs the OTHER tool as an independent reviewer and returns its verdict."""

    def __init__(self, runner: _WorktreeRunner):
        self.runner = runner  # the tool that did NOT build this PR

    def review(self, repo: str, workdir: str, pr_number: int, pr_head_branch: str,
               slug: str, issue_complexity: Complexity, acceptance: str = "") -> Verdict:
        head_sha = self._head_sha(repo, pr_number)
        if not head_sha:
            return _unparseable("could not read PR head SHA")

        wt = Path(workdir) / ".agentflow" / "worktrees" / f"{self.runner.tool}-review" / f"pr-{pr_number}-{slug}"
        try:
            self.runner.prepare_worktree_detached(workdir, f"origin/{pr_head_branch}", wt)
            self.runner.provision(wt)
        except subprocess.CalledProcessError as e:
            return _unparseable(f"review worktree/provision failed: {e}")

        # The verdict is the reviewer's captured final message — read by US, not a
        # model-written file in the (untrusted) PR tree, so a builder cannot forge it.
        prompt = REVIEW_PROMPT.format(pr=pr_number, acceptance=acceptance or "(none provided)")
        # Review at the issue's own complexity — a correctness-sensitive build gets a
        # correctness-sensitive reviewer (ADR 0018; the old light floor is moot).
        model = self.runner.model_for(issue_complexity)
        ok, message = self.runner.launch(prompt, cwd=str(wt), model=model)
        if not ok:
            return _unparseable("reviewer session errored (launch non-zero)")
        v = parse_verdict(message, expected_sha=head_sha)
        return Verdict(v.clean, v.findings, v.parsed, v.detail, reviewer_tool=self.runner.tool)

    def _head_sha(self, repo: str, pr_number: int) -> str:
        r = _run(["gh", "pr", "view", str(pr_number), "--repo", repo,
                  "--json", "headRefOid", "-q", ".headRefOid"])
        return r.stdout.strip() if r.returncode == 0 else ""
