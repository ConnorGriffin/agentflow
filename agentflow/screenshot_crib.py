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
unescaped brace here would break every render.
"""

SCREENSHOT_HARNESS = (
    "Capture them with the canonical harness: `node scripts/screenshots.mjs <config.json>` — "
    "write a small per-issue config (url, theme, out per shot) and run the script. If this repo "
    "has no `scripts/screenshots.mjs`, that is the one case where you write it: port agentflow's "
    "own copy in at exactly that path and commit it with your change, so the next session inherits "
    "a working harness. Never improvise a throwaway harness in a scratch directory — every session "
    "that does rediscovers the same sandbox walls and burns its whole turn budget on them."
)
