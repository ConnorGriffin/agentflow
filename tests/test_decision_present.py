"""Regression tests for the decision_present idempotency guard (issue #192).

decision_present must recognise only the breadcrumb entry this resolution
writes, not any incidental #N cross-reference elsewhere in 'Decisions so far'.
"""

from agentflow.coordinated_research import (
    ResearchDisposition,
    decision_line,
    decision_present,
    with_decision,
)


NO_BUILD = ResearchDisposition(
    kind="no_build",
    summary="The existing router already covers the widget path.",
)


def test_incidental_cross_reference_does_not_suppress_breadcrumb():
    """An unrelated mention of #5 in 'Decisions so far' (e.g. 'supersedes #5') must not
    cause decision_present to return True — only the actual written breadcrumb line counts."""
    body = (
        "# Map\n\n## Decisions so far\n\n"
        "- **Some other decision** — supersedes #5, resolved by unattended research (#99).\n"
    )
    assert not decision_present(body, 5)
    updated = with_decision(body, decision_line("Audit the widget path", 5, NO_BUILD))
    assert decision_present(updated, 5)


def test_own_written_line_is_recognised_as_replay():
    """A true replay — the ticket's own breadcrumb already written — is still detected so
    the caller's guard can skip the append."""
    line = decision_line("Audit the widget path", 5, NO_BUILD)
    body = f"# Map\n\n## Decisions so far\n\n{line}\n"
    assert decision_present(body, 5)
