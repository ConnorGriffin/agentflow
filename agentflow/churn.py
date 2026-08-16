"""Zero-token churn analysis over fleet transcripts.

Parses Claude Code session JSONL (``~/.claude/projects``) and Codex rollout
JSONL, filters to fleet-spawned sessions for the
configured repositories, and ranks them by token-burning churn: tool errors,
repeated commands, re-reads, screenshot flailing, and compactions. Pure
stdlib; read-only over both transcript trees.

This is tuning tooling for the fleet operator, not part of the merge gate:
`agentflow churn` prints a ranking so a human decides which sessions are
worth reading, rather than any pipeline stage acting on the score.
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections import defaultdict
from pathlib import Path, PurePath

from agentflow.codex_transcripts import (
    codex_sessions_root, rollout_paths, was_started_by_codex_exec, where_did_session_run,
    what_did_session_spend)
from agentflow.loop import RepoConfig

DEFAULT_CLAUDE_ROOT = os.path.expanduser("~/.claude/projects")
DEFAULT_CODEX_ROOT = str(codex_sessions_root())

SCREENSHOT_RE = re.compile(r"screenshot|playwright|chromium|puppeteer|page\.pdf", re.I)
TEST_RE = re.compile(r"\b(pytest|npm (run )?test|vitest|jest|cargo test|go test)\b")

# Commands whose repetition is normal, not churn.
BENIGN_REPEAT_RE = re.compile(
    r"^(git (status|diff|log|add|show)|ls|pwd|cat|head|tail|echo|true)\b"
)


def _encode_cwd(path: str) -> str:
    """Claude's project-directory encoding of an absolute cwd: every `/` and `.` becomes `-`.

    Matching session directories this way, instead of a hardcoded per-user path pattern,
    is what lets one repo's configured `workdir` (any user, any location) find its own
    transcripts.
    """
    return re.sub(r"[/.]", "-", path)


def norm_cmd(cmd: str) -> str:
    """Normalize a shell command for repeat detection."""
    c = " ".join(cmd.split())
    return c[:160]


def phase_of(label: str) -> str:
    """Infer pipeline phase from a worktree/session label."""
    for p in ("intake", "review", "diagnose", "mockup", "ask", "scope"):
        if p in label:
            return p
    if re.search(r"\bpr-\d+", label):
        return "review"
    if re.search(r"issue-\d+", label):
        return "build"
    return "other"


def _new_session(source, path, label, repo, fleet=True):
    return {
        "source": source,
        "path": path,
        "label": label,
        "repo": repo,
        "fleet": fleet,
        "phase": phase_of(label),
        "api_calls": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "uncached_input_tokens": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "max_error_streak": 0,
        "repeat_cmd_calls": 0,  # calls beyond the 2nd of an identical command
        "repeated_cmds": [],  # (count, cmd) for cmds seen >= 3 times
        "rereads": 0,  # Reads beyond the 2nd of the same file
        "edits": 0,
        "test_runs": 0,
        "screenshot_calls": 0,
        "screenshot_errors": 0,
        "compactions": 0,
    }


def _finish_session(s, cmd_counter, read_counter, err_streak):
    s["max_error_streak"] = max(s["max_error_streak"], err_streak)
    for cmd, n in cmd_counter.items():
        if n >= 3 and not BENIGN_REPEAT_RE.match(cmd):
            s["repeated_cmds"].append((n, cmd))
            s["repeat_cmd_calls"] += n - 2
    s["repeated_cmds"].sort(reverse=True)
    s["repeated_cmds"] = s["repeated_cmds"][:5]
    s["rereads"] = sum(n - 2 for n in read_counter.values() if n >= 3)
    # Churn score: token-priced where possible, structural otherwise.
    s["churn_score"] = (
        s["tool_errors"] * 3
        + s["repeat_cmd_calls"] * 2
        + s["rereads"]
        + s["screenshot_errors"] * 3
        + s["compactions"] * 5
    )
    return s


# ---------------------------------------------------------------- Claude side


def claude_session_dirs(repos: list[RepoConfig], claude_root: str = DEFAULT_CLAUDE_ROOT):
    for d in sorted(glob.glob(os.path.join(claude_root, "*"))):
        name = os.path.basename(d)
        if "worktrees" not in name:
            continue
        for cfg in repos:
            prefix = _encode_cwd(str(Path(cfg.workdir).resolve()))
            if name.startswith(prefix + "-"):
                yield d, cfg.repo
                break


def parse_claude_file(path, repo, label, fleet=True):
    s = _new_session("claude", path, label, repo, fleet)
    cmds, reads = defaultdict(int), defaultdict(int)
    streak = 0
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "summary" or d.get("isCompactSummary"):
                s["compactions"] += 1
                continue
            msg = d.get("message") or {}
            if t == "assistant":
                u = msg.get("usage") or {}
                if u:
                    s["api_calls"] += 1
                    s["output_tokens"] += u.get("output_tokens", 0)
                    s["cache_creation_tokens"] += u.get("cache_creation_input_tokens", 0)
                    s["uncached_input_tokens"] += u.get("input_tokens", 0)
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        s["tool_calls"] += 1
                        name = b.get("name", "")
                        inp = b.get("input") or {}
                        if name == "Bash":
                            cmd = norm_cmd(inp.get("command", ""))
                            cmds[cmd] += 1
                            if SCREENSHOT_RE.search(cmd):
                                s["screenshot_calls"] += 1
                            if TEST_RE.search(cmd):
                                s["test_runs"] += 1
                        elif name == "Read":
                            reads[inp.get("file_path", "")] += 1
                        elif name in ("Edit", "Write", "MultiEdit"):
                            s["edits"] += 1
            elif t == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            if b.get("is_error"):
                                s["tool_errors"] += 1
                                streak += 1
                                sc = str(b.get("content", ""))[:400]
                                if SCREENSHOT_RE.search(sc):
                                    s["screenshot_errors"] += 1
                            else:
                                s["max_error_streak"] = max(s["max_error_streak"], streak)
                                streak = 0
    return _finish_session(s, cmds, reads, streak)


def collect_claude(repos: list[RepoConfig], claude_root: str = DEFAULT_CLAUDE_ROOT):
    out = []
    for d, repo in claude_session_dirs(repos, claude_root):
        name = os.path.basename(d)
        label = name.split("worktrees-", 1)[-1]
        fleet = "--agentflow-worktrees-" in name
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            s = parse_claude_file(f, repo, label, fleet)
            if s["api_calls"] or s["tool_calls"]:
                out.append(s)
    return out


# ----------------------------------------------------------------- Codex side


def _repo_for_cwd(cwd: str, repos: list[RepoConfig]) -> str | None:
    """Which configured repo a rollout's recorded working directory sits under.

    The recorded value comes out of a file another tool wrote, so it is compared
    lexically and never resolved against this machine's filesystem.
    """
    recorded = PurePath(os.path.normpath(cwd))
    for cfg in repos:
        try:
            recorded.relative_to(Path(cfg.workdir).resolve())
        except ValueError:
            continue
        return cfg.repo
    return None


def parse_codex_file(path, repos: list[RepoConfig]):
    with open(path, errors="replace") as fh:
        if not was_started_by_codex_exec(Path(path)):
            return None
        cwd = where_did_session_run(Path(path))
        if cwd is None or "worktrees" not in cwd:
            return None
        repo = _repo_for_cwd(cwd, repos)
        if not repo:
            return None
        fleet = ".agentflow/worktrees" in cwd
        label = cwd.split("worktrees/", 1)[-1]
        s = _new_session("codex", path, label, repo, fleet)
        cmds, reads = defaultdict(int), defaultdict(int)
        streak = 0
        spend = what_did_session_spend(Path(path))
        fh.readline()                 # the reader checked the required first metadata record
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = d.get("payload") or {}
            pt = p.get("type")
            if d.get("type") == "response_item":
                if pt in ("function_call", "custom_tool_call"):
                    s["tool_calls"] += 1
                    args = p.get("arguments") or p.get("input") or ""
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"raw": args}
                    cmd = args.get("command") if isinstance(args, dict) else None
                    if isinstance(cmd, list):
                        cmd = " ".join(str(c) for c in cmd)
                    if cmd:
                        cmd = norm_cmd(str(cmd))
                        cmds[cmd] += 1
                        if SCREENSHOT_RE.search(cmd):
                            s["screenshot_calls"] += 1
                        if TEST_RE.search(cmd):
                            s["test_runs"] += 1
                elif pt in ("function_call_output", "custom_tool_call_output"):
                    out = p.get("output", "")
                    if isinstance(out, dict):
                        out = json.dumps(out)
                    failed = bool(re.search(r'"exit_code":\s*[1-9]|\bexited (?!0)\d+', str(out)[:2000]))
                    if failed:
                        s["tool_errors"] += 1
                        streak += 1
                        if SCREENSHOT_RE.search(str(out)[:400]):
                            s["screenshot_errors"] += 1
                    else:
                        s["max_error_streak"] = max(s["max_error_streak"], streak)
                        streak = 0
            elif d.get("type") == "event_msg" and pt == "patch_apply_end":
                s["edits"] += 1
            elif d.get("type") == "compacted" or pt == "compacted":
                s["compactions"] += 1
        s["api_calls"] = 1  # codex reports cumulative usage, not per-call
        s["output_tokens"] = spend.output_tokens if spend and spend.output_tokens is not None else 0
        s["uncached_input_tokens"] = (
            (spend.input_tokens or 0) - (spend.cached_input_tokens or 0) if spend else 0)
        return _finish_session(s, cmds, reads, streak)


def collect_codex(repos: list[RepoConfig], codex_root: str = DEFAULT_CODEX_ROOT):
    out = []
    for f in rollout_paths(Path(codex_root)):
        s = parse_codex_file(f, repos)
        if s and (s["tool_calls"] or s["output_tokens"]):
            out.append(s)
    return out


def collect_sessions(
    repos: list[RepoConfig],
    *,
    include_manual: bool = False,
    claude_root: str | None = None,
    codex_root: str | None = None,
) -> list[dict]:
    sessions = collect_claude(repos, claude_root or DEFAULT_CLAUDE_ROOT) + collect_codex(
        repos, codex_root or DEFAULT_CODEX_ROOT
    )
    if not include_manual:
        sessions = [s for s in sessions if s["fleet"]]
    return sessions


# ------------------------------------------------------------------ reporting


def _fmt_row(s: dict) -> str:
    return (
        f"{s['churn_score']:>5}  {s['output_tokens']:>8}  {s['tool_errors']:>4}"
        f"  {s['repeat_cmd_calls']:>4}  {s['rereads']:>3}  {s['screenshot_calls']:>4}/{s['screenshot_errors']:<3}"
        f"  {s['source']:<6} {s['repo']:<20} {s['phase']:<7} {s['label'][:60]}"
    )


def format_report(sessions: list[dict], *, top: int = 25) -> str:
    """Render the churn ranking, repo/phase/tool aggregate, and repeated-command list."""
    if not sessions:
        return "no fleet sessions matched"

    lines = [
        f"{len(sessions)} fleet sessions "
        f"({sum(1 for s in sessions if s['source'] == 'claude')} claude, "
        f"{sum(1 for s in sessions if s['source'] == 'codex')} codex)",
        "",
    ]

    hdr = "churn    out_tok  errs  reps  rrd  shot/err  source repo                 phase   label"
    lines.append("== Top sessions by churn score ==")
    lines.append(hdr)
    for s in sorted(sessions, key=lambda x: -x["churn_score"])[:top]:
        lines.append(_fmt_row(s))

    lines.append("")
    lines.append("== Top sessions by output tokens ==")
    lines.append(hdr)
    for s in sorted(sessions, key=lambda x: -x["output_tokens"])[:top]:
        lines.append(_fmt_row(s))

    agg = defaultdict(lambda: defaultdict(int))
    repeat_global = defaultdict(int)
    for s in sessions:
        key = (s["repo"], s["phase"], s["source"])
        a = agg[key]
        a["n"] += 1
        for k in ("output_tokens", "tool_errors", "repeat_cmd_calls",
                  "screenshot_calls", "screenshot_errors", "compactions"):
            a[k] += s[k]
        for n, cmd in s["repeated_cmds"]:
            repeat_global[cmd] += n

    lines.append("")
    lines.append("== By repo / phase / tool ==")
    lines.append("  n   out_tok  errs  reps  shot/err  cmpct  repo phase source")
    for key in sorted(agg, key=lambda k: -agg[k]["output_tokens"]):
        a = agg[key]
        lines.append(
            f"{a['n']:>3}  {a['output_tokens']:>8}  {a['tool_errors']:>4}  "
            f"{a['repeat_cmd_calls']:>4}  {a['screenshot_calls']:>4}/{a['screenshot_errors']:<3}"
            f"  {a['compactions']:>5}  {key[0]} {key[1]} {key[2]}"
        )

    lines.append("")
    lines.append("== Most-repeated commands across corpus ==")
    for cmd, n in sorted(repeat_global.items(), key=lambda kv: -kv[1])[:20]:
        lines.append(f"{n:>5}  {cmd}")

    return "\n".join(lines)


# ------------------------------------------------------------- excerpt reads


def _blocks(msg):
    c = (msg.get("message") or {}).get("content")
    return c if isinstance(c, list) else []


def format_excerpt(path: str, *, max_chars: int = 9000, window: int = 1) -> str:
    """The error windows of one Claude session: failed tool calls, `window` calls of
    preceding context, and the truncated error text — cheap enough for a subagent to
    read where the full transcript would not be.
    """
    calls = []  # {id, name, input, error, result}
    by_id = {}
    claude_shaped = False
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") in ("assistant", "user"):
                claude_shaped = True
            if d.get("type") == "assistant":
                for b in _blocks(d):
                    if b.get("type") == "tool_use":
                        c = {"id": b.get("id"), "name": b.get("name", ""),
                             "input": b.get("input") or {}, "error": False, "result": ""}
                        calls.append(c)
                        by_id[c["id"]] = c
            elif d.get("type") == "user":
                for b in _blocks(d):
                    if b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                        c = by_id[b["tool_use_id"]]
                        c["error"] = bool(b.get("is_error"))
                        content = b.get("content")
                        if isinstance(content, list):
                            content = " ".join(x.get("text", "") for x in content
                                               if isinstance(x, dict))
                        c["result"] = str(content or "")

    if not claude_shaped:
        # Codex rollouts rank in the same report, so this path is reached by drilling
        # into a codex row — say so rather than reporting an empty Claude session.
        return (f"# {path}\n"
                "# no Claude session records here — excerpts read Claude session "
                "transcripts only; read this one directly\n")

    keep = set()
    for i, c in enumerate(calls):
        if c["error"]:
            keep.update(range(max(0, i - window), i + 1))

    out, budget = [], max_chars
    last = -2
    for i in sorted(keep):
        c = calls[i]
        if i != last + 1:
            out.append("...")
        last = i
        inp = c["input"].get("command") or c["input"].get("file_path") or \
            json.dumps(c["input"])[:200]
        tag = "ERR" if c["error"] else "ok "
        piece = f"[{i}] {tag} {c['name']}: {' '.join(str(inp).split())[:300]}"
        if c["error"]:
            piece += f"\n      -> {' '.join(c['result'].split())[:400]}"
        out.append(piece)
        budget -= len(piece)
        if budget <= 0:
            out.append(f"... truncated at {max_chars} chars "
                       f"({sum(1 for x in calls if x['error'])} errors total)")
            break

    total_err = sum(1 for c in calls if c["error"])
    header = f"# {path}\n# {len(calls)} tool calls, {total_err} errors\n"
    return header + "\n" + "\n".join(out)
