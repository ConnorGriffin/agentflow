"""Test the reviewer through its interface — the pure, fail-safe verdict parser.

The load-bearing property: anything we cannot read as a clean PASS must come back
`clean=False`, so the auto-merge gate never lands a diff on an unreadable review.
Cases marked `# adversarial` are regressions for the refutation pass that hardened
this module (see reviewer.py docstring / git history).
"""

import pytest

from agentflow.reviewer import REVIEW_PROMPT, parse_verdict


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


def test_review_prompt_formats_and_carries_the_evidence_gates():
    # The live reviewer formats this before every review (reviewer.py: launch). A stray
    # unescaped brace in the rubric would KeyError here and wedge every review — so this
    # both guards the bracing and locks ADR 0018's two always-on gates into the rubric,
    # without which the reviewer structurally can't block on them.
    body = REVIEW_PROMPT.format(pr=42, acceptance="ships a thing", surfaces="`agentflow/static/`")
    assert "#42" in body and "ships a thing" in body
    # the reviewer must actually fetch the body/files, not just the diff
    assert "headRefOid,files,body" in body
    assert "screenshot" in body.lower()                # UI-change evidence gate
    assert "framed for the human" in body.lower()      # plain-language / no-jargon gate
    assert "agentflow/static/" in body                 # the repo's declared surfaces, not a hardcoded example
