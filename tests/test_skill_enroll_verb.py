"""Issue #383: the shipped operator skill must teach the `enroll` verb."""

from pathlib import Path

SKILL = Path("skills/agentflow/SKILL.md").read_text()


def test_skill_documents_enroll_verb():
    assert "### `enroll <path>`" in SKILL


def test_skill_description_mentions_enrolling_a_repository():
    front_matter = SKILL.split("---", 2)[1]
    assert "enroll" in front_matter.lower()


def test_enroll_verb_names_the_three_profiles_and_the_safe_default():
    section = SKILL.split("### `enroll <path>`", 1)[1].split("\n### ", 1)[0]
    assert "`reviewed`" in section
    assert "`guarded`" in section
    assert "`autonomous`" in section
    assert "safe default" in section.lower()


def test_enroll_verb_states_manual_github_follow_up_is_ci_only():
    section = SKILL.split("### `enroll <path>`", 1)[1].split("\n### ", 1)[0]
    assert "pull-request CI" in section
    assert "no** manual step" in section or "no manual step" in section
    assert "config.toml" in section


def test_argument_summary_no_longer_claims_every_verb_takes_n():
    intro = SKILL.split("## Verbs", 1)[1].split("### ", 1)[0]
    assert "repository path" in intro
