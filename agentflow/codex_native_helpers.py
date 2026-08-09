"""The 0.144.0 native-Codex-helper compatibility adapter (#509, ADR 541).

Codex CLI 0.144.0's ``spawn_agent`` schema hides the ``agent_type``/``model``/
``reasoning_effort`` fields it presents to Sol; the runtime still accepts them through a
private per-launch custom role (``docs/research/codex-0.144-native-subagent-routing.md``).
This module is the only place that knows how to turn a routed Codex worker pair into a
role file and CLI overrides for that hidden path — routing and the runner never see the
version gate, the file format, or the temp-directory lifecycle. It never copies the routing
table: :meth:`agentflow.routing.CapabilityRouting.codex_worker_roles` remains the one
source of which model/reasoning pairs exist.

Fails closed on every uncertain signal (an unreadable ``--version``, a build outside the
allowlist, a file the temp directory refuses to create) — an unavailable adapter is a
provider-fallback condition for the caller, never a silently wrong model.

This module never launches a subprocess itself: every real provider command (including the
``codex --version`` capability probe) is built and run only by :mod:`agentflow.runner`, the
package's one adapter boundary for provider-capable processes (tests/test_dispatch.py). Version
parsing here (:func:`is_supported_version`) is pure and takes the already-captured CLI output.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# The exact installed CLI builds this adapter is proven against
# (docs/research/codex-0.144-native-subagent-routing.md). Version alone is necessary but not
# sufficient for the live schema to behave this way (the research note's own caveat), so this
# allowlist is deliberately an exact-match set, never a floor or a range.
SUPPORTED_CODEX_VERSIONS = frozenset({"0.144.0"})

_VERSION_LINE = re.compile(r"codex-cli\s+(\S+)")

_ROLE_DIR_PREFIX = "agentflow-codex-roles-"
# A role directory is owned by the launch that created it: the double-forked launch supervisor
# (agentflow/coordinator/_launch_child.py) is the one process that both calls
# :func:`build_role_overrides` (immediately before it spawns the provider) and later calls
# :func:`cleanup_role_dirs` on the exact :class:`RoleOverrides` it got back — a local value that
# never leaves that process, crosses no argv/env/durable-record boundary, and so is never
# reconstructed from anything CodeQL would treat as external input (#509). Cleanup runs in a
# path that covers a clean exit, a provider failure, a spawn failure, and a role-generation
# failure alike — never only the happy path. The 24h sweep below stays as the crash-recovery
# backstop for the one case ownership cannot cover: the launch supervisor itself was killed
# before its cleanup ran. Mirrors the stranded-worktree idiom in runner.py
# (STRANDED_IDLE_SECONDS, SWEEP_ARCHIVE_BUDGET) — old enough to be certainly abandoned, budgeted
# so one sweep never stalls a launch scanning an unbounded backlog.
STALE_ROLE_DIR_SECONDS = 24 * 3600
ROLE_DIR_SWEEP_BUDGET = 20

# The exact name grammar :func:`tempfile.mkdtemp` produces for this prefix: the fixed prefix
# followed by one or more of its random-suffix characters, never anything else. Every deletion
# path in this module (the stale sweep and the per-launch cleanup) must match this grammar *and*
# resolve to a real, non-symlink directory directly under the system temp root before it is ever
# passed to ``shutil.rmtree`` — a crafted or traversed name is rejected before it becomes a
# deletion target.
_ROLE_DIR_NAME = re.compile(rf"{re.escape(_ROLE_DIR_PREFIX)}[A-Za-z0-9_-]+\Z")


def _owned_role_dir(path: Path) -> Path | None:
    """Return this module's own role directory for *path*, or ``None`` for anything else.

    Follows CodeQL's documented path-injection containment shape directly at each filesystem
    sink in this function, rather than behind an interprocedural sanitizer: a canonical trusted
    base (``realpath`` of the system temp root), a normalized candidate built from that base plus
    a generated-name-grammar-checked basename, an explicit ``candidate.startswith(base + os.sep)``
    containment check *before* any filesystem call, then symlink rejection and ``realpath``
    resolution with the same containment check re-applied to the resolved path before the
    real-directory check. Returns ``None`` for a symlink, a plain file, a differently-shaped name,
    or a path a traversal or an absolute override tried to point outside the temp root."""
    base = os.path.realpath(tempfile.gettempdir())
    normalized = os.path.normpath(os.fspath(path))
    name = os.path.basename(normalized)
    if os.path.dirname(normalized) != base or not _ROLE_DIR_NAME.fullmatch(name):
        return None
    candidate = os.path.normpath(os.path.join(base, name))
    if candidate != base and not candidate.startswith(base + os.sep):
        return None
    try:
        if os.path.islink(candidate):
            return None
        resolved = os.path.realpath(candidate)
    except OSError:
        return None
    if resolved != candidate:
        return None
    if resolved != base and not resolved.startswith(base + os.sep):
        return None
    if not os.path.isdir(resolved) or os.path.islink(resolved):
        return None
    return Path(resolved)


_ROLE_DEVELOPER_INSTRUCTIONS = (
    "You are an AgentFlow-routed Codex worker delegated by this session's lead. Do the task "
    "given in this turn's message exactly; do not act outside the scope that message states."
)


def is_supported_version(version_output: str | None) -> bool:
    """Whether already-captured ``codex --version`` stdout names an exact build this adapter is
    proven against. Pure — the caller (:mod:`agentflow.runner`) owns actually running the
    process. ``None`` (the caller's own signal for "could not read the version") and any
    unparseable or unrecognized output fail closed — absence of proof is never treated as proof
    the compatibility path is safe."""
    if not version_output:
        return False
    match = _VERSION_LINE.search(version_output)
    return match is not None and match.group(1) in SUPPORTED_CODEX_VERSIONS


def _sweep_stale_role_dirs() -> None:
    """Reclaim role directories a crashed or killed launch left behind, bounded per call.

    These candidates come from enumerating the filesystem (a glob over the temp root), so —
    like :func:`cleanup_role_dirs` — the containment and real-directory checks are re-run
    inline, immediately before ``shutil.rmtree``, rather than trusted from
    :func:`_owned_role_dir`'s return value alone."""
    base = os.path.realpath(tempfile.gettempdir())
    now = time.time()
    swept = 0
    try:
        entries = sorted(Path(base).glob(f"{_ROLE_DIR_PREFIX}*"))
    except OSError:
        return
    for entry in entries:
        if swept >= ROLE_DIR_SWEEP_BUDGET:
            return
        owned = _owned_role_dir(entry)
        if owned is None:
            continue
        try:
            age = now - owned.stat().st_mtime
        except OSError:
            continue
        if age < STALE_ROLE_DIR_SECONDS:
            continue
        candidate = os.path.normpath(os.fspath(owned))
        if candidate != base and not candidate.startswith(base + os.sep):
            continue
        try:
            if os.path.islink(candidate):
                continue
            resolved = os.path.realpath(candidate)
        except OSError:
            continue
        if resolved != candidate:
            continue
        if resolved != base and not resolved.startswith(base + os.sep):
            continue
        if not os.path.isdir(resolved) or os.path.islink(resolved):
            continue
        shutil.rmtree(resolved, ignore_errors=True)
        swept += 1


@dataclass(frozen=True)
class RoleOverrides:
    """A real :func:`build_role_overrides` call's CLI argv, plus the role directory it created —
    a local value the launch supervisor (:mod:`agentflow.coordinator._launch_child`) keeps to
    itself for this call's whole lifetime: generated immediately before it spawns the provider,
    read to build that provider's argv, and handed straight back to :func:`cleanup_role_dirs` in
    the same process. It never crosses argv, an environment variable, the durable record, or any
    other external boundary (#509)."""

    argv: list[str]
    directory: Path | None = None


def build_role_overrides(routes: tuple[tuple[str, str, str], ...]) -> RoleOverrides:
    """CLI ``-c``/``--strict-config`` argv naming one private, owner-only role file per
    ``(role_name, cli_id, reasoning_effort)`` triple from
    :meth:`agentflow.routing.CapabilityRouting.codex_worker_roles`, paired with the directory
    that owns them.

    Each role file is mode-0600 inside a fresh mode-0700 directory outside any consumer
    worktree (the system temp root), and carries only the allowlisted ``model``,
    ``model_reasoning_effort``, and a fixed ``developer_instructions`` string — never a
    copy of the routing table's provenance, bans, or ladders, and never consumer-repo
    content. Each file's own name is a fixed ``role-<index>.toml`` — never the routed
    ``role_name`` — so a route's name is only ever used inside a config key or a file's content,
    never as a filesystem path component. ``--strict-config`` is added alongside so an
    unrecognized override anywhere in this launch's config fails the launch instead of silently
    loading a partial policy.

    Returns ``RoleOverrides([], None)`` for an empty ``routes`` (nothing to declare — a Codex
    parent with no reachable worker pair, which the routing table's own validation makes
    unreachable in practice)."""
    if not routes:
        return RoleOverrides([], None)
    _sweep_stale_role_dirs()
    # tempfile.gettempdir()'s raw path can itself be a symlink (macOS: /var -> /private/var);
    # _owned_role_dir requires the canonical realpath base, so the directory is created
    # directly under that resolved base rather than the raw one — otherwise this function's
    # own directory would be rejected by cleanup_role_dirs immediately after.
    base = os.path.realpath(tempfile.gettempdir())
    directory = Path(tempfile.mkdtemp(prefix=_ROLE_DIR_PREFIX, dir=base))
    argv: list[str] = ["--strict-config"]
    try:
        directory.chmod(0o700)
        for index, (role_name, cli_id, reasoning) in enumerate(routes):
            role_path = directory / f"role-{index}.toml"
            toml = (
                f'model = {json.dumps(cli_id)}\n'
                f'model_reasoning_effort = {json.dumps(reasoning)}\n'
                f'developer_instructions = {json.dumps(_ROLE_DEVELOPER_INSTRUCTIONS)}\n'
            )
            fd = os.open(role_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(toml)
            argv += [
                "-c", f"agents.{role_name}.description={json.dumps(f'AgentFlow routed role {role_name}')}",
                "-c", f"agents.{role_name}.config_file={json.dumps(str(role_path))}",
            ]
    except OSError:
        # A partial generation (a chmod or some role files written, one failed) is never left
        # for the 24h sweep to find — cleaned up immediately, then the caller's own fail-closed
        # launch fallback applies (this module never launches, so it never masks the failure).
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return RoleOverrides(argv, directory)


def cleanup_role_dirs(role_dir: str | Path | None) -> None:
    """Remove the role directory produced by this same call's own :func:`build_role_overrides`
    (its :attr:`RoleOverrides.directory`), if any. The launch supervisor
    (:mod:`agentflow.coordinator._launch_child`) is both the only caller of
    :func:`build_role_overrides` and the only caller of this function, in the same process, for
    the same local value — nothing here is ever reconstructed from ``argv``, an environment
    variable, or the durable record (#509). Called from a path that covers a clean exit, a
    provider failure, a spawn failure, and a role-generation failure alike, so the directory is
    removed the moment its one launch ends rather than relied on to be reclaimed only by the 24h
    stale sweep. Best-effort: a missing or never-created ``role_dir`` is not an error here — it
    is simply not removed.

    :func:`_owned_role_dir` still performs the canonical containment, generated-name-grammar, and
    symlink/real-directory checks immediately before the removal below — belt-and-suspenders
    given this value's own local provenance, not a defense against an external caller."""
    if not role_dir:
        return
    owned = _owned_role_dir(Path(role_dir))
    if owned is None:
        return
    shutil.rmtree(owned, ignore_errors=True)
