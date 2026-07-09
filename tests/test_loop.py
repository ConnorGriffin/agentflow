"""Pure helpers of the M0 loop. The live orchestration (build/review/merge) is
proven by the first live run; these are the parsing bits that must be exact."""

import pytest

from agentflow.loop import pr_number, repo_profile, slug, tier_from_labels
from agentflow.runner import Tier


def test_tier_from_labels_reads_the_tier_label():
    assert tier_from_labels(["ready-for-agent", "tier:light"]) is Tier.LIGHT
    assert tier_from_labels(["tier:standard"]) is Tier.STANDARD
    assert tier_from_labels(["tier:deep"]) is Tier.DEEP


def test_tier_from_labels_is_none_without_one():
    # ADR 0014 hard gate: no tier label => the loop must skip, not guess.
    assert tier_from_labels(["ready-for-agent", "bug"]) is None
    assert tier_from_labels([]) is None


def test_tier_from_labels_ignores_lookalikes():
    assert tier_from_labels(["tier:xl", "tiering"]) is None


@pytest.mark.parametrize("title,expected", [
    ("Add a slugify(text) helper", "add-a-slugify-text-helper"),
    ("  Foo__Bar!!  ", "foo-bar"),
    ("", "issue"),
    ("!!!", "issue"),
])
def test_slug(title, expected):
    assert slug(title) == expected


def test_slug_truncates_to_40():
    assert len(slug("word " * 40)) <= 40


def test_pr_number_from_url():
    assert pr_number("https://github.com/o/r/pull/42") == 42
    assert pr_number("https://github.com/o/r/pull/42/") == 42


def test_repo_profile_reads_the_dial(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# repo\n\nprofile: autonomous\n\n## facts\n")
    assert repo_profile(str(tmp_path)) == "autonomous"


def test_repo_profile_prefers_agents_md_then_claude(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("profile: guarded\n")
    assert repo_profile(str(tmp_path)) == "guarded"


def test_repo_profile_defaults_reviewed_when_absent(tmp_path):
    # ADR 0002 safe default — never auto-merge a repo that didn't opt in.
    assert repo_profile(str(tmp_path)) == "reviewed"
