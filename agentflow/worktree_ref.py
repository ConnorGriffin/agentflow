"""One value type owns the per-session worktree-path layout (ADR 0041).

Every pipeline session's checkout lives at a path that encodes a
``(workdir, tool, kind, issue-or-PR number, slug)`` tuple::

    {workdir}/.agentflow/worktrees/{lane}/{name}

where ``lane`` is ``{tool}``, ``{tool}-review``, ``{tool}-intake``, or ``{tool}-audit``, ``name`` is
``issue-{n}-{slug}``, ``mockup-{n}-{slug}``, ``pr-{pr}-{slug}``, ``issue-{n}``,
``research-{n}``, or ``ask-{token}``, and the branch is ``agentflow/{lane}/{name}``.

:class:`WorktreeRef` is the single owner of that convention. It reads a path apart
(:meth:`WorktreeRef.parse`) and builds one up (the ``for_*`` constructors), so the two
directions cannot drift — a round-trip (``parse(ref.path) == ref``) pins the whole layout
in one place. Parse owns only the *shape* rule: a malformed or unrecognized path reads as
``None`` (the fail-closed convention every current site uses), never a raise. Whether a
path's tool or lane agrees with a particular record's expectations is a second judgment —
the *type* never takes a record; the record-shaped derivations at the foot of this module
do, and they are the one place that judgment lives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_MARKER = "/.agentflow/worktrees/"


class WorktreeKind(str, Enum):
    """The kind of work a checkout holds — the layout's typed discriminator."""

    BUILD = "build"
    REVISE = "revise"
    REVIEW = "review"
    MOCKUP = "mockup"
    INTAKE = "intake"
    RESEARCH = "research"
    CONVERSE = "converse"
    AUDIT = "audit"


@dataclass(frozen=True)
class _Shape:
    """How one kind's ``name`` reads apart and builds up — the single per-kind rule."""

    lane_suffix: str  # "", "-review", or "-intake"
    head: str  # the name's leading token: "issue", "pr", "mockup", ...
    pattern: re.Pattern  # parses a name into ("num"?, "slug"?) groups
    has_number: bool
    has_slug: bool

    def render(self, number: int | None, slug: str) -> str:
        """The ``name`` segment for these parts."""
        if self.has_number and self.has_slug:
            return f"{self.head}-{number}-{slug}"
        if self.has_number:
            return f"{self.head}-{number}"
        return f"{self.head}-{slug}"


# Build's and revise's on-disk shape are identical — a revise reuses the builder's
# retained worktree — so only build appears here; revise round-trips through it.
_SHAPES: dict[WorktreeKind, _Shape] = {
    WorktreeKind.BUILD: _Shape("", "issue", re.compile(r"^issue-(?P<num>\d+)-(?P<slug>.+)$"), True, True),
    WorktreeKind.MOCKUP: _Shape("", "mockup", re.compile(r"^mockup-(?P<num>\d+)-(?P<slug>.+)$"), True, True),
    WorktreeKind.RESEARCH: _Shape("", "research", re.compile(r"^research-(?P<num>\d+)$"), True, False),
    WorktreeKind.CONVERSE: _Shape("", "ask", re.compile(r"^ask-(?P<slug>.+)$"), False, True),
    WorktreeKind.REVIEW: _Shape("-review", "pr", re.compile(r"^pr-(?P<num>\d+)-(?P<slug>.+)$"), True, True),
    WorktreeKind.INTAKE: _Shape("-intake", "issue", re.compile(r"^issue-(?P<num>\d+)$"), True, False),
    # The plan audit reads the same issue intake just triaged, so it needs its own reserved lane
    # suffix: without one the two checkouts of the same issue would render the same path and each
    # would be mistakable for the other (ADR 380). It comes last so a tool whose own name ends in
    # `-audit` still has its build recognized as a build, exactly as `_lane_tool` describes.
    WorktreeKind.AUDIT: _Shape("-audit", "issue", re.compile(r"^issue-(?P<num>\d+)$"), True, False),
}


