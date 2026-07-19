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

Review launch is owned by the session coordinator. This module keeps the durable prompt,
worktree naming, and pure verdict parser used by that adapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


# Severities we accept as non-blocking. ANYTHING else (incl. "", "critical",
# "blocker", "high", unknown) is treated as blocking — fail safe.
_NIT_SEVERITIES = {"nit", "nits", "info", "minor", "low", "style", "note",
                   "suggestion", "trivial", "cosmetic"}

# The provider-neutral shape a reviewer's terminal verdict must match. Each runner adapter
# translates it into that CLI's native structured-output surface (Claude `--json-schema`,
# Codex `--output-schema`); the CLI enforces it, so `parse_verdict` validates a real object
# rather than scavenging JSON out of free text.
REVIEW_VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "BLOCK"]},
        "reviewed_sha": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["blocking", "nit"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "required": ["severity", "file", "line", "summary"],
            },
        },
    },
    "required": ["verdict", "reviewed_sha", "findings"],
}


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


def parse_verdict(payload: str, expected_sha: str | None = None) -> Verdict:
    """Validate a reviewer's structured verdict. Pure, defensive, fail-safe (test surface).

    The CLI enforces `REVIEW_VERDICT_SCHEMA` natively, so the payload is the verdict object
    itself — parsed strictly (with the duplicate-key guard), never scavenged out of
    reasoning prose. `clean` requires: a JSON dict carrying `verdict`, verdict == PASS, no
    blocking finding, and — when `expected_sha` is given — a matching `reviewed_sha` (proof
    it reviewed the head we're about to merge). Any deviation returns not-clean.
    """
    try:
        stripped = payload.strip()
        if not stripped:
            return _unparseable("empty reviewer output")
        data = json.loads(stripped, object_pairs_hook=_no_duplicate_keys)
        if not isinstance(data, dict) or "verdict" not in data:
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
`gh pr diff {pr}`. If any changed file is under a user-facing surface (this repo's are:
{surfaces}), also run `gh pr view {pr} --json comments` to check for attached screenshots.
Judge the PR as a merge-ready artifact — its body and evidence, not only its diff.

BLOCKING (only these):
- a real bug or security hole that breaks a stated acceptance criterion, or
- a violation of the engineering charter in your instructions:
  - a shallow module, an unmocked UI surface, or an interface you cannot test through;
  - a **PR body not framed for the human who merges** — it leans on file / function /
    test names or CSS / API specifics instead of plain app behavior (ADR 0018);
  - a **user-facing change with no screenshot** — any PR whose files touch a
    user-facing surface ({surfaces}) must ship before/after screenshots, covering both
    light and dark themes where the app has them; none is a blocking gap (ADR 0018).
    Screenshots committed on the branch under `docs/screenshots/` (usually also embedded
    in the body as markdown images) COUNT as attached — open them in your checkout, don't
    block just because an image link doesn't render inline. Backend-only PRs need none.
    (Note: a mechanical gate also parks such a PR independent of your verdict — you
    cannot waive this one.)
A correctness gap BEYOND the stated acceptance — an unhandled case the issue did not
ask for — is a NIT, not blocking; note it so it can be filed as a follow-up.
Style, naming, and minor perf are nits.

Your final response IS the verdict as a structured object — the harness enforces its
schema natively, so you do not hand-write or fence the JSON; just produce these fields:
- "verdict": "PASS" | "BLOCK" — PASS only if there are zero blocking findings
- "reviewed_sha": the headRefOid you fetched above (proof you reviewed THIS diff)
- "findings": a list of {{"severity": "blocking" | "nit", "file": path, "line": 0,
  "summary": the human-facing note}}"""


def review_worktree(workdir: str, tool: str, pr_number: int, slug: str) -> Path:
    return (Path(workdir) / ".agentflow" / "worktrees" / f"{tool}-review" /
            f"pr-{pr_number}-{slug}")
