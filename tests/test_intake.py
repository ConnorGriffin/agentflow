"""Test intake through its interface — the pure, fail-safe decision parser and the
label mapping. Like the reviewer: anything we cannot read as a confident build-ready
decision must fall back to holding for a human, never an accidental `ready`.
"""

import json
from types import SimpleNamespace

import pytest

from agentflow import intake as intake_mod
from agentflow.intake import (INTAKE_MARK, IntakeResult, IntakeRoute, apply_intake,
                              awaiting_recheck, compose_ready_body, intake_labels,
                              intake_prompt, intake_result_is_durable, parse_intake,
                              replies_since_intake, sweep_legacy_labels)
from agentflow.runner import Complexity, Effort


def test_ready_with_all_fields_is_build_ready():
    v = parse_intake('{"route": "ready", "title": "ISF: widen the measurement window", '
                     '"complexity": "standard", "effort": "high", "body": "## Agent Brief\\n..."}')
    assert v.route is IntakeRoute.READY and v.parsed
    assert v.complexity is Complexity.STANDARD and v.effort is Effort.HIGH
    assert v.title == "ISF: widen the measurement window" and v.body.startswith("## Agent Brief")


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
    assert intake_labels(parse_intake('{"route": "mockup", "body": "m"}')) == ["agentflow:needs-mockup"]


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


class _GhRecorder:
    """Capture the gh commands apply_intake runs, and answer its body fetch."""

    def __init__(self, current_body=""):
        self.calls = []
        self.current_body = current_body

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        if "issue" in cmd and "view" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"body": self.current_body}))
        return SimpleNamespace(returncode=0, stdout="")

    def _edit_body(self):
        for c in self.calls:
            if "edit" in c and "--body" in c:
                return c[c.index("--body") + 1]
        return None

    def _edit_title(self):
        for c in self.calls:
            if "edit" in c and "--title" in c:
                return c[c.index("--title") + 1]
        return None

    def _comment(self):
        for c in self.calls:
            if "comment" in c and "--body" in c:
                return c[c.index("--body") + 1]
        return None


def test_apply_intake_ready_writes_brief_to_body_and_a_short_comment(monkeypatch):
    rec = _GhRecorder(current_body="original one-liner as filed")
    monkeypatch.setattr(intake_mod, "_run", rec)
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\n### Summary\nthe full grounded brief",
                          "area: specific change", Complexity.DEEP, Effort.MEDIUM)
    apply_intake("owner/repo", 16, "old title", [], result)

    body = rec._edit_body()
    assert body is not None, "ready routing must edit the issue body"
    assert body.startswith("## Agent Brief")
    assert "original one-liner as filed" in body and "<details>" in body

    comment = rec._comment()
    assert comment is not None and INTAKE_MARK in comment
    assert "the full grounded brief" not in comment          # not the wall
    assert comment.count("\n") <= 8                            # short


def test_coordinated_ready_projects_title_and_original_from_durable_source(monkeypatch):
    rec = _GhRecorder(current_body="later mutable body")
    monkeypatch.setattr(intake_mod, "_run", rec)
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "",
                          Complexity.DEEP, Effort.MEDIUM)

    apply_intake("owner/repo", 16, "later mutable title", [], result,
                 "Filed title", "original as filed")

    assert rec._edit_title() == "Filed title"
    assert rec._edit_body() == compose_ready_body(result.body, "original as filed")


