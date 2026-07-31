"""Cross-tool review-and-fix → a machine-readable verdict (ADR 0003, 0004, 0047).

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
- Structured finding actions are fail-safe: malformed actions cannot produce a clean result.
- Legacy severity verdicts remain fail-safe: anything not explicitly non-blocking counts as
  blocking while old durable records drain.
- Malformed containers, duplicate keys, and any parse exception → not-clean.

Review launch is owned by the session coordinator. This module keeps the durable prompt,
worktree naming, and pure verdict parser used by that adapter. Reviewers may now ship clear,
bounded fixes on the same PR branch before returning the verdict; the starting and final head
SHAs keep that mutation inside the exact-head gate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agentflow.review_policy import (
    FollowUp,
    ReviewAction,
    ReviewAxis,
    ReviewDepth,
    ReviewFinding,
    Uncertainty,
    parse_review_result,
)
from agentflow.screenshot_crib import SCREENSHOT_HARNESS
from agentflow.shell_crib import SHELL_CRIB
from agentflow.worktree_ref import WorktreeRef


# Legacy severities accepted as non-blocking while pre-ADR-0047 durable verdicts drain. Anything
# else (including empty or unknown values) remains blocking.
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
        "final_sha": {"type": "string"},
        "pushed_sha": {"type": "string"},
        "fixes": {"type": "array", "items": {"type": "string"}},
        "depth": {"type": "string", "enum": ["focused", "targeted", "full"]},
        "depth_reason": {"type": "string"},
        "axis": {"type": "string", "enum": [
            "combined", "product", "standards", "fix", "decision"]},
        "change_author_tool": {"type": "string", "enum": ["claude", "codex"]},
        "checks": {"type": "array", "items": {"type": "string"}},
        "decision": {"type": "string"},
        "follow_ups": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "url": {"type": "string"}, "evidence": {"type": "string"},
                    "desired_outcome": {"type": "string"},
                    "duplicate_query": {"type": "string"},
                },
                "required": ["url", "evidence", "desired_outcome", "duplicate_query"],
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "enum": [
                        "fix_before_completion", "necessary_follow_up", "ask_maintainer",
                        "discard_preference"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
                    "grounding": {"type": "string"},
                },
                "required": ["action", "file", "line", "summary", "grounding"],
            },
        },
        "uncertainty": {
            "anyOf": [
                {"type": "null"},
                {"type": "object", "additionalProperties": False,
                 "properties": {
                     "options": {"type": "array", "minItems": 2, "maxItems": 2,
                                 "items": {"type": "string"}},
                     "missing_guidance": {"type": "string"},
                     "recommendation": {"type": "string"},
                 },
                 "required": ["options", "missing_guidance", "recommendation"]},
            ],
        },
    },
    "required": ["verdict", "depth", "depth_reason", "axis", "change_author_tool",
                 "reviewed_sha", "final_sha", "pushed_sha", "fixes", "follow_ups",
                 "checks", "decision", "findings", "uncertainty"],
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
    reviewed_sha: str = ""
    final_sha: str = ""
    pushed_sha: str = ""
    fixes: tuple[str, ...] = ()
    follow_up_issues: tuple[str, ...] = ()
    depth: ReviewDepth = ReviewDepth.TARGETED
    depth_reason: str = ""
    axis: ReviewAxis = ReviewAxis.COMBINED
    change_author_tool: str = ""
    follow_ups: tuple[FollowUp, ...] = ()
    checks: tuple[str, ...] = ()
    actions: tuple[ReviewFinding, ...] = ()
    uncertainty: Uncertainty | None = None
    decision: str = ""

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


def parse_verdict(payload: str, expected_sha: str | None = None, *,
                  expected_depth: str | ReviewDepth | None = None,
                  expected_axis: str | ReviewAxis | None = None,
                  expected_author: str | None = None,
                  owned_heads: tuple[str, ...] = ()) -> Verdict:
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

        if "depth" in data:
            result = parse_review_result(
                stripped, expected_sha=expected_sha, expected_depth=expected_depth,
                expected_axis=expected_axis, expected_author=expected_author,
                owned_heads=owned_heads)
            if not result.parsed:
                return _unparseable(result.detail)
            compatibility_findings = tuple(
                Finding("blocking" if finding.action in {ReviewAction.FIX, ReviewAction.ASK}
                        else "nit", finding.summary, finding.file, finding.line)
                for finding in result.findings)
            return Verdict(
                clean=result.clean, findings=compatibility_findings, parsed=True,
                reviewed_sha=result.reviewed_sha, final_sha=result.final_sha,
                pushed_sha=result.pushed_sha, fixes=result.fixes,
                follow_up_issues=tuple(item.url for item in result.follow_ups),
                depth=result.depth, depth_reason=result.depth_reason, axis=result.axis,
                change_author_tool=result.change_author_tool, follow_ups=result.follow_ups,
                checks=result.checks, actions=result.findings, uncertainty=result.uncertainty,
                decision=result.decision)

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

        reviewed_sha = str(data.get("reviewed_sha", ""))
        if expected_sha is not None and reviewed_sha != expected_sha:
            return _unparseable("reviewed_sha missing or does not match the PR head")

        # Backward compatibility for verdicts captured before Review gained a write surface:
        # their reviewed head is also their final head and they carry no fix/follow-up ledger.
        final_sha = str(data.get("final_sha") or reviewed_sha)
        pushed_sha = str(data.get("pushed_sha") or "")
        raw_fixes = data.get("fixes", [])
        raw_follow_ups = data.get("follow_up_issues", [])
        if (not isinstance(raw_fixes, list) or not all(isinstance(v, str) for v in raw_fixes)
                or not isinstance(raw_follow_ups, list)
                or not all(isinstance(v, str) for v in raw_follow_ups)):
            return _unparseable("fixes or follow_up_issues is not a string list")
        if (final_sha != reviewed_sha and not pushed_sha
                and final_sha not in owned_heads):
            # A continuation reviewer whose fixes were pushed by an earlier attempt of the same
            # logical review honestly reports pushed_sha: "" — the caller proves that head's
            # provenance durably (the retained checkout owns it) and names it in owned_heads.
            return _unparseable("changed final head has no push provenance")
        if pushed_sha and (pushed_sha != final_sha or final_sha == reviewed_sha):
            return _unparseable("pushed_sha does not identify a new final head")
        if raw_fixes and not pushed_sha:
            return _unparseable("fixes have no push provenance")

        said_pass = str(data.get("verdict", "")).upper() == "PASS"
        has_blocking = any(f.severity == "blocking" for f in findings)
        return Verdict(
            clean=said_pass and not has_blocking, findings=tuple(findings), parsed=True,
            reviewed_sha=reviewed_sha, final_sha=final_sha, pushed_sha=pushed_sha,
            fixes=tuple(raw_fixes), follow_up_issues=tuple(raw_follow_ups))
    except Exception as e:  # noqa: BLE001 — the whole point is to never propagate
        return _unparseable(f"parse error: {type(e).__name__}")


REVIEW_PROMPT = """You are the independent cross-tool reviewer-and-fixer for PR #{pr} in this repo.
A different agent built it to satisfy a specific issue. The review started at exact head
`{starting_sha}`. Decide if it is safe to merge, and ship any clear fixes you find.

