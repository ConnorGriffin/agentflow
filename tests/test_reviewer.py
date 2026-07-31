"""Test the reviewer through its interface — the pure, fail-safe verdict parser.

The load-bearing property: anything we cannot read as a clean PASS must come back
`clean=False`, so the auto-merge gate never lands a diff on an unreadable review.
Cases marked `# adversarial` are regressions for the refutation pass that hardened
this module (see reviewer.py docstring / git history).
"""

import pytest

from agentflow.reviewer import REVIEW_PROMPT, parse_verdict, review_worktree


def test_reviewer_prompt_exposes_only_the_four_action_vocabulary():
    prompt = REVIEW_PROMPT.lower()

    assert all(action in prompt for action in (
        "fix_before_completion", "necessary_follow_up", "ask_maintainer",
        "discard_preference"))
    assert "blocking" not in prompt
    assert "severity" not in prompt
    assert " nit" not in prompt


def test_reviewer_prompt_calibrates_against_speculative_hardening():
    prompt = " ".join(REVIEW_PROMPT.split()).lower()

    assert "speculative hardening is not work" in prompt
    assert "reachable under the system's enforced invariants" in prompt
    # The carve-out, so the calibration never argues away a real trust boundary.
    assert "trust boundary are never speculative" in prompt
    assert "an illustrative list, not a closed one" in prompt
    # Routed through the existing actions — discard the ask, delete the guard a builder shipped,
    # and say what makes the state unreachable.
    assert "when you were about to ask for one, `discard_preference`" in prompt
    assert "`fix_before_completion` by deleting it" in prompt
    assert "name in the finding the enforced invariant" in prompt


def test_pass_with_no_findings_is_clean():
    assert parse_verdict('{"verdict": "PASS", "findings": []}').clean is True


def test_block_with_a_blocking_finding_is_not_clean():
    v = parse_verdict('{"verdict": "BLOCK", "findings": [{"severity": "blocking", "summary": "off-by-one"}]}')
    assert v.clean is False and len(v.blocking) == 1


def test_pass_but_lists_a_blocking_finding_is_still_not_clean():
    v = parse_verdict('{"verdict": "PASS", "findings": [{"severity": "blocking", "summary": "sqli"}]}')
    assert v.clean is False


def test_nits_only_is_clean():
    v = parse_verdict('{"verdict": "PASS", "findings": [{"severity": "nit", "summary": "rename x"}]}')
    assert v.clean is True and v.blocking == []


# adversarial #3/#6 — severity synonyms must NOT downgrade to nit
@pytest.mark.parametrize("sev", ["BLOCKER", "critical", "high", "severe", "blocking ", "", "moderate", "error"])
def test_non_nit_severity_counts_as_blocking(sev):
    payload = f'{{"verdict": "PASS", "findings": [{{"severity": "{sev}", "summary": "x"}}]}}'
    assert parse_verdict(payload).clean is False


# adversarial #5/#6 — malformed findings containers must fail safe, not crash or empty
@pytest.mark.parametrize("findings", ["5", "true", '"blocking"', '{"severity": "blocking"}', '["blocking: sqli"]'])
def test_malformed_findings_container_is_not_clean(findings):
    v = parse_verdict(f'{{"verdict": "PASS", "findings": {findings}}}')
    assert v.clean is False


# adversarial #7 — duplicate verdict keys must not flip BLOCK->PASS
def test_duplicate_verdict_keys_is_not_clean():
    assert parse_verdict('{"verdict": "BLOCK", "verdict": "PASS", "findings": []}').clean is False


def test_malformed_json_is_unparseable_and_not_clean():
    v = parse_verdict("not json at all")
    assert v.clean is False and v.parsed is False


def test_missing_verdict_field_is_not_clean():
    assert parse_verdict('{"findings": []}').clean is False


def test_empty_payload_is_not_clean():
    assert parse_verdict("").clean is False


def test_pure_structured_verdict_parses_with_surrounding_whitespace():
    # Native schema output is the verdict object itself; only surrounding whitespace is tolerated.
    v = parse_verdict('\n\n{"verdict": "PASS", "reviewed_sha": "abc123", "findings": []}\n\n',
                      expected_sha="abc123")
    assert v.clean is True and v.parsed is True


@pytest.mark.parametrize("payload", [
    'Here is my review:\n```json\n{"verdict": "PASS", "findings": []}\n```\nDone.',
    "No blocking issues.\n\n{\"verdict\": \"PASS\", \"reviewed_sha\": \"abc123\", \"findings\": []}",
    'Reasoning first...\n{"verdict": "BLOCK", "verdict": "PASS", "findings": []}',
])
def test_prose_wrapped_verdict_is_no_longer_scavenged(payload):
    # The prompt-only JSON extraction is gone: with a native schema the verdict is pure
    # structured output, so a verdict buried in reasoning prose is unreadable and never clean.
    v = parse_verdict(payload, expected_sha="abc123")
    assert v.clean is False and v.parsed is False


