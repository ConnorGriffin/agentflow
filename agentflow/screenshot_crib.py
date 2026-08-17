"""The one owned copy of how a spawned session captures the UI evidence the charter demands.

Same failure shape as the shell crib (see ``shell_crib``): sessions share no memory, so every
session that needs a screenshot rediscovers the same sandbox walls — crashpad, the 104-char
sockaddr_un limit, ``page.route`` not intercepting ``file://`` fetches, ``context.close()``
killing a ``--single-process`` browser. ``scripts/screenshots.mjs`` bakes that recipe in, and
every prompt points at it.

The escape hatch matters as much as the instruction. Only the repos that were fixed up by hand
carry the harness — enrolment did not install it — so the earlier wording ordered a session to
run a script that was not there while forbidding it to write one. Three reviews of one PR each
burned their whole turn budget improvising Playwright and returned no verdict at all. A session
in a repo without the harness must be told to port it in, not left to improvise.

Keep it free of ``{`` / ``}`` — the prompts that carry it are ``str.format``-rendered, so an
unescaped brace here would break every render. The one deliberate exception is the
``{screenshot_entry_note}`` placeholder every render site must fill (see ``screenshot_entry_note``);
an accidental brace anywhere else would still break every render.
"""

# How a session independently verifies a user-facing change by actually running the app (#737).
# Codex's launcher adds its narrow browser-escalation recovery; Claude's strict sandbox exposes no
# equivalent. State both contracts in the review prompt so a reviewer never pretends it can recover
# on Claude or reports an unavailable browser without the evidence settlement needs. Same brace
# constraint as the harness above: the prompts carrying it are format-rendered.
UI_VERIFICATION_PROCEDURE = (
    "When the change touches a declared user-facing surface, verify it by running the app "
    "yourself, headless — the same procedure a build follows. Boot the app locally, then drive it "
    "with the shared drive-local-webapp driver to load the affected screens and exercise the "
    "changed behavior. When that driver prints HEADLESS-SANDBOX-BLOCKED, the sandbox blocked the "
    "browser, not the change. On Codex, use the Codex-only browser recovery supplied by your "
    "launcher and continue; keep the app server and every other command inside the workspace "
    "sandbox. On Claude, the strict launcher provides no sandbox-escalation mechanism, so do not "
    "claim a "
    "recovery exists: report ui_verification as unavailable, including the exact command and "
    "HEADLESS-SANDBOX-BLOCKED in checks. Never substitute a written explanation for actually "
    "running the app."
)

SCREENSHOT_HARNESS = (
    "Capture them with the canonical harness: `node scripts/screenshots.mjs <config.json>` — "
    "write a small per-issue config (url, theme, out per shot) and run the script. If this repo "
    "has no `scripts/screenshots.mjs`, that is the one case where you write it: port agentflow's "
    "own copy in at exactly that path and commit it with your change, so the next session inherits "
    "a working harness. Never improvise a throwaway harness in a scratch directory — every session "
    "that does rediscovers the same sandbox walls and burns its whole turn budget on them."
    "{screenshot_entry_note}"
)


def screenshot_entry_note(entry: str | None) -> str:
    """The sentence that redirects a session to a repo's own sanctioned capture wrapper.

    Empty when the repo declares no ``screenshot-entry:`` — the canonical-harness instruction stands
    alone exactly as before. When a repo declares a wrapper that imports the pinned harness, this
    points the session at it without ever licensing an edit to ``scripts/screenshots.mjs`` itself
    (#735); a repo-local modification belongs in the extension, never in the pinned bytes.
    """
    if not entry:
        return ""
    return (
        f" This repo declares a local capture entry point: run `node {entry} <config.json>` "
        "instead — it wraps the pinned harness with this repo's sanctioned extensions; still "
        "never edit `scripts/screenshots.mjs` itself."
    )