def test_intake_result_must_be_visible_before_its_worktree_is_disposable(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped",
                          Complexity.DEEP, Effort.MEDIUM)
    # The durable body is the canonical composition — the brief over the preserved original.
    issue = {"title": "Scoped",
             "body": compose_ready_body(result.body, "the original as filed"),
             "labels": [{"name": name} for name in intake_labels(result)],
             "comments": [{"body": intake_mod._READY_COMMENT}]}
    monkeypatch.setattr(intake_mod, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(issue)))
    assert intake_result_is_durable("owner/repo", 5, result) is True

    monkeypatch.setattr(intake_mod, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert intake_result_is_durable("owner/repo", 5, result) is False


def test_intake_durability_requires_exact_title_and_routing_labels(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped title",
                          Complexity.DEEP, Effort.MEDIUM)
    issue = {
        "title": "Wrong title",
        "body": result.body,
        "labels": ([{"name": name} for name in intake_labels(result)]
                   + [{"name": "agentflow:needs-grilling"},
                      {"name": "agentflow:effort:high"}]),
        "comments": [{"body": intake_mod._READY_COMMENT}],
    }
    monkeypatch.setattr(intake_mod, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(issue)))

    assert intake_result_is_durable("owner/repo", 5, result) is False


def test_intake_durability_requires_this_routes_exact_comment(monkeypatch):
    result = IntakeResult(IntakeRoute.GRILL,
                          "> *agentflow intake — generated by AI.*\n\nnew question")
    issue = {"title": "t", "body": "", "labels": [{"name": "agentflow:needs-grilling"}],
             "comments": [{"body": f"{INTAKE_MARK}\n\nold question"}]}
    monkeypatch.setattr(intake_mod, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(issue)))

    assert intake_result_is_durable("owner/repo", 5, result) is False


def test_ready_durability_requires_the_exact_composed_body_not_a_substring(monkeypatch):
    # A body that merely CONTAINS the brief (but is not the canonical composition preserving
    # the original) must not read as durable — only the exact composed body does.
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped",
                          Complexity.DEEP, Effort.MEDIUM)
    base = {"title": "Scoped",
            "labels": [{"name": name} for name in intake_labels(result)],
            "comments": [{"body": intake_mod._READY_COMMENT}]}

    substring_only = dict(base, body=f"noise\n{result.body}\nmore noise")  # brief present, not canonical
    monkeypatch.setattr(intake_mod, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(substring_only)))
    assert intake_result_is_durable("owner/repo", 5, result) is False

    canonical = dict(base, body=compose_ready_body(result.body, "original as filed"))
    monkeypatch.setattr(intake_mod, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(canonical)))
    assert intake_result_is_durable("owner/repo", 5, result) is True


def test_ready_durability_binds_original_body_and_title_to_the_submission(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "",
                          Complexity.DEEP, Effort.MEDIUM)
    base = {"title": "Filed title",
            "labels": [{"name": name} for name in intake_labels(result)],
            "comments": [{"body": intake_mod._READY_COMMENT}]}
    wrong_original = dict(base, body=compose_ready_body(result.body, "different text"))
    monkeypatch.setattr(intake_mod, "_run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(wrong_original)))
    assert intake_result_is_durable(
        "owner/repo", 5, result, source_title="Filed title", source_body="as filed") is False

    exact = dict(base, body=compose_ready_body(result.body, "as filed"))
    monkeypatch.setattr(intake_mod, "_run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(exact)))
    assert intake_result_is_durable(
        "owner/repo", 5, result, source_title="Filed title", source_body="as filed") is True


def test_apply_intake_ready_defers_and_preserves_original_when_body_unreadable(monkeypatch):
    # An unreadable body must fail closed — we cannot compose the brief without the original to
    # preserve, and treating unreadable as "" would clobber the real original text.
    calls = []

    def gh(cmd, cwd=None):
        calls.append(cmd)
        if "view" in cmd and "comments" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"comments": []}))
        if "view" in cmd and "body" in cmd:
            return SimpleNamespace(returncode=1, stdout="")  # body read fails
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(intake_mod, "_run", gh)
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped",
                          Complexity.DEEP, Effort.MEDIUM)
    summary = apply_intake("owner/repo", 5, "old", [], result)

    assert "deferred" in summary
    # Nothing was written: no body edit (which would clobber the original), no labels, no comment.
    assert not any("edit" in c or "comment" in c and "view" not in c for c in calls)