The issue's acceptance criteria — judge against THIS, not your own wishlist:
---
{acceptance}
---

First, prove you looked: run `gh pr view {pr} --json headRefOid,files,body` and
`gh pr diff {pr}`. If any changed file is under a user-facing surface (this repo's are:
{surfaces}), also run `gh pr view {pr} --json comments` to check for attached screenshots.
Judge the PR as a merge-ready artifact — its body and evidence, not only its diff.

Also read the checks reported on the exact head you are reviewing (`gh pr checks {pr}`) and
record what you saw in your "checks" proof. A red check you can fix, fix and push like any other
correction. A red check you cannot fix is `fix_before_completion` — never PASS over it. Pending
checks block nothing. (A mechanical gate also reads the reviewed head's checks at settlement,
independent of your verdict — you cannot waive this one.)

If your local test run fails for reasons that look environmental, a differential run against
`main` counts as evidence ONLY when the baseline is demonstrably the baseline — same interpreter,
same installed package, the compared tree actually checked out — and your "checks" proof states
how you verified that. Dismiss a failure the change could plausibly reach only with an
individually named cause, never wholesale because the set matches `main`. A local run too noisy
to read is unusable evidence: say so, and lean on the PR's own checks instead of reasoning
around the noise.

You own bounded cleanup in this review; do not merely report work you can safely finish:
- Fix every clear in-scope correctness, security, acceptance, or charter issue yourself.
- Fix grounded stale helpers, broken evidence, misleading prose, and naming/style defects when they
  matter. Discard unsupported taste; clear and project-grounded wording is not work.
