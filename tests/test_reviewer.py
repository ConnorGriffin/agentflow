"""Test the reviewer through its interface — the pure, fail-safe verdict parser.

The load-bearing property: anything we cannot read as a clean PASS must come back
`clean=False`, so the auto-merge gate never lands a diff on an unreadable review.
Cases marked `# adversarial` are regressions for the refutation pass that hardened
this module (see reviewer.py docstring / git history).
"""

from subprocess import CompletedProcess

import pytest

from agentflow.reviewer import REVIEW_PROMPT, Reviewer, Verdict, parse_verdict
from agentflow.runner import Complexity


class _RecordingRunner:
    tool = "claude"

    def __init__(self):
        self.complexities = []
        self.launched_model = ""

    def prepare_worktree_detached(self, *_args):
        pass

    def provision(self, *_args):
        pass

    def model_for(self, complexity):
        self.complexities.append(complexity)
        return f"model-for-{complexity.value}"

    def launch(self, _prompt, *, cwd, model):
        self.launched_model = model
        return True, '{"verdict":"PASS","reviewed_sha":"abc123","findings":[]}'


def test_every_review_uses_deep_complexity_through_the_public_interface(monkeypatch, tmp_path):
    monkeypatch.setattr("agentflow.reviewer._run",
                        lambda *_args, **_kwargs: CompletedProcess([], 0, "abc123\n", ""))
    runner = _RecordingRunner()

    verdict = Reviewer(runner).review("owner/repo", str(tmp_path), 28, "feature", "issue-28")

    assert verdict.clean is True
    assert runner.complexities == [Complexity.DEEP]
    assert runner.launched_model == "model-for-deep"


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


def test_prose_then_fenced_json_is_parsed():
    payload = "Here is my review:\n```json\n{\"verdict\": \"PASS\", \"findings\": []}\n```\nDone."
    assert parse_verdict(payload).clean is True


def test_prose_then_bare_json_verdict_is_recovered():
    # The live loop failure (PR #7): the reviewer reasons, then emits a bare verdict.
    payload = ("I checked the diff against the acceptance criteria — word_count is correct\n"
               "and the test covers empty + multi-word. No blocking issues.\n\n"
               '{"verdict": "PASS", "reviewed_sha": "abc123", "findings": []}')
    v = parse_verdict(payload, expected_sha="abc123")
    assert v.clean is True and v.parsed is True


def test_prose_then_bare_json_block_recovers_findings():
    payload = 'Looks off.\n{"verdict": "BLOCK", "findings": [{"severity": "blocking", "summary": "off-by-one"}]}'
    v = parse_verdict(payload)
    assert v.clean is False and len(v.blocking) == 1


# adversarial: dup-key protection must survive even with leading reasoning prose
def test_prose_then_duplicate_verdict_keys_is_not_clean():
    payload = 'Reasoning first...\n{"verdict": "BLOCK", "verdict": "PASS", "findings": []}'
    assert parse_verdict(payload).clean is False


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