def test_ready_projection_rejects_a_malformed_original_envelope(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "Scoped",
                          Complexity.DEEP, Effort.MEDIUM)
    malformed = f"old brief\n\n{intake_mod._ORIGINAL_MARK}\noriginal without details"
    calls = []

    def gh(cmd, cwd=None):
        calls.append(cmd)
        if "view" in cmd and "comments" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"comments": []}))
        if "view" in cmd and "body" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"body": malformed}))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(intake_mod, "_run", gh)
    assert "deferred" in apply_intake("owner/repo", 5, "old", [], result)
    assert not any("edit" in cmd or "comment" in cmd and "view" not in cmd for cmd in calls)


def test_apply_intake_grill_keeps_the_full_comment_and_never_touches_the_body(monkeypatch):
    rec = _GhRecorder()
    monkeypatch.setattr(intake_mod, "_run", rec)
    result = IntakeResult(IntakeRoute.GRILL, "> *agentflow intake — generated by AI.*\n\nwhich did you mean?")
    apply_intake("owner/repo", 7, "t", [], result)

    assert rec._edit_body() is None, "a hold must not rewrite the body"
    assert "which did you mean?" in rec._comment()


# --- no-spam: nothing-new, idempotence, infra failures (issue #23) ----------------

def test_nothing_new_route_parses_without_a_body():
    # A resume that found nothing genuinely open needs no body — it must not fall back to
    # a fail-safe hold just because the body is empty.
    v = parse_intake('{"route": "nothing-new"}')
    assert v.route is IntakeRoute.NOTHING_NEW and v.parsed


class _GhSpy:
    """Records every gh command and answers the comments/body fetches with a canned tail."""

    def __init__(self, latest_comment="", current_body=""):
        self.calls = []
        self.latest_comment = latest_comment
        self.current_body = current_body

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        if "view" in cmd and "comments" in cmd:
            return SimpleNamespace(returncode=0,
                                   stdout=json.dumps({"comments": [{"body": self.latest_comment}]}))
        if "view" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"body": self.current_body}))
        return SimpleNamespace(returncode=0, stdout="")

    def wrote_anything(self):
        return any(("comment" in c) or ("--add-label" in c) or ("--remove-label" in c)
                   or ("--body" in c) or ("--title" in c) for c in self.calls)


def test_apply_intake_nothing_new_writes_absolutely_nothing(monkeypatch):
    spy = _GhSpy()
    monkeypatch.setattr(intake_mod, "_run", spy)
    apply_intake("owner/repo", 5, "t", ["agentflow:needs-grilling"],
                 IntakeResult(IntakeRoute.NOTHING_NEW, ""))
    assert not spy.wrote_anything(), "a nothing-new recheck must post no comment and touch no labels"


def test_apply_intake_skips_a_re_post_of_the_same_hold(monkeypatch):
    # The exact spam vector: our last word already says this. Re-applying it changes
    # nothing, so it must post no comment and churn no labels.
    question = "> *agentflow intake — generated by AI.*\n\nwhich window did you mean?"
    spy = _GhSpy(latest_comment=question)
    monkeypatch.setattr(intake_mod, "_run", spy)
    apply_intake("owner/repo", 5, "t", ["agentflow:needs-grilling"],
                 IntakeResult(IntakeRoute.GRILL, question))
    assert not spy.wrote_anything(), "an identical re-apply must be a no-op"


def test_apply_intake_finishes_partial_labels_without_duplicate_comment(monkeypatch):
    question = "> *agentflow intake — generated by AI.*\n\nwhich window did you mean?"
    spy = _GhSpy(latest_comment=question)
    monkeypatch.setattr(intake_mod, "_run", spy)

    apply_intake("owner/repo", 5, "t", [], IntakeResult(IntakeRoute.GRILL, question))

    assert not any("comment" in c and "view" not in c for c in spy.calls)
    assert any("--add-label" in c for c in spy.calls)


