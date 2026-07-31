"""Test intake through its interface — the pure, fail-safe decision parser and the
label mapping. Like the reviewer: anything we cannot read as a confident build-ready
decision must fall back to holding for a human, never an accidental `ready`.

The GitHub-touching tests state facts through the shared `github` module's interface
(ADR 0040) — a canned typed read, or a recorded typed write — never a `gh` argument
vector. A read that returns ``None`` models the module's fail-closed "unreadable"
contract, which intake must never confuse with an empty subject.
"""

import pytest

from agentflow import intake as intake_mod
from agentflow.github import Comment, IssueRow, IssueView
from agentflow.intake import (INTAKE_MARK, IntakeResult, IntakeRoute, apply_intake,
                              awaiting_recheck, compose_ready_body, intake_labels,
                              intake_prompt, intake_result_is_durable, parse_intake,
                              replies_since_intake, sweep_legacy_labels)
from agentflow.runner import Complexity, Effort, MockupScope


def test_ready_with_all_fields_is_build_ready():
    v = parse_intake('{"route": "ready", "title": "Widen the measurement window", '
                     '"complexity": "standard", "effort": "high", "body": "## Agent Brief\\n..."}')
    assert v.route is IntakeRoute.READY and v.parsed
    assert v.complexity is Complexity.STANDARD and v.effort is Effort.HIGH
    assert v.title == "Widen the measurement window" and v.body.startswith("## Agent Brief")


def test_ready_missing_complexity_is_an_invalid_result_not_a_deep_default():
    # A ready with no complexity used to silently size up to deep; it must now be an
    # explicit invalid result so a garbled decision never upgrades the build on its own.
    v = parse_intake('{"route": "ready", "title": "t", "body": "brief", "effort": "low"}')
    assert v.parsed is False and v.route is IntakeRoute.GRILL
    assert "complexity" in v.detail
    invalid = parse_intake(
        '{"route": "ready", "title": "t", "body": "brief", "complexity": "huge"}')
    assert invalid.parsed is False and invalid.route is IntakeRoute.GRILL


def test_ready_missing_effort_defaults_medium():
    assert parse_intake(
        '{"route": "ready", "title": "t", "body": "b", "complexity": "deep"}').effort is Effort.MEDIUM


def test_ready_without_a_title_preserves_the_ready_route():
    # Title rewriting is optional routing output; coordinated projection preserves the durable
    # filed title when it is omitted.
    titleless = parse_intake('{"route": "ready", "body": "brief", "complexity": "deep"}')
    assert titleless.parsed is True and titleless.route is IntakeRoute.READY
    assert parse_intake(
        '{"route": "ready", "title": "   ", "body": "brief", "complexity": "deep"}'
    ).route is IntakeRoute.READY


def test_grill_and_mockup_routes():
    assert parse_intake('{"route": "grill", "body": "which did you mean?"}').route is IntakeRoute.GRILL
    assert parse_intake('{"route": "mockup", "body": "kickoff"}').route is IntakeRoute.MOCKUP


def test_unknown_route_holds_for_human():
    v = parse_intake('{"route": "merge-it", "body": "x"}')
    assert v.route is IntakeRoute.GRILL and v.parsed is False


def test_ready_without_a_body_holds():
    assert parse_intake('{"route": "ready", "body": "   ", "complexity": "deep"}').parsed is False


def test_non_object_payload_holds():
    v = parse_intake('["ready"]')
    assert v.route is IntakeRoute.GRILL and v.parsed is False


def test_pure_structured_decision_parses_with_surrounding_whitespace():
    # Native schema output is the decision object itself; only surrounding whitespace is tolerated.
    payload = '\n\n{"route": "grill", "body": "which did you mean?"}\n\n'
    assert parse_intake(payload).route is IntakeRoute.GRILL


@pytest.mark.parametrize("payload", [
    'Here you go:\n```json\n{"route": "grill", "body": "q"}\n```\n',
    'The premise holds.\n\n{"route": "grill", "title": "t", "body": "which did you mean?"}',
    'For example {"route": "ready", "body": "x"} but actually\n{"route": "grill", "body": "q"}',
])
def test_prose_wrapped_json_is_no_longer_scavenged(payload):
    # The prompt-only JSON extraction is gone: with a native schema the decision is pure
    # structured output, so anything wrapped in reasoning prose is an invalid result held for
    # a human, not a decision dug out of the text.
    v = parse_intake(payload)
    assert v.parsed is False and v.route is IntakeRoute.GRILL