- Speculative hardening is not work either: an edge case is real only when its state is reachable
  under the system's enforced invariants — rejected or made unrepresentable before the guard by code
  or a pinned test. Guards at a trust boundary are never speculative (external input, cross-process
  or cross-tool data, concurrent state, durable state a crash, a human edit, or the clock can
  perturb — an illustrative list, not a closed one). Elsewhere, a guard for a state that cannot occur
  is complexity, not safety: when you were about to ask for one, `discard_preference`; when the
  builder already shipped one, `fix_before_completion` by deleting it, and name in the finding the
  enforced invariant that makes the state unreachable. Keep every guard that acceptance, security,
  or observed behavior grounds.
- For a real necessary gap outside this PR's scope, search existing issues first, then file one
  follow-up issue in this repo. Record its URL, evidence, desired outcome, and duplicate-search
  query. Do not file issues for unsupported preferences.
- If a choice is materially ambiguous, changes product intent, needs missing context, or cannot be
  verified safely, do not guess. Record exactly two options, missing guidance, and your
  recommendation for agentflow's one private cross-tool decision handoff. Do not comment on GitHub.

When you change anything, work only in this checkout, run the relevant tests, commit it, and push
to this PR's SAME head branch (`gh pr view {pr} --json headRefName`). This checkout is detached, so
push explicitly with `git push origin HEAD:<headRefName>`. Never force-push, merge, or open another
PR. A rejected push means the branch moved concurrently: do not overwrite it; leave a
`fix_before_completion` finding. If your fix changes user-facing output, refresh the required
before/after evidence, commit it, and update
the PR body just as the original builder would. """ + SCREENSHOT_HARNESS + """
Before every push, ensure every non-merge commit you create or amend is DCO-signed: use
`git commit -s` for new commits and `git commit --amend -s` for amendments. Each
`Signed-off-by` email must match the Git commit author email. On a continuation, inspect
the existing work and history first; repair an unsigned existing commit with an amendment
where appropriate, never a separate sign-off-only or duplicate-work commit.
After a successful push, re-read the PR head and
review the complete final diff again.