# adversarial #2 — proof-of-work: the verdict must name the head SHA we're merging
def test_sha_match_required_when_expected():
    ok = '{"verdict": "PASS", "reviewed_sha": "abc123", "findings": []}'
    assert parse_verdict(ok, expected_sha="abc123").clean is True
    assert parse_verdict(ok, expected_sha="deadbeef").clean is False
    assert parse_verdict('{"verdict": "PASS", "findings": []}', expected_sha="abc123").clean is False


# adversarial #5 — the "fail-safe" parser must NEVER raise, whatever the input
@pytest.mark.parametrize("payload", ["", "null", "[]", "5", '{"verdict": 5}',
                                     '{"verdict": "PASS", "findings": 5}',
                                     '{"verdict": "PASS", "findings": [null]}',
                                     "\x00\xff not utf clean", '{"verdict"}'])
def test_parse_never_raises(payload):
    assert parse_verdict(payload).clean is False  # returns, does not throw


def test_review_worktree_path_matches_worktree_ref_convention():
    # The path must encode (workdir, tool, pr_number, slug) in the shared layout.
    p = review_worktree("/work", "claude", 42, "fix-thing")
    assert str(p) == "/work/.agentflow/worktrees/claude-review/pr-42-fix-thing"


def test_review_prompt_formats_and_carries_the_evidence_gates():
    # The live reviewer formats this before every review (reviewer.py: launch). A stray
    # unescaped brace in the rubric would KeyError here and wedge every review — so this
    # both guards the bracing and locks ADR 0018's two always-on gates into the rubric,
    # without which the reviewer structurally can't block on them.
    body = REVIEW_PROMPT.format(
        pr=42, starting_sha="abc123", acceptance="ships a thing",
        surfaces="`agentflow/static/`")
    assert "#42" in body and "ships a thing" in body
    # the reviewer must actually fetch the body/files, not just the diff
    assert "headRefOid,files,body" in body
    assert "screenshot" in body.lower()                # UI-change evidence gate
    assert "framed for the human" in body.lower()      # plain-language / no-jargon gate
    assert "agentflow/static/" in body                 # the repo's declared surfaces, not a hardcoded example
    assert "ship any clear fixes" in body.lower()      # Review owns safe fixes, not report-only nits
    assert "follow-up issue" in body.lower()           # necessary out-of-scope work is not lost


def test_review_prompt_judges_screenshots_against_the_locked_contract():
    # ADR 0048: when the brief carries a LOCKED visual contract, the reviewer compares the
    # implementation screenshots to it — a STATED-line violation is fix_before_completion, while
    # unstated visual taste stays discard_preference (the four-action split is preserved).
    body = REVIEW_PROMPT.format(
        pr=42, starting_sha="abc", acceptance="a", surfaces="`agentflow/webui/src/`")
    lower = body.lower()
    assert "locked visual contract" in lower
    assert "stated" in lower                            # only a stated-line violation blocks
    assert "fix_before_completion" in lower and "discard_preference" in lower


def test_review_fix_ledger_binds_the_final_pushed_head():
    payload = '''{
      "verdict": "PASS",
      "reviewed_sha": "start",
      "final_sha": "fixed",
      "pushed_sha": "fixed",
      "fixes": ["Removed the stale helper"],
      "follow_up_issues": ["https://github.com/o/r/issues/9"],
      "findings": []
    }'''
    verdict = parse_verdict(payload, expected_sha="start")
    assert verdict.clean is True
    assert verdict.final_sha == "fixed" and verdict.pushed_sha == "fixed"
    assert verdict.fixes == ("Removed the stale helper",)
    assert verdict.follow_up_issues == ("https://github.com/o/r/issues/9",)


def test_review_fix_ledger_rejects_a_push_that_is_not_the_final_reviewed_head():
    payload = '''{
      "verdict": "PASS",
      "reviewed_sha": "start",
      "final_sha": "reviewed",
      "pushed_sha": "different",
      "fixes": ["Changed it"],
      "follow_up_issues": [],
      "findings": []
    }'''
    verdict = parse_verdict(payload, expected_sha="start")
    assert verdict.parsed is False and verdict.clean is False


def test_parse_verdict_accepts_a_prior_attempt_final_head_the_caller_proved():
    """A continuation reviewer whose fixes were pushed by an earlier attempt of the same logical
    review honestly reports ``pushed_sha: ""`` — the prompt orders exactly that — and the strict
    provenance rule rejected it, so the honest verdict could never parse and the review parked
    after burning its budget (the #346-class park). A final head the caller proved durably
    (``owned_heads``) is accepted; an unproven one stays rejected."""
    import json
    payload = json.dumps({"verdict": "PASS", "reviewed_sha": "sha-a",
                          "final_sha": "sha-b", "pushed_sha": "", "findings": []})

    strict = parse_verdict(payload, expected_sha="sha-a")
    assert strict.parsed is False and "provenance" in strict.detail

    accepted = parse_verdict(payload, expected_sha="sha-a", owned_heads=("sha-b",))
    assert accepted.parsed is True and accepted.clean is True
    assert accepted.final_sha == "sha-b" and accepted.pushed_sha == ""
