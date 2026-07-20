"""The one owned copy of the sandbox shell rules every spawned session is taught up front.

Fleet transcripts show the plurality of provider tool errors are the harness rejecting the
agent's own shell commands — chained operations, `cd` prefixes, `$VAR` expansion the static
analyzer refuses, `/tmp` writes, `sleep`-chaining — not the app failing (issue #279). Each
rejection burns a retry cycle, and sessions share no memory, so every session rediscovers the
same walls. Teaching the rules once, for ~200 tokens, removes the multi-thousand-token retry
loops.

This is the single location: every stage that spawns a session appends ``SHELL_CRIB`` to its
prompt. Keep it free of ``{`` / ``}`` — the prompts that carry it are ``str.format``-rendered,
so an unescaped brace here would break every render.
"""

SHELL_CRIB = """

Shell habits for this sandbox — following them avoids the command rejections that burn retry
cycles. Different tools sandbox the shell differently, so if the harness ever rejects a command,
adjust it as below rather than re-running the same form hoping it passes:
- Keep each command to one operation. If a `;`- or `&&`-chained command is rejected, split it
  into separate commands instead of retrying the chain.
- Prefer passing the target path to the tool over a `cd <dir> && ...` prefix: `git -C <dir> ...`,
  `pytest <dir>`, absolute paths for reads and writes.
- If `$VAR` / `$(...)` expansion or a glob (`*`) inside a command's arguments is rejected, write
  the literal paths, or put the logic in a small script file and run that.
- Write any temp file inside your assigned worktree (the only writable location) — never `/tmp`
  or an absolute path outside the worktree; the sandbox rejects those.
- Don't `sleep N && cmd` to wait — run the command directly, or poll in separate steps."""