# fail-safe: whatever the input, intake never raises and never invents a `ready`
@pytest.mark.parametrize("payload", ["", "null", "5", "not json", '{"route": 5}',
                                     '{"route": "ready"}', '{"body": "x"}', '{"route"}'])
def test_parse_never_raises_and_holds(payload):
    assert parse_intake(payload).route is IntakeRoute.GRILL


def test_labels_for_ready_carry_both_dials():
    r = parse_intake('{"route": "ready", "title": "t", "body": "b", "complexity": "deep", "effort": "extra"}')
    assert intake_labels(r) == ["ready-for-agent", "agentflow:complexity:deep", "agentflow:effort:extra"]


def test_labels_for_holds_are_a_single_state():
    assert intake_labels(parse_intake('{"route": "grill", "body": "q"}')) == ["agentflow:needs-grilling"]
    # a mockup hold carries its scope alongside the state label; missing scope defaults to local
    assert intake_labels(parse_intake('{"route": "mockup", "body": "m"}')) == [
        "agentflow:needs-mockup", "agentflow:mockup:local"]


def test_mockup_scope_parsed_and_defaults_local():
    surface = parse_intake('{"route": "mockup", "body": "m", "mockup_scope": "surface"}')
    assert surface.route is IntakeRoute.MOCKUP and surface.mockup_scope is MockupScope.SURFACE
    assert intake_labels(surface) == ["agentflow:needs-mockup", "agentflow:mockup:surface"]
    local = parse_intake('{"route": "mockup", "body": "m", "mockup_scope": "local"}')
    assert local.mockup_scope is MockupScope.LOCAL


@pytest.mark.parametrize("scope_json", [
    '', ', "mockup_scope": null', ', "mockup_scope": "everything"', ', "mockup_scope": 5'])
def test_mockup_scope_fails_safe_to_local(scope_json):
    # An unknown/missing/invalid scope on a mockup route never reopens the whole surface —
    # it fails safe to the narrower local round (ADR 0048).
    v = parse_intake('{"route": "mockup", "body": "m"' + scope_json + '}')
    assert v.route is IntakeRoute.MOCKUP and v.mockup_scope is MockupScope.LOCAL


def test_non_mockup_routes_carry_no_scope():
    ready = parse_intake('{"route": "ready", "title": "t", "body": "b", '
                         '"complexity": "deep", "effort": "low", "mockup_scope": "surface"}')
    assert ready.mockup_scope is None            # scope is meaningless off a mockup route
    assert parse_intake('{"route": "grill", "body": "q"}').mockup_scope is None


def _c(body, author=None):
    c = {"body": body}
    if author is not None:
        c["author"] = {"login": author}
    return c


def test_awaiting_recheck_true_when_maintainer_replied_last():
    comments = [_c("> *agentflow intake — generated by AI.*\n\nwhich did you mean?"),
                _c("option a, keep it simple")]
    assert awaiting_recheck(comments) is True


def test_awaiting_recheck_false_when_intake_spoke_last():
    comments = [_c("some human note"),
                _c("> *agentflow intake — generated by AI.*\n\nsharpened question?")]
    assert awaiting_recheck(comments) is False


def test_awaiting_recheck_false_without_comments():
    assert awaiting_recheck([]) is False
    assert awaiting_recheck([_c("   ")]) is False


def test_replies_since_intake_collects_only_the_new_answer():
    comments = [_c("> *agentflow intake — generated by AI.*\n\nold questions"),
                _c("answer one"), _c("answer two")]
    assert replies_since_intake(comments) == "answer one\n\nanswer two"


# --- quote-reply stripping -------------------------------------------------------

_INTAKE_COMMENT = "> *agentflow intake — generated by AI.*\n\nwhich did you mean?"
_QUOTE_REPLY = ("> > *agentflow intake — generated by AI.*\n>\n> which did you mean?\n\noption a")


def test_awaiting_recheck_true_for_quote_reply():
    # Before fix this would return False — the quoted marker fooled the check.
    comments = [_c(_INTAKE_COMMENT), _c(_QUOTE_REPLY)]
    assert awaiting_recheck(comments) is True


def test_awaiting_recheck_false_for_bare_intake_comment():
    # Pure intake comment (no real reply from maintainer) must still be False.
    comments = [_c("some text"), _c(_INTAKE_COMMENT)]
    assert awaiting_recheck(comments) is False