@dataclass(frozen=True)
class WorktreeRef:
    """A per-session checkout path decomposed into its typed parts (ADR 0041).

    ``number`` is the issue or PR number (``None`` for a conversation, which has no number);
    ``slug`` is the trailing descriptor (empty for an intake or research checkout, and the
    conversation token for a converse checkout).
    """

    workdir: str
    tool: str
    kind: WorktreeKind
    number: int | None
    slug: str

    # --- build up -----------------------------------------------------------------------

    @classmethod
    def for_build(cls, workdir: str, tool: str, number: int, slug: str) -> "WorktreeRef":
        return cls(workdir, tool, WorktreeKind.BUILD, number, slug)

    @classmethod
    def for_revise(cls, workdir: str, tool: str, number: int, slug: str) -> "WorktreeRef":
        """A revise reuses the builder's retained build worktree, so its ref *is* a build
        ref — the review→build sibling derivation is ``for_build`` from the parsed review
        ref plus the record's builder lineage (ADR 0041)."""
        return cls.for_build(workdir, tool, number, slug)

    @classmethod
    def for_mockup(cls, workdir: str, tool: str, number: int, slug: str) -> "WorktreeRef":
        return cls(workdir, tool, WorktreeKind.MOCKUP, number, slug)

    @classmethod
    def for_review(cls, workdir: str, tool: str, pr_number: int, slug: str) -> "WorktreeRef":
        return cls(workdir, tool, WorktreeKind.REVIEW, pr_number, slug)

    @classmethod
    def for_intake(cls, workdir: str, tool: str, number: int) -> "WorktreeRef":
        return cls(workdir, tool, WorktreeKind.INTAKE, number, "")

    @classmethod
    def for_plan_audit(cls, workdir: str, tool: str, number: int) -> "WorktreeRef":
        return cls(workdir, tool, WorktreeKind.AUDIT, number, "")

    @classmethod
    def for_research(cls, workdir: str, tool: str, number: int) -> "WorktreeRef":
        return cls(workdir, tool, WorktreeKind.RESEARCH, number, "")

    @classmethod
    def for_converse(cls, workdir: str, tool: str, token: str) -> "WorktreeRef":
        return cls(workdir, tool, WorktreeKind.CONVERSE, None, token)

    # --- read apart ---------------------------------------------------------------------

    @classmethod
    def parse(cls, source: str | None) -> "WorktreeRef | None":
        """The parts behind a checkout path, or ``None`` if it is not a well-formed worktree
        path. Judges *shape* only; never raises."""
        if not source or _MARKER not in source:
            return None
        workdir, tail = source.split(_MARKER, 1)
        parts = tail.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        lane, name = parts
        for kind, shape in _SHAPES.items():
            tool = _lane_tool(lane, shape.lane_suffix)
            if tool is None:
                continue
            match = shape.pattern.match(name)
            if match is None:
                continue
            number = int(match.group("num")) if shape.has_number else None
            slug = match.group("slug") if shape.has_slug else ""
            return cls(workdir, tool, kind, number, slug)
        return None

    # --- render -------------------------------------------------------------------------

    @property
    def lane(self) -> str:
        return f"{self.tool}{_SHAPES[self._shape_kind].lane_suffix}"

    @property
    def name(self) -> str:
        return _SHAPES[self._shape_kind].render(self.number, self.slug)

    @property
    def path(self) -> str:
        return str(Path(self.workdir) / ".agentflow" / "worktrees" / self.lane / self.name)

    @property
    def branch(self) -> str:
        return f"agentflow/{self.lane}/{self.name}"

    @property
    def _shape_kind(self) -> WorktreeKind:
        # Revise shares build's on-disk shape.
        return WorktreeKind.BUILD if self.kind is WorktreeKind.REVISE else self.kind


def _lane_tool(lane: str, suffix: str) -> str | None:
    """The tool a lane names once its kind's reserved suffix is stripped, or ``None`` if the
    lane does not carry that suffix (or leaves no tool). The *name* — not the lane — fixes the
    kind (only a ``pr-…`` name is a review, only a slug-less ``issue-…`` name is an intake), so
    a plain lane is read as the whole tool even when that tool's own name ends in ``-review`` or
    ``-intake``. That keeps such a tool's build representable instead of mistaking it for a
    stripped review/intake lane."""
    if suffix:
        if not lane.endswith(suffix):
            return None
        lane = lane[: -len(suffix)]
    return lane or None


# --- the branch form, read apart ---------------------------------------------------------
# A branch names the same ``{lane}/{name}`` convention the paths above render, minus the
# workdir a path carries — so it reads apart here rather than through :class:`WorktreeRef`,
# which cannot be built without one.

_BRANCH_ISSUE_RE = re.compile(r"^agentflow/[^/]+/issue-(\d+)-")
BUILD_BRANCH_RE = re.compile(r"^agentflow/([^/]+)/issue-(\d+)-(.+)$")


def issue_of_branch(branch: str) -> int | None:
    """The issue number an agentflow PR branch is working, or None. Pure — the signal
    that an agent already owns an issue (dispatch dedup, beyond ADR 0009's merge floor)."""
    m = _BRANCH_ISSUE_RE.match(branch or "")
    return int(m.group(1)) if m else None


def slug(title: str) -> str:
    """The trailing descriptor an issue's title contributes to its branch and worktree name."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40].strip("-") or "issue"


# --- a stage record's own checkout --------------------------------------------------------
# The layout above says how a path reads apart; these two say whether the path a *record*
# carries is the checkout that record is entitled to. Build, Revise, Respond, Mockup, Review
# and the park handoff all ask one of these questions, so the answer lives with the layout
# rather than in whichever stage module happened to hold it.

def source_facts(record):
    """The ``(workdir, branch, path)`` of the branch-owning checkout a Build, Revise, Respond
    or Mockup record owns, or ``None`` when the source is not that record's own checkout —
    a foreign tool, a lineage the pool no longer matches, or the wrong kind of path."""
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.tool != record.pool or record.lineage != record.pool:
        return None
    if record.stage == "mockup":
        if ref.kind is not WorktreeKind.MOCKUP or str(ref.number) != str(record.subject):
            return None
    elif ref.kind is not WorktreeKind.BUILD:
        return None
    return ref.workdir, ref.branch, Path(record.source)


def review_source_facts(record):
    """The ``(workdir, pr_number)`` a review worktree encodes, or ``None``. The review source is
    ``.../<tool>-review/pr-<pr>-<slug>``, so the PR number is recoverable for the park handoff
    without a second durable field."""
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.kind is not WorktreeKind.REVIEW:
        return None
    return ref.workdir, ref.number