def test_apply_intake_writes_nothing_when_comment_history_is_unreadable(monkeypatch):
    calls = []

    def gh(cmd, cwd=None):
        calls.append(cmd)
        if "view" in cmd and "comments" in cmd:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(intake_mod, "_run", gh)
    result = IntakeResult(
        IntakeRoute.GRILL,
        "> *agentflow intake — generated by AI.*\n\nwhich window did you mean?",
    )

    assert "deferred" in apply_intake("owner/repo", 5, "t", [], result)
    assert not any(("comment" in cmd and "view" not in cmd) or "edit" in cmd for cmd in calls)


def test_apply_intake_finishes_partial_ready_body_without_duplicate_comment(monkeypatch):
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief\nship it", "t",
                          Complexity.DEEP, Effort.MEDIUM)
    spy = _GhSpy(latest_comment=intake_mod._READY_COMMENT, current_body="original")
    monkeypatch.setattr(intake_mod, "_run", spy)

    apply_intake("owner/repo", 5, "t", intake_labels(result), result)

    assert not any("comment" in c and "view" not in c for c in spy.calls)
    assert any("--body" in c for c in spy.calls)


def test_apply_intake_still_posts_a_genuinely_new_question(monkeypatch):
    # Idempotence must not silence a real re-post — a different question still goes out.
    spy = _GhSpy(latest_comment="> *agentflow intake — generated by AI.*\n\nold question")
    monkeypatch.setattr(intake_mod, "_run", spy)
    apply_intake("owner/repo", 5, "t", [],
                 IntakeResult(IntakeRoute.GRILL, "> *agentflow intake — generated by AI.*\n\na new question"))
    assert any("comment" in c for c in spy.calls), "a new question must still post"


# --- dial label cleanup on re-route (issue #27) ----------------------------------

def _label_edit_cmd(rec: _GhRecorder) -> list[str]:
    """The gh issue edit call that sets labels (has --add-label or just removes)."""
    for c in rec.calls:
        if "edit" in c and ("--add-label" in c or "--remove-label" in c) and "--body" not in c:
            return c
    return []


def test_apply_intake_clears_stale_dial_labels_on_reroute(monkeypatch):
    # Before fix: only STATE_LABELS were stripped; old dials accreted.
    # After fix: stale agentflow:complexity:* and agentflow:effort:* are removed too.
    rec = _GhRecorder(current_body="brief v1")
    monkeypatch.setattr(intake_mod, "_run", rec)
    result = IntakeResult(IntakeRoute.READY, "## Agent Brief v2\nnew scope",
                          "", Complexity.STANDARD, Effort.LOW)
    apply_intake("owner/repo", 7, "t",
                 ["ready-for-agent", "agentflow:complexity:deep", "agentflow:effort:medium"],
                 result)

    cmd = _label_edit_cmd(rec)
    assert cmd, "should have a label edit command"
    adds = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-label"]
    removes = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--remove-label"]

    assert "agentflow:complexity:standard" in adds
    assert "agentflow:effort:low" in adds
    assert "agentflow:complexity:deep" in removes, "stale complexity dial must be stripped"
    assert "agentflow:effort:medium" in removes, "stale effort dial must be stripped"


def test_apply_intake_unchanged_dials_not_removed(monkeypatch):
    # Re-routing with the same dials should not generate spurious --remove-label calls.
    rec = _GhRecorder(current_body="brief v1")
    monkeypatch.setattr(intake_mod, "_run", rec)
    result = IntakeResult(IntakeRoute.READY, "## Brief v2", "", Complexity.DEEP, Effort.MEDIUM)
    apply_intake("owner/repo", 9, "t",
                 ["ready-for-agent", "agentflow:complexity:deep", "agentflow:effort:medium"],
                 result)

    cmd = _label_edit_cmd(rec)
    removes = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--remove-label"]
    assert "agentflow:complexity:deep" not in removes
    assert "agentflow:effort:medium" not in removes


