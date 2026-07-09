"""Test the reviewer through its interface — the pure, fail-safe verdict parser.

The load-bearing property: anything we cannot read as a clean PASS must come back
`clean=False`, so the auto-merge gate never lands a diff on an unreadable review.
"""

from agentflow.reviewer import Verdict, parse_verdict, review_tier
from agentflow.runner import Tier


def test_pass_with_no_findings_is_clean():
    v = parse_verdict('{"verdict": "PASS", "findings": []}')
    assert v.clean is True and v.parsed is True


def test_block_with_a_blocking_finding_is_not_clean():
    v = parse_verdict('{"verdict": "BLOCK", "findings": [{"severity": "blocking", "summary": "off-by-one"}]}')
    assert v.clean is False
    assert len(v.blocking) == 1


def test_pass_but_lists_a_blocking_finding_is_still_not_clean():
    # The defensive case: a model that contradicts itself must fail safe.
    v = parse_verdict('{"verdict": "PASS", "findings": [{"severity": "blocking", "summary": "sql injection"}]}')
    assert v.clean is False


def test_nits_only_is_clean():
    v = parse_verdict('{"verdict": "PASS", "findings": [{"severity": "nit", "summary": "rename x"}]}')
    assert v.clean is True
    assert v.blocking == []


def test_malformed_json_is_unparseable_and_not_clean():
    v = parse_verdict("not json at all")
    assert v.clean is False and v.parsed is False


def test_missing_verdict_field_is_not_clean():
    v = parse_verdict('{"findings": []}')
    assert v.clean is False and v.parsed is False


def test_empty_payload_is_not_clean():
    assert parse_verdict("").clean is False


def test_fenced_json_block_is_parsed():
    v = parse_verdict('```json\n{"verdict": "PASS", "findings": []}\n```')
    assert v.clean is True and v.parsed is True


def test_review_tier_floors_at_standard():
    assert review_tier(Tier.LIGHT) is Tier.STANDARD
    assert review_tier(Tier.STANDARD) is Tier.STANDARD
    assert review_tier(Tier.DEEP) is Tier.DEEP