Ground every observation in acceptance, security, or the engineering charter. Clear
`fix_before_completion` examples include:
- a real bug or security hole that breaks a stated acceptance criterion;
- a violation of the engineering charter in your instructions:
  - a shallow module, an unmocked UI surface, or an interface you cannot test through;
  - a **PR body not framed for the human who merges** — it leans on file / function /
    test names or CSS / API specifics instead of plain app behavior (ADR 0018);
  - a **user-facing change with no screenshot** — any PR whose files touch a
    user-facing surface ({surfaces}) must ship before/after screenshots, covering both
    light and dark themes where the app has them; missing evidence requires a fix (ADR 0018).
    Screenshots committed on the branch under `docs/screenshots/` (usually also embedded
    in the body as markdown images) COUNT as attached — open them in your checkout, don't
    block just because an image link doesn't render inline. Backend-only PRs need none.
    (Note: a mechanical gate also parks such a PR independent of your verdict — you
    cannot waive this one.)
  - a **screenshot that violates the locked visual contract** — if the acceptance brief carries a
    `LOCKED visual contract`, OPEN the implementation screenshots and compare them to it line by
    line. A mismatch is `fix_before_completion` ONLY when it breaks a STATED contract line (a
    named visual rule, a required interaction/state, a state the contract says must be
    screenshotted but isn't, or an out-of-scope boundary the build crossed). Unstated visual taste
    the contract never claimed is NOT a violation — that stays `discard_preference`. No contract in
    the brief means there is nothing here to check.
A correctness gap beyond the stated acceptance becomes `necessary_follow_up` only when it is a real,
evidenced improvement outside this PR's purpose. Otherwise discard it as unsupported scope growth.

Choose exactly one action for every observation:
1. `fix_before_completion` — ship the clear correction before returning.
2. `necessary_follow_up` — file and prove real out-of-scope work.
3. `ask_maintainer` — only for unresolved product intent after the narrow decision handoff.
4. `discard_preference` — unsupported reviewer taste; creates no work.

Escalate depth, never downgrade it:
- Focused to Targeted when a screenshot-link correction reveals the screenshot no longer matches.
- Focused to Full when one line changes a destructive or safety-sensitive action.
- Targeted to Full when a contained journey changes a shared decision used elsewhere.
- Conflict to Full when apparent wording chooses between product behaviors.

Your final response IS the verdict as a structured object — the harness enforces its
schema natively, so you do not hand-write or fence the JSON; just produce these fields:
- "verdict": "PASS" | "BLOCK" — PASS only when no `fix_before_completion` or
  `ask_maintainer` action remains
- "reviewed_sha": `{starting_sha}` (proof you inspected the exact starting diff)
- "final_sha": the exact final head you reviewed after any push (same as reviewed_sha if unchanged)
- "pushed_sha": the exact pushed head if THIS session pushed to the branch, otherwise an empty
  string
- "fixes": concise human-facing summaries of the fixes THIS session pushed — nothing else.
  If a previous session already pushed fixes and you only re-verified its head, this is an
  empty list and "pushed_sha" is an empty string; those earlier fixes are already recorded
  and restating them here contradicts your own push provenance and voids the verdict.
- "checks": exact proof/checks you completed
- "follow_ups": necessary issues as {{"url", "evidence", "desired_outcome", "duplicate_query"}}
- "findings": unresolved/discarded observations as {{"action", "file", "line", "summary",
  "grounding"}}. Do not report already-fixed issues as unresolved findings.
- "uncertainty": null, or {{"options": [exactly two], "missing_guidance", "recommendation"}}
- "decision": the grounded choice for a decision-axis pass, otherwise an empty string
- "depth", "depth_reason", "axis", and "change_author_tool": repeat the assigned durable values.""" + SHELL_CRIB


_ASSIGNMENT_START = "<!-- agentflow-review-assignment:start -->"
_ASSIGNMENT_END = "<!-- agentflow-review-assignment:end -->"
_MARKED_ASSIGNMENT_RE = re.compile(
    rf"\s*{re.escape(_ASSIGNMENT_START)}.*?{re.escape(_ASSIGNMENT_END)}",
    re.DOTALL)
_LEGACY_ASSIGNMENT_RE = re.compile(
    r"\n\nPrivate review assignment \(verify it; do not publish this handoff\):.*?"
    r"complete required checks\.", re.DOTALL)


def review_assignment_prompt(*, depth: ReviewDepth, reason: str, axis: ReviewAxis,
                             change_author_tool: str, handoff: str = "") -> str:
    """Render one marked private assignment block."""
    context = handoff.strip() or "No earlier private review handoff; verify the PR directly."
    axis_rule = {
        ReviewAxis.PRODUCT: "Inspect product outcome only. Do not edit during this axis pass.",
        ReviewAxis.STANDARDS: ("Inspect project standards only. Do not edit during this axis pass. "
                               "Preserve every grounded product finding from the private handoff "
                               "in your result so the fix pass receives the combined set."),
        ReviewAxis.FIX: "Ship the combined clear fixes, then record the pushed head.",
        ReviewAxis.DECISION: ("Resolve only the two supplied product options. Do not edit. "
                              "Set decision to the chosen option and its grounded reason, or "
                              "return the remaining uncertainty."),
        ReviewAxis.COMBINED: "Verify the assigned scope and ship clear fixes.",
    }[axis]
    return (
        f"\n\n{_ASSIGNMENT_START}\n"
        "Private review assignment (verify it; do not publish this handoff):\n"
        f"- Depth: {depth.value.title()} — {reason}\n"
        f"- Axis: {axis.value}. {axis_rule}\n"
        f"- Current change author tool: {change_author_tool}\n"
        f"- Prior handoff: {context}\n"
        "Focused verifies the exact change/proof and that nothing else moved. Targeted verifies "
        "the contained behavior and relevant project rules. Full verifies the whole PR with "
        f"complete required checks.\n{_ASSIGNMENT_END}")


def with_review_assignment(prompt: str, *, depth: ReviewDepth, reason: str, axis: ReviewAxis,
                           change_author_tool: str, handoff: str = "") -> str:
    """Replace every old assignment with one current bounded assignment."""
    base = _MARKED_ASSIGNMENT_RE.sub("", prompt or "")
    base = _LEGACY_ASSIGNMENT_RE.sub("", base).rstrip()
    return base + review_assignment_prompt(
        depth=depth, reason=reason, axis=axis,
        change_author_tool=change_author_tool, handoff=handoff)


def review_worktree(workdir: str, tool: str, pr_number: int, slug: str) -> Path:
    return Path(WorktreeRef.for_review(workdir, tool, pr_number, slug).path)