def test_replies_since_intake_returns_full_text_of_quote_reply():
    # The maintainer's full body (including the quoted section) survives extraction.
    comments = [_c(_INTAKE_COMMENT), _c(_QUOTE_REPLY, author="owner")]
    result = replies_since_intake(comments, allowlist={"owner"})
    assert "option a" in result
    assert result.strip() == _QUOTE_REPLY.strip()


def test_replies_since_intake_cut_point_sees_through_quoted_marker():
    # A comment that only quotes our marker (no new text) is NOT the cut point —
    # scan continues past it looking for our real intake comment.
    only_quoted = "> > *agentflow intake — generated by AI.*\n>\n> some context"
    comments = [_c(_INTAKE_COMMENT), _c(only_quoted, author="owner"), _c("answer", author="owner")]
    result = replies_since_intake(comments, allowlist={"owner"})
    assert "answer" in result


# --- allowlist filtering ---------------------------------------------------------

def test_awaiting_recheck_skips_non_allowlisted_author():
    # A drive-by comment must be invisible — it should not count as a maintainer reply.
    comments = [_c(_INTAKE_COMMENT), _c("option a, keep it simple", author="stranger")]
    assert awaiting_recheck(comments, allowlist={"owner"}) is False


def test_awaiting_recheck_true_for_allowlisted_author():
    comments = [_c(_INTAKE_COMMENT), _c("option a, keep it simple", author="owner")]
    assert awaiting_recheck(comments, allowlist={"owner"}) is True


def test_awaiting_recheck_none_allowlist_does_not_filter():
    # allowlist=None is backwards-compatible: any author counts.
    comments = [_c(_INTAKE_COMMENT), _c("option a", author="stranger")]
    assert awaiting_recheck(comments, allowlist=None) is True


def test_replies_since_intake_omits_non_allowlisted():
    comments = [_c(_INTAKE_COMMENT), _c("bot spam", author="bot"), _c("real answer", author="owner")]
    result = replies_since_intake(comments, allowlist={"owner"})
    assert "real answer" in result
    assert "bot spam" not in result


# --- the brief lands in the body (issue #16) -------------------------------------

def test_compose_ready_body_puts_brief_on_top_original_below():
    body = compose_ready_body("## Agent Brief\nthe scope", "the one-line as-filed text")
    assert body.startswith("## Agent Brief")
    # the as-filed text survives, under a collapsed block below the brief
    assert "<details>" in body and "the one-line as-filed text" in body
    assert body.index("## Agent Brief") < body.index("<details>")


def test_compose_ready_body_updates_in_place_on_reintake():
    # A second intake replaces the brief but must NOT nest the old brief or add a
    # second <details> — the true original stays preserved exactly once.
    first = compose_ready_body("## Brief v1", "the one-line as-filed text")
    second = compose_ready_body("## Brief v2", first)
    assert second.startswith("## Brief v2") and "## Brief v1" not in second
    assert second.count("<details>") == 1
    assert second.count("the one-line as-filed text") == 1


# --- GitHub projection through the shared module -----------------------------------

class FakeGH:
    """Stand-in for the `github` module intake calls: records the typed writes and serves
    canned typed reads. A ``None`` read answer models the module's fail-closed unreadable
    contract, distinct from an empty subject."""

    def __init__(self, *, body="", comment_bodies=(), comments_readable=True,
                 issue=None, issues=None):
        self._read_body = body                  # issue_body answer (None => unreadable)
        self._comment_bodies = comment_bodies   # bodies present on the issue thread
        self._comments_readable = comments_readable
        self._durability_issue = issue          # issue_view answer (None => unreadable)
        self._issues = issues                   # list_issues answer (None => unreadable)
        self.added: list[str] = []
        self.removed: list[str] = []
        self.created: list[str] = []
        self.title = None
        self.written_body = None
        self.posted_comment = None

    # reads
    def issue_body(self, repo, issue):
        return self._read_body

    def issue_comments(self, repo, issue):
        if not self._comments_readable:
            return None
        return [Comment(body=b, created_at="") for b in self._comment_bodies]

    def list_issues(self, repo, *, label=None, limit=100):
        return self._issues

    def issue_view(self, repo, issue):
        return self._durability_issue

    # writes
    def create_label(self, repo, name, color, description=""):
        self.created.append(name)
        return True

    def add_label(self, repo, issue, label):
        self.added.append(label)
        return True

    def remove_label(self, repo, issue, label):
        self.removed.append(label)
        return True

    def edit_title(self, repo, issue, title):
        self.title = title
        return True

    def edit_body(self, repo, issue, body):
        self.written_body = body
        return True

    def comment(self, repo, issue, body):
        self.posted_comment = body
        return True

    def wrote_anything(self):
        return bool(self.added or self.removed
                    or self.written_body is not None
                    or self.posted_comment is not None
                    or self.title is not None)