def test_apply_intake_strips_dials_when_routing_to_hold(monkeypatch):
    # Transitioning from ready to grill should clear dial labels — they belong only on ready.
    rec = _GhRecorder()
    monkeypatch.setattr(intake_mod, "_run", rec)
    result = IntakeResult(IntakeRoute.GRILL, "> *agentflow intake*\n\nwhich did you mean?")
    apply_intake("owner/repo", 5, "t",
                 ["ready-for-agent", "agentflow:complexity:deep", "agentflow:effort:high"],
                 result)

    cmd = _label_edit_cmd(rec)
    removes = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--remove-label"]
    assert "agentflow:complexity:deep" in removes
    assert "agentflow:effort:high" in removes


# --- legacy label sweep (issue #27) ----------------------------------------------

def _fake_run_for_sweep(issues_payload: str):
    """Return a fake _run that serves the given payload for list calls."""
    calls: list[list[str]] = []

    def _run(cmd, cwd=None):
        calls.append(cmd)
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=issues_payload)
        return SimpleNamespace(returncode=0, stdout="")

    return _run, calls


def test_sweep_migrates_bare_grilling_label(monkeypatch):
    issues = [{"number": 1, "labels": [{"name": "needs-grilling"}]}]
    fake_run, calls = _fake_run_for_sweep(json.dumps(issues))
    monkeypatch.setattr(intake_mod, "_run", fake_run)

    changed = sweep_legacy_labels("owner/repo")

    assert len(changed) == 1 and "#1" in changed[0]
    assert "agentflow:needs-grilling" in changed[0]
    edit_calls = [c for c in calls if "edit" in c]
    assert any("--add-label" in c and "agentflow:needs-grilling" in c for c in edit_calls)
    assert any("--remove-label" in c and "needs-grilling" in c for c in edit_calls)


def test_sweep_removes_bare_when_namespaced_already_present(monkeypatch):
    # Issue already has the namespaced form — just drop the bare one, don't re-add.
    issues = [{"number": 2, "labels": [{"name": "needs-mockup"},
                                        {"name": "agentflow:needs-mockup"}]}]
    fake_run, calls = _fake_run_for_sweep(json.dumps(issues))
    monkeypatch.setattr(intake_mod, "_run", fake_run)

    changed = sweep_legacy_labels("owner/repo")

    assert len(changed) == 1
    cmd = next(c for c in calls if "edit" in c)
    assert "--remove-label" in cmd and "needs-mockup" in cmd
    assert "--add-label" not in cmd, "should not re-add the namespaced label that's already there"


def test_sweep_leaves_already_correct_and_unrelated_labels_alone(monkeypatch):
    issues = [
        {"number": 3, "labels": [{"name": "agentflow:needs-grilling"}]},  # already namespaced
        {"number": 4, "labels": [{"name": "ready-for-agent"}, {"name": "bug"}]},  # unrelated
    ]
    fake_run, calls = _fake_run_for_sweep(json.dumps(issues))
    monkeypatch.setattr(intake_mod, "_run", fake_run)

    changed = sweep_legacy_labels("owner/repo")

    assert changed == [], "nothing to change"
    edit_calls = [c for c in calls if "edit" in c]
    assert edit_calls == [], "no edits should be issued"


def test_sweep_ready_for_agent_stays_bare(monkeypatch):
    # ready-for-agent is intentionally bare (ADR 0018) — sweep must not touch it.
    issues = [{"number": 5, "labels": [{"name": "ready-for-agent"}]}]
    fake_run, calls = _fake_run_for_sweep(json.dumps(issues))
    monkeypatch.setattr(intake_mod, "_run", fake_run)

    changed = sweep_legacy_labels("owner/repo")
    assert changed == []


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
