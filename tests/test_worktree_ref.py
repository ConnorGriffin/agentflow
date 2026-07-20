"""Tests for the worktree-path value type (ADR 0041).

Everything here goes through :class:`WorktreeRef`'s own interface — the ``for_*``
constructors, :attr:`~WorktreeRef.path`, :attr:`~WorktreeRef.branch`, and
:meth:`~WorktreeRef.parse` — never the private shape table.
"""

import pytest

from agentflow.worktree_ref import WorktreeKind, WorktreeRef

WORKDIR = "/srv/agentflow"

# One ref per session kind, built through its named constructor.
REFS = {
    "build": WorktreeRef.for_build(WORKDIR, "claude", 249, "worktree-ref"),
    "revise": WorktreeRef.for_revise(WORKDIR, "codex", 249, "worktree-ref"),
    "mockup": WorktreeRef.for_mockup(WORKDIR, "claude", 218, "new-panel"),
    "review": WorktreeRef.for_review(WORKDIR, "codex", 220, "conflict-revise"),
    "intake": WorktreeRef.for_intake(WORKDIR, "claude", 42),
    "research": WorktreeRef.for_research(WORKDIR, "codex", 7),
    "converse": WorktreeRef.for_converse(WORKDIR, "claude", "abcd1234"),
}


@pytest.mark.parametrize("ref", REFS.values(), ids=list(REFS))
def test_round_trip_path(ref):
    """Build → read-apart → the same parts, for every session kind. This is the property
    that keeps construction and parsing from drifting (ADR 0041)."""
    assert WorktreeRef.parse(ref.path) == ref


def test_path_and_branch_layout():
    """The rendered path and branch match the on-disk convention callers still hand-roll."""
    build = REFS["build"]
    assert build.path == f"{WORKDIR}/.agentflow/worktrees/claude/issue-249-worktree-ref"
    assert build.branch == "agentflow/claude/issue-249-worktree-ref"

    review = REFS["review"]
    assert review.path == f"{WORKDIR}/.agentflow/worktrees/codex-review/pr-220-conflict-revise"
    assert review.branch == "agentflow/codex-review/pr-220-conflict-revise"

    intake = REFS["intake"]
    assert intake.path == f"{WORKDIR}/.agentflow/worktrees/claude-intake/issue-42"
    assert intake.branch == "agentflow/claude-intake/issue-42"

    research = REFS["research"]
    assert research.path == f"{WORKDIR}/.agentflow/worktrees/codex/research-7"

    converse = REFS["converse"]
    assert converse.path == f"{WORKDIR}/.agentflow/worktrees/claude/ask-abcd1234"


def test_parse_exposes_typed_parts():
    ref = WorktreeRef.parse(f"{WORKDIR}/.agentflow/worktrees/codex-review/pr-220-conflict-revise")
    assert ref is not None
    assert ref.workdir == WORKDIR
    assert ref.tool == "codex"
    assert ref.kind is WorktreeKind.REVIEW
    assert ref.number == 220
    assert ref.slug == "conflict-revise"


def test_revise_reuses_the_build_worktree():
    """A revise's checkout is the builder's retained build worktree, so it parses back as a
    build ref — the sibling derivation the ADR calls for."""
    revise = WorktreeRef.for_revise(WORKDIR, "codex", 249, "worktree-ref")
    assert revise == WorktreeRef.for_build(WORKDIR, "codex", 249, "worktree-ref")
    assert WorktreeRef.parse(revise.path).kind is WorktreeKind.BUILD


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "/srv/agentflow/src/main.py",  # no marker
        "/srv/agentflow/.agentflow/worktrees/claude",  # no name segment
        "/srv/agentflow/.agentflow/worktrees/claude/",  # empty name
        "/srv/agentflow/.agentflow/worktrees//issue-1-x",  # empty lane
        "/srv/agentflow/.agentflow/worktrees/claude/issue-1-x/extra",  # too deep
        "/srv/agentflow/.agentflow/worktrees/claude/issue-abc-x",  # non-numeric number
        "/srv/agentflow/.agentflow/worktrees/claude/issue-5",  # build name without a slug
        "/srv/agentflow/.agentflow/worktrees/claude/issue-5-",  # build name with empty slug
        "/srv/agentflow/.agentflow/worktrees/claude-review/issue-5-x",  # review lane, build name
        "/srv/agentflow/.agentflow/worktrees/claude/pr-5-x",  # pr name off a review lane
        "/srv/agentflow/.agentflow/worktrees/claude-intake/issue-5-x",  # intake lane, slugged name
        "/srv/agentflow/.agentflow/worktrees/-review/pr-5-x",  # lane leaves no tool
        "/srv/agentflow/.agentflow/worktrees/claude/mystery-5-x",  # unknown name head
    ],
)
def test_malformed_reads_as_nothing(bad):
    """A malformed or unrecognized path reads as ``None`` — fail-closed, never a raise."""
    assert WorktreeRef.parse(bad) is None