_GH_NAMES = ("issue_body", "issue_comments", "list_issues", "issue_view", "create_label",
             "add_label", "remove_label", "edit_title", "edit_body", "comment")


def _install(monkeypatch, fake):
    for name in _GH_NAMES:
        monkeypatch.setattr(intake_mod.github, name, getattr(fake, name))
    return fake


def test_apply_intake_ready_writes_brief_to_body_and_a_short_comment(monkeypatch):
    fake = _install(monkeypatch, FakeGH(body="original one-liner as filed"))
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\n### Summary\nthe full grounded brief",
                          "area: specific change", Complexity.DEEP, Effort.MEDIUM)
    apply_intake("owner/repo", 16, "old title", [], result)

    assert fake.written_body is not None, "ready routing must edit the issue body"
    assert fake.written_body.startswith("## Agent Brief")
    assert "original one-liner as filed" in fake.written_body and "<details>" in fake.written_body

    assert fake.posted_comment is not None and INTAKE_MARK in fake.posted_comment
    assert "the full grounded brief" not in fake.posted_comment   # not the wall
    assert fake.posted_comment.count("\n") <= 8                    # short


def test_coordinated_ready_projects_title_and_original_from_durable_source(monkeypatch):
    fake = _install(monkeypatch, FakeGH(body="later mutable body"))
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "",
                          Complexity.DEEP, Effort.MEDIUM)

    apply_intake("owner/repo", 16, "later mutable title", [], result,
                 "Filed title", "original as filed")

    assert fake.title == "Filed title"
    assert fake.written_body == compose_ready_body(result.body, "original as filed")


def _durable_issue(result, *, title, body, comment=None):
    return IssueView(
        title=title, body=body, state="OPEN", url="",
        labels=frozenset(intake_labels(result)),
        comments=[Comment(body=comment if comment is not None else intake_mod._READY_COMMENT,
                          created_at="")])


def test_intake_result_must_be_visible_before_its_worktree_is_disposable(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped",
                          Complexity.DEEP, Effort.MEDIUM)
    # The durable body is the canonical composition — the brief over the preserved original.
    issue = _durable_issue(result, title="Scoped",
                           body=compose_ready_body(result.body, "the original as filed"))
    _install(monkeypatch, FakeGH(issue=issue))
    assert intake_result_is_durable("owner/repo", 5, result) is True

    # An unreadable issue (the module returns None) must never read as durable.
    _install(monkeypatch, FakeGH(issue=None))
    assert intake_result_is_durable("owner/repo", 5, result) is False


def test_intake_durability_requires_exact_title_and_routing_labels(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped title",
                          Complexity.DEEP, Effort.MEDIUM)
    issue = IssueView(
        title="Wrong title", body=result.body, state="OPEN", url="",
        labels=frozenset(intake_labels(result))
        | {"agentflow:needs-grilling", "agentflow:effort:high"},
        comments=[Comment(body=intake_mod._READY_COMMENT, created_at="")])
    _install(monkeypatch, FakeGH(issue=issue))
    assert intake_result_is_durable("owner/repo", 5, result) is False


def test_intake_durability_requires_this_routes_exact_comment(monkeypatch):
    result = IntakeResult(IntakeRoute.GRILL,
                          "> *agentflow intake — generated by AI.*\n\nnew question")
    issue = IssueView(title="t", body="", state="OPEN", url="",
                      labels=frozenset({"agentflow:needs-grilling"}),
                      comments=[Comment(body=f"{INTAKE_MARK}\n\nold question", created_at="")])
    _install(monkeypatch, FakeGH(issue=issue))
    assert intake_result_is_durable("owner/repo", 5, result) is False


