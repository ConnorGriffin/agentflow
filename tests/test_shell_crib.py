"""Every daemon-spawned session is taught the sandbox shell rules up front (issue #279).

The crib lives in exactly one place (``agentflow.shell_crib.SHELL_CRIB``) and every stage that
launches a provider session appends it to that session's prompt. These tests render each stage's
prompt the way the coordinator does — through ``str.format`` — proving both that the crib reaches
the session and that it stays brace-safe so it never breaks a render.
"""

from __future__ import annotations

from collections import defaultdict

from agentflow.coordinated_converse import ASK_PROMPT, BUILD_ISSUE_PROMPT
from agentflow.coordinated_research import RESEARCH_PROMPT
from agentflow.intake import INTAKE_PROMPT
from agentflow.loop import BUILD_PROMPT, PRODUCE_PROMPT, RESPOND_PROMPT, REVISE_PROMPT
from agentflow.reviewer import REVIEW_PROMPT
from agentflow.shell_crib import SHELL_CRIB

# One prompt per stage that spawns a provider session. If a new session-spawning stage is added,
# it belongs here.
SESSION_PROMPTS = {
    "build": BUILD_PROMPT,
    "mockup": PRODUCE_PROMPT,
    "review": REVIEW_PROMPT,
    "revise": REVISE_PROMPT,
    "respond": RESPOND_PROMPT,
    "intake": INTAKE_PROMPT,
    "research": RESEARCH_PROMPT,
    "converse-ask": ASK_PROMPT,
    "converse-build-issue": BUILD_ISSUE_PROMPT,
}


def _render(template: str) -> str:
    """Render a prompt the way the coordinator does — every placeholder gets a value, so a rendered
    prompt (not just the raw template) is what we assert against."""
    return template.format_map(defaultdict(lambda: "x"))


def test_crib_covers_every_required_shell_rule():
    rules = ["chain", "cd ", "$VAR", "worktree", "/tmp", "sleep"]
    for rule in rules:
        assert rule in SHELL_CRIB, f"crib is missing the {rule!r} rule"


def test_every_spawned_session_prompt_carries_the_crib():
    for stage, template in SESSION_PROMPTS.items():
        rendered = _render(template)
        assert SHELL_CRIB in rendered, f"{stage} prompt does not carry the shell-rules crib"


def test_crib_is_brace_safe_so_it_never_breaks_a_render():
    # The crib flows through str.format; a stray brace would raise here.
    assert "{" not in SHELL_CRIB and "}" not in SHELL_CRIB
    for template in SESSION_PROMPTS.values():
        _render(template)  # must not raise