def test_ready_durability_requires_the_exact_composed_body_not_a_substring(monkeypatch):
    # A body that merely CONTAINS the brief (but is not the canonical composition preserving
    # the original) must not read as durable — only the exact composed body does.
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped",
                          Complexity.DEEP, Effort.MEDIUM)

    substring_only = _durable_issue(result, title="Scoped",
                                    body=f"noise\n{result.body}\nmore noise")
    _install(monkeypatch, FakeGH(issue=substring_only))
    assert intake_result_is_durable("owner/repo", 5, result) is False

    canonical = _durable_issue(result, title="Scoped",
                               body=compose_ready_body(result.body, "original as filed"))
    _install(monkeypatch, FakeGH(issue=canonical))
    assert intake_result_is_durable("owner/repo", 5, result) is True


def test_ready_durability_binds_original_body_and_title_to_the_submission(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "",
                          Complexity.DEEP, Effort.MEDIUM)
    wrong_original = _durable_issue(result, title="Filed title",
                                    body=compose_ready_body(result.body, "different text"))
    _install(monkeypatch, FakeGH(issue=wrong_original))
    assert intake_result_is_durable(
        "owner/repo", 5, result, source_title="Filed title", source_body="as filed") is False

    exact = _durable_issue(result, title="Filed title",
                           body=compose_ready_body(result.body, "as filed"))
    _install(monkeypatch, FakeGH(issue=exact))
    assert intake_result_is_durable(
        "owner/repo", 5, result, source_title="Filed title", source_body="as filed") is True


def test_apply_intake_ready_defers_and_preserves_original_when_body_unreadable(monkeypatch):
    # An unreadable body must fail closed — we cannot compose the brief without the original to
    # preserve, and treating unreadable as "" would clobber the real original text.
    fake = _install(monkeypatch, FakeGH(body=None))
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped",
                          Complexity.DEEP, Effort.MEDIUM)
    summary = apply_intake("owner/repo", 5, "old", [], result)

    assert "deferred" in summary
    assert not fake.wrote_anything(), "nothing may be written when the original is unknown"


def test_ready_projection_rejects_a_malformed_original_envelope(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped",
                          Complexity.DEEP, Effort.MEDIUM)
    malformed = f"old brief\n\n{intake_mod._ORIGINAL_MARK}\noriginal without details"
    fake = _install(monkeypatch, FakeGH(body=malformed))

    assert "deferred" in apply_intake("owner/repo", 5, "old", [], result)
    assert not fake.wrote_anything()


def test_apply_intake_grill_keeps_the_full_comment_and_never_touches_the_body(monkeypatch):
    fake = _install(monkeypatch, FakeGH())
    result = IntakeResult(IntakeRoute.GRILL, "> *agentflow intake — generated by AI.*\n\nwhich did you mean?")
    apply_intake("owner/repo", 7, "t", [], result)

    assert fake.written_body is None, "a hold must not rewrite the body"
    assert "which did you mean?" in fake.posted_comment


# --- no-spam: nothing-new, idempotence, infra failures (issue #23) ----------------

def test_nothing_new_route_parses_without_a_body():
    # A resume that found nothing genuinely open needs no body — it must not fall back to
    # a fail-safe hold just because the body is empty.
    v = parse_intake('{"route": "nothing-new"}')
    assert v.route is IntakeRoute.NOTHING_NEW and v.parsed


def test_apply_intake_nothing_new_writes_absolutely_nothing(monkeypatch):
    fake = _install(monkeypatch, FakeGH())
    apply_intake("owner/repo", 5, "t", ["agentflow:needs-grilling"],
                 IntakeResult(IntakeRoute.NOTHING_NEW, ""))
    assert not fake.wrote_anything(), "a nothing-new recheck must post no comment and touch no labels"


def test_apply_intake_skips_a_re_post_of_the_same_hold(monkeypatch):
    # The exact spam vector: our last word already says this. Re-applying it changes
    # nothing, so it must post no comment and churn no labels.
    question = "> *agentflow intake — generated by AI.*\n\nwhich window did you mean?"
    fake = _install(monkeypatch, FakeGH(comment_bodies=(question,)))
    apply_intake("owner/repo", 5, "t", ["agentflow:needs-grilling"],
                 IntakeResult(IntakeRoute.GRILL, question))
    assert not fake.wrote_anything(), "an identical re-apply must be a no-op"


def test_apply_intake_finishes_partial_labels_without_duplicate_comment(monkeypatch):
    question = "> *agentflow intake — generated by AI.*\n\nwhich window did you mean?"
    fake = _install(monkeypatch, FakeGH(comment_bodies=(question,)))

    apply_intake("owner/repo", 5, "t", [], IntakeResult(IntakeRoute.GRILL, question))

    assert fake.posted_comment is None, "the comment already exists — no duplicate"
    assert fake.added, "the missing label must still be applied"


def test_apply_intake_writes_nothing_when_comment_history_is_unreadable(monkeypatch):
    fake = _install(monkeypatch, FakeGH(comments_readable=False))
    result = IntakeResult(
        IntakeRoute.GRILL,
        "> *agentflow intake — generated by AI.*\n\nwhich window did you mean?",
    )

    assert "deferred" in apply_intake("owner/repo", 5, "t", [], result)
    assert not fake.wrote_anything()


def test_apply_intake_finishes_partial_ready_body_without_duplicate_comment(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "t",
                          Complexity.DEEP, Effort.MEDIUM)
    fake = _install(monkeypatch, FakeGH(comment_bodies=(intake_mod._READY_COMMENT,),
                                        body="original"))

    apply_intake("owner/repo", 5, "t", intake_labels(result), result)

    assert fake.posted_comment is None, "the ready comment already exists — no duplicate"
    assert fake.written_body is not None, "the body still needs the composed brief"


def test_apply_intake_still_posts_a_genuinely_new_question(monkeypatch):
    # Idempotence must not silence a real re-post — a different question still goes out.
    fake = _install(monkeypatch, FakeGH(
        comment_bodies=("> *agentflow intake — generated by AI.*\n\nold question",)))
    apply_intake("owner/repo", 5, "t", [],
                 IntakeResult(IntakeRoute.GRILL, "> *agentflow intake — generated by AI.*\n\na new question"))
    assert fake.posted_comment is not None, "a new question must still post"


# --- dial label cleanup on re-route (issue #27) ----------------------------------

def test_apply_intake_clears_stale_dial_labels_on_reroute(monkeypatch):
    # Before fix: only STATE_LABELS were stripped; old dials accreted.
    # After fix: stale agentflow:complexity:* and agentflow:effort:* are removed too.
    fake = _install(monkeypatch, FakeGH(body="brief v1"))
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief v2\nnew scope",
                          "", Complexity.STANDARD, Effort.LOW)
    apply_intake("owner/repo", 7, "t",
                 ["ready-for-agent", "agentflow:complexity:deep", "agentflow:effort:medium"],
                 result)

    assert "agentflow:complexity:standard" in fake.added
    assert "agentflow:effort:low" in fake.added
    assert "agentflow:complexity:deep" in fake.removed, "stale complexity dial must be stripped"
    assert "agentflow:effort:medium" in fake.removed, "stale effort dial must be stripped"


def test_apply_intake_unchanged_dials_not_removed(monkeypatch):
    # Re-routing with the same dials should not generate spurious removals.
    fake = _install(monkeypatch, FakeGH(body="brief v1"))
    result = IntakeResult(IntakeRoute.READY, "## Brief v2", "", Complexity.DEEP, Effort.MEDIUM)
    apply_intake("owner/repo", 9, "t",
                 ["ready-for-agent", "agentflow:complexity:deep", "agentflow:effort:medium"],
                 result)

    assert "agentflow:complexity:deep" not in fake.removed
    assert "agentflow:effort:medium" not in fake.removed


def test_apply_intake_strips_dials_when_routing_to_hold(monkeypatch):
    # Transitioning from ready to grill should clear dial labels — they belong only on ready.
    fake = _install(monkeypatch, FakeGH())
    result = IntakeResult(IntakeRoute.GRILL, "> *agentflow intake*\n\nwhich did you mean?")
    apply_intake("owner/repo", 5, "t",
                 ["ready-for-agent", "agentflow:complexity:deep", "agentflow:effort:high"],
                 result)

    assert "agentflow:complexity:deep" in fake.removed
    assert "agentflow:effort:high" in fake.removed


# --- legacy label sweep (issue #27) ----------------------------------------------

def test_sweep_migrates_bare_grilling_label(monkeypatch):
    fake = _install(monkeypatch, FakeGH(issues=[
        IssueRow(number=1, title="", body="", labels=frozenset({"needs-grilling"}))]))

    changed = sweep_legacy_labels("owner/repo")

    assert len(changed) == 1 and "#1" in changed[0]
    assert "agentflow:needs-grilling" in changed[0]
    assert "agentflow:needs-grilling" in fake.added
    assert "needs-grilling" in fake.removed


def test_sweep_removes_bare_when_namespaced_already_present(monkeypatch):
    # Issue already has the namespaced form — just drop the bare one, don't re-add.
    fake = _install(monkeypatch, FakeGH(issues=[
        IssueRow(number=2, title="", body="",
                 labels=frozenset({"needs-mockup", "agentflow:needs-mockup"}))]))

    changed = sweep_legacy_labels("owner/repo")

    assert len(changed) == 1
    assert "needs-mockup" in fake.removed
    assert "agentflow:needs-mockup" not in fake.added, "must not re-add a label that's already there"


def test_sweep_leaves_already_correct_and_unrelated_labels_alone(monkeypatch):
    fake = _install(monkeypatch, FakeGH(issues=[
        IssueRow(number=3, title="", body="", labels=frozenset({"agentflow:needs-grilling"})),
        IssueRow(number=4, title="", body="", labels=frozenset({"ready-for-agent", "bug"}))]))

    changed = sweep_legacy_labels("owner/repo")

    assert changed == [], "nothing to change"
    assert fake.added == [] and fake.removed == [], "no edits should be issued"


def test_sweep_ready_for_agent_stays_bare(monkeypatch):
    # ready-for-agent is intentionally bare (ADR 0018) — sweep must not touch it.
    fake = _install(monkeypatch, FakeGH(issues=[
        IssueRow(number=5, title="", body="", labels=frozenset({"ready-for-agent"}))]))

    assert sweep_legacy_labels("owner/repo") == []
    assert fake.added == [] and fake.removed == []


def test_sweep_reports_error_when_the_issue_list_is_unreadable(monkeypatch):
    # Fail closed: an unreadable listing (the module returns None) is an error, not "no issues".
    fake = _install(monkeypatch, FakeGH(issues=None))

    changed = sweep_legacy_labels("owner/repo")

    assert changed and "error" in changed[0].lower()
    assert not fake.wrote_anything()


def test_intake_prompt_carries_the_effort_rubric():
    # ADR 0046: the effort dial needs anchored rungs, not a bare "how much work it warrants"
    # line, plus the two misrating warnings from the backtest's real misses.
    prompt = intake_prompt("owner/repo", {"number": 1, "title": "t", "body": "b"})

    for anchor in ("~70-line diff", "~180 lines", "~390 lines", "1800+ lines"):
        assert anchor in prompt, f"missing effort rubric anchor: {anchor}"
    assert "blast radius" in prompt, "missing the brevity-vs-blast-radius misrating warning"
    assert "doesn't make the change big" in prompt, "missing the scary-name misrating warning"
    assert "builder reasoning depth" in prompt, "missing the over-rating-burns-capacity note"
    # the JSON output-field contract for effort must stay untouched
    assert '"effort": "low" | "medium" | "high" | "extra" — for "ready"; null for a hold' in prompt


def test_intake_prompt_states_the_mockup_scope_contract():
    # ADR 0048: intake must be told to classify scope and emit the mockup_scope field, defaulting
    # to local when unsure.
    prompt = intake_prompt("owner/repo", {"number": 1, "title": "t", "body": "b"})
    assert '"mockup_scope": "local" | "surface"' in prompt
    assert "MOCKUP SCOPE" in prompt
    assert "default" in prompt.lower() and "local" in prompt


def test_pick_resume_copies_the_locked_contract_verbatim_into_the_brief():
    # The pick/resume path (a maintainer reply after a variant round) must tell intake to copy the
    # chosen variant's LOCKED contract VERBATIM into the ready brief and record the committed path,
    # so review stays self-contained after the mockups are archived (ADR 0048).
    prompt = intake_prompt("owner/repo", {"number": 1, "title": "t", "body": "b"},
                           extra="B, but tighter")
    assert "LOCKED" in prompt
    assert "VERBATIM" in prompt or "verbatim" in prompt
    assert "committed mockup path" in prompt.lower() or "exact committed mockup path" in prompt.lower()


def test_intake_prompt_names_the_interface_shape_section_and_its_escape_hatch():
    # issue #382: the ready-route brief must carry an explicit interface-shape expectation
    # (judged by the charter's deep-module/deletion test), never silently omitted.
    prompt = intake_prompt("owner/repo", {"number": 1, "title": "t", "body": "b"})
    assert "**Interface shape**" in prompt
    assert "depth test" in prompt
    assert "purely internal" in prompt
