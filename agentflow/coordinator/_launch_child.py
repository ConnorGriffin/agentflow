"""The provider-launch bootstrap child (ADR 0030).

Run as
``python -m agentflow.coordinator._launch_child <store_path> <identity> <token> <timeout>
[--build-lease <provider> <silent> <test> <absolute>] --inherited-worktree [argv...]``.

It double-forks so the provider family is reparented away from the daemon (and so an ended
provider never lingers as a zombie the daemon would misread as alive), then makes a *guarded*
durable ``started`` write with the detached supervisor's pid — recorded only if this reservation
still holds ``token``. It then forks the provider into a separate session behind a pipe gate,
durably expands the family to ``supervisor:provider-group``, and releases the provider to exec.
If the coordinator already disowned this launch or another bootstrap won, the guarded write is
refused and the child exits without becoming a provider. If the supervisor vanishes before the
family expansion, pipe EOF makes the gated child exit without running provider code. With no
provider argv (the dormant slice), the supervisor exits after the successful start claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from agentflow.coordinator.session import events_path, write_result
from agentflow.coordinator.store import Store

_HEAD_FILE_BYTES = 8 * 1024 * 1024
_HEAD_OBSERVATION_S = 0.025
_HEAD_HELPERS: set[int] = set()
_INHERITED_WORKTREE = "--inherited-worktree"
_NO_WORKTREE = "--no-worktree"


class _ForkedProvider:
    """A provider fork held behind a pipe gate until its process group is durable."""

    def __init__(self, pid: int, command: list[str], gate_write: int) -> None:
        self.pid = pid
        self._command = command
        self._gate_write: int | None = gate_write
        self._returncode: int | None = None

    def release(self) -> None:
        try:
            os.write(self._gate_write, b"1")
        finally:
            self._close_gate()

    def refuse(self) -> None:
        self._close_gate()  # EOF makes the child exit without executing provider code
        self.wait()

    def _close_gate(self) -> None:
        if self._gate_write is None:
            return
        try:
            os.close(self._gate_write)
        finally:
            self._gate_write = None

    def _finish(self, status: int) -> int:
        self._returncode = os.waitstatus_to_exitcode(status)
        return self._returncode

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        waited, status = os.waitpid(self.pid, os.WNOHANG)
        return self._finish(status) if waited else None

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is not None:
            return self._returncode
        if timeout is None:
            _, status = os.waitpid(self.pid, 0)
            return self._finish(status)
        deadline = time.monotonic() + timeout
        while True:
            result = self.poll()
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self._command, timeout)
            time.sleep(min(0.01, remaining))


def _spawn_provider(provider: list[str], output) -> _ForkedProvider:
    """Fork a separate-session provider that cannot exec until its family is durable."""
    gate_read, gate_write = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(gate_read)
        os.close(gate_write)
        raise
    if pid == 0:
        os.close(gate_write)
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.setsid()
            os.dup2(output.fileno(), 1)
            os.dup2(output.fileno(), 2)
            released = os.read(gate_read, 1) == b"1"
            os.close(gate_read)
            if not released:
                os._exit(0)
            os.execvp(provider[0], provider)
        except OSError:
            os._exit(127)
    os.close(gate_read)
    return _ForkedProvider(pid, provider, gate_write)


def _reap_head_helpers() -> None:
    for pid in tuple(_HEAD_HELPERS):
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            waited = pid
        if waited:
            _HEAD_HELPERS.discard(pid)


def _read_regular_text(path: Path, limit: int = _HEAD_FILE_BYTES) -> str | None:
    """Read one bounded regular Git metadata file without following a special-file trap."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > limit:
                return None
            return raw.decode("utf-8")
        finally:
            os.close(fd)
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _object_id(value: str) -> str | None:
    fields = value.split()
    if len(fields) != 1:
        return None
    oid = fields[0]
    return (oid if len(oid) in {40, 64}
            and all(c in "0123456789abcdef" for c in oid) else None)


def _git_dir(working_dir: str) -> Path | None:
    dot_git = Path(working_dir) / ".git"
    if dot_git.is_dir():
        return dot_git
    marker_text = _read_regular_text(dot_git)
    if marker_text is None:
        return None
    marker = marker_text.strip()
    if not marker.startswith("gitdir: "):
        return None
    path = Path(marker.removeprefix("gitdir: "))
    return path if path.is_absolute() else dot_git.parent / path


def _head_snapshot(working_dir: str) -> str | None:
    """Read HEAD from bounded regular Git metadata files, failing closed on every anomaly."""
    if not working_dir or (git_dir := _git_dir(working_dir)) is None:
        return None
    head_text = _read_regular_text(git_dir / "HEAD")
    if head_text is None:
        return None
    head = head_text.strip()
    if not head.startswith("ref: "):
        return _object_id(head)
    ref = head.removeprefix("ref: ")
    parts = Path(ref).parts
    if not ref.startswith("refs/") or not parts or ".." in parts:
        return None
    common_dir = git_dir
    try:
        common_text = _read_regular_text(git_dir / "commondir")
        if common_text is None:
            raise ValueError("no regular commondir")
        common = common_text.strip()
        common_dir = Path(common) if Path(common).is_absolute() else git_dir / common
    except (OSError, ValueError):
        pass
    for root in (git_dir, common_dir):
        text = _read_regular_text(root / ref)
        if text is not None and (oid := _object_id(text)):
            return oid
    packed_text = _read_regular_text(common_dir / "packed-refs")
    if packed_text is None:
        return None
    packed = packed_text.splitlines()
    for line in packed:
        if line.startswith(("#", "^")):
            continue
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref:
            return _object_id(fields[0])
    return None


def _head(working_dir: str, timeout: float = _HEAD_OBSERVATION_S) -> str | None:
    """Observe HEAD in a killable helper so slow metadata cannot strand the provider owner."""
    _reap_head_helpers()
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        return None
    try:
        pid = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        return None
    if pid == 0:
        os.close(read_fd)
        try:
            head = _head_snapshot(working_dir)
            if head is not None:
                os.write(write_fd, head.encode("ascii"))
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    raw = b""
    deadline = time.monotonic() + timeout
    try:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([read_fd], [], [], remaining)
        if ready:
            raw = os.read(read_fd, 65)
    except (OSError, ValueError):
        raw = b""
    finally:
        os.close(read_fd)
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited == 0:
                os.kill(pid, signal.SIGKILL)
                waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited == 0:
                _HEAD_HELPERS.add(pid)
        except (ChildProcessError, OSError):
            _HEAD_HELPERS.discard(pid)
    try:
        return _object_id(raw.decode("ascii"))
    except UnicodeError:
        return None


_TEST_PREFIXES = (("pytest",), ("uv", "run", "pytest"), ("npm", "test"),
                  ("npm", "run", "test"), ("pnpm", "test"), ("yarn", "test"),
                  ("cargo", "test"), ("go", "test"), ("make", "test"))
_SHELL_COMPOSITION = frozenset(";&|<>()$`\\*?[]{}~!#\r\n")
_EVENT_READ_BYTES = 64 * 1024
_EVENT_RECORDS_PER_POLL = 128
_EVENT_POLL_SLICE_S = 0.01
_EVENT_RECORD_BYTES = 1024 * 1024
_WORKTREE_OBSERVATION_S = 0.10
_WORKTREE_STATUS_BYTES = 1024 * 1024
_WORKTREE_PATHS = 4096
_WORKTREE_FILE_BYTES = 8 * 1024 * 1024
_WORKTREE_TOTAL_BYTES = 64 * 1024 * 1024
_WORKTREE_HELPERS: set[int] = set()


def _recognized_test(command: object, provider: str) -> bool:
    """Recognize only the provider's canonical, single test-command shape.

    Codex reports the shell wrapper it owns; Claude reports the Bash input itself. Shell
    composition is deliberately refused: a pipeline or chained command's eventual success is
    not proof that the named test succeeded, and supervising it would turn arbitrary commands
    beginning with ``pytest`` into test work.
    """
    if not isinstance(command, str):
        return False
    test_command = command
    if provider == "codex":
        try:
            words = shlex.split(command)
        except ValueError:
            return False
        if len(words) != 3 or words[:2] != ["/bin/zsh", "-lc"]:
            return False
        test_command = words[2]
    elif provider != "claude":
        return False
    # Reject shell interpretation before tokenizing. Even quoted or escaped syntax fails closed:
    # this lease recognizes one literal test command, never composition, substitution,
    # redirection, variable/glob/brace/tilde expansion, or a comment/newline boundary.
    if (not test_command or any(char in _SHELL_COMPOSITION for char in test_command)
            or any(ord(char) < 32 and char != "\t" for char in test_command)):
        return False
    try:
        words = shlex.split(test_command)
    except ValueError:
        return False
    return any(tuple(words[:len(prefix)]) == prefix for prefix in _TEST_PREFIXES)


def _recognized_worker(command: object) -> bool:
    """Recognize the exact bounded-worker command shape the Codex launcher may approve."""
    if not isinstance(command, str):
        return False
    try:
        outer = shlex.split(command)
        if (len(outer) != 3 or outer[:2] != ["/bin/zsh", "-lc"]
                or shlex.join(outer) != command):
            return False
        program = outer[2]
    except ValueError:
        return False
    matched = re.fullmatch(
        r'agentflow-codex-worker --worker ([A-Za-z0-9._-]+) '
        r'--effort (low|medium|high|extra) --timeout ([0-9]+) < "([^"\r\n]+)"',
        program)
    if matched is None:
        return False
    worker, _effort, timeout_text, prompt_text = matched.groups()
    if any(char in "\\$`" for char in prompt_text):
        return False
    try:
        timeout = int(timeout_text)
        prompt = Path(prompt_text)
        info = prompt.lstat()
        from agentflow.routing import RoutingConfigError, routing
        routing.codex_worker_cli_identifier(worker)
    except (OSError, RoutingConfigError, ValueError):
        return False
    return (timeout_text == str(timeout) and 1 <= timeout <= 900 and prompt.is_absolute()
            and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600)


def _reap_worktree_helpers() -> bool:
    clean = True
    for pid in tuple(_WORKTREE_HELPERS):
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            _WORKTREE_HELPERS.discard(pid)
            clean = False
            continue
        except OSError:
            clean = False
            continue
        if waited:
            _WORKTREE_HELPERS.discard(pid)
        else:
            clean = False
    return clean


def _status_paths(raw: bytes) -> list[tuple[bytes, bytes, bytes | None]] | None:
    """Decode bounded porcelain-v1 records into the paths whose durable bytes matter."""
    records = raw.split(b"\0")
    if records[-1] != b"":
        return None
    paths: list[tuple[bytes, bytes, bytes | None]] = []
    index = 0
    while index < len(records) - 1:
        record = records[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" ":
            return None
        status, path = record[:2], record[3:]
        if not path or b"\0" in path:
            return None
        prior_path = None
        if b"R" in status or b"C" in status:
            if index >= len(records) - 1 or not records[index]:
                return None
            prior_path = records[index]
            index += 1
        paths.append((status, path, prior_path))
        if len(paths) > _WORKTREE_PATHS:
            return None
    return paths


def _numstat_paths(raw: bytes) -> set[bytes] | None:
    """Decode content-changing paths from bounded, rename-disabled Git numstat output."""
    records = raw.split(b"\0")
    if records[-1] != b"":
        return None
    paths: set[bytes] = set()
    for record in records[:-1]:
        fields = record.split(b"\t", 2)
        if len(fields) != 3 or not fields[2]:
            return None
        added, deleted, path = fields
        if (added != b"-" and not added.isdigit()) or (deleted != b"-" and not deleted.isdigit()):
            return None
        if added != b"0" or deleted != b"0":
            paths.add(path)
    return paths


def _hash_frame(digest, tag: bytes, value: bytes) -> None:
    """Hash one unambiguous typed field without allocating its framed representation."""
    digest.update(len(tag).to_bytes(2, "big"))
    digest.update(tag)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _close_fd(fd: int) -> None:
    """Release a snapshot descriptor without letting cleanup obscure a failed observation."""
    try:
        os.close(fd)
    except OSError:
        pass


def _worktree_member(root: Path, encoded: bytes) -> tuple[int, str] | None:
    """Open a no-symlink parent inside ``root`` for one Git-relative filename."""
    parent: int | None = None
    try:
        relative = Path(os.fsdecode(encoded))
        if relative.is_absolute() or not relative.parts or any(
                component in ("", ".", "..") for component in relative.parts):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW | os.O_DIRECTORY
        parent = os.open(root, flags)
        for component in relative.parts[:-1]:
            child = os.open(component, flags, dir_fd=parent)
            _close_fd(parent)
            parent = child
    except (OSError, UnicodeError, ValueError):
        if parent is not None:
            _close_fd(parent)
        return None
    return parent, relative.name


def _worktree_snapshot_child(working_dir: str, write_fd: int) -> None:
    """Write one bounded digest of Git-enforced changed and untracked worktree state."""
    if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY")):
        return
    try:
        root = Path(working_dir).resolve(strict=True)
    except (OSError, ValueError):
        return
    if not root.is_dir():
        return
    process = subprocess.Popen(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z",
         "--untracked-files=all", "--ignored=no", "--", ".",
         ":(exclude).agentflow", ":(exclude).agentflow/**"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdout is not None
    raw = process.stdout.read(_WORKTREE_STATUS_BYTES + 1)
    if len(raw) > _WORKTREE_STATUS_BYTES:
        process.kill()
        process.wait()
        return
    if process.wait() != 0 or (paths := _status_paths(raw)) is None:
        return
    process = subprocess.Popen(
        ["git", "-c", "diff.renames=false", "-C", str(root), "diff", "--no-ext-diff",
         "--numstat", "-z", "HEAD", "--", ".", ":(exclude).agentflow",
         ":(exclude).agentflow/**"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdout is not None
    raw = process.stdout.read(_WORKTREE_STATUS_BYTES + 1)
    if len(raw) > _WORKTREE_STATUS_BYTES:
        process.kill()
        process.wait()
        return
    if process.wait() != 0 or (content_paths := _numstat_paths(raw)) is None:
        return

    digest = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    for status, encoded, prior_path in paths:
        member = _worktree_member(root, encoded)
        if member is None:
            return
        prior_member = _worktree_member(root, prior_path) if prior_path is not None else None
        if prior_path is not None and prior_member is None:
            _close_fd(member[0])
            return
        parent_fd, name = member
        try:
            if prior_member is not None:
                _close_fd(prior_member[0])
                prior_member = None
            structural_change = any(change in status for change in b"ADRC")
            if status != b"??" and encoded not in content_paths and not structural_change:
                continue
            record = hashlib.sha256()
            _hash_frame(record, b"path", encoded)
            if prior_path is not None:
                _hash_frame(record, b"prior-path", prior_path)
            try:
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if b"D" not in status:
                    return
                _hash_frame(digest, b"deletion-record", record.digest())
                continue
            except (OSError, ValueError):
                return
            if stat.S_ISLNK(before.st_mode):
                try:
                    target = os.fsencode(os.readlink(name, dir_fd=parent_fd))
                except OSError:
                    return
                if len(target) > _WORKTREE_FILE_BYTES:
                    return
                _hash_frame(record, b"symlink-target", target)
                total += len(target)
            elif stat.S_ISREG(before.st_mode):
                if before.st_size > _WORKTREE_FILE_BYTES:
                    return
                try:
                    fd = os.open(name, flags, dir_fd=parent_fd)
                    try:
                        chunks: list[bytes] = []
                        remaining = _WORKTREE_FILE_BYTES + 1
                        while remaining:
                            chunk = os.read(fd, min(64 * 1024, remaining))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            remaining -= len(chunk)
                        after = os.fstat(fd)
                    finally:
                        os.close(fd)
                except OSError:
                    return
                content = b"".join(chunks)
                if (len(content) > _WORKTREE_FILE_BYTES or before.st_ino != after.st_ino
                        or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns):
                    return
                _hash_frame(record, b"file-content", content)
                total += len(content)
            else:
                return
            if total > _WORKTREE_TOTAL_BYTES:
                return
            record_type = (b"untracked-record" if status == b"??" else
                           b"rename-record" if b"R" in status else
                           b"copy-record" if b"C" in status else
                           b"addition-record" if b"A" in status else
                           b"content-record")
            _hash_frame(digest, record_type, record.digest())
        finally:
            if prior_member is not None:
                _close_fd(prior_member[0])
            _close_fd(parent_fd)
    os.write(write_fd, digest.digest())


def _worktree_snapshot(working_dir: str,
                       timeout: float = _WORKTREE_OBSERVATION_S) -> bytes | None:
    """Observe durable Git worktree state in a killable, bounded process group."""
    if not working_dir:
        return None
    if not _reap_worktree_helpers():
        return None
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        return None
    try:
        pid = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        return None
    if pid == 0:
        os.close(read_fd)
        try:
            os.setpgid(0, 0)
            _worktree_snapshot_child(working_dir, write_fd)
        except (OSError, ValueError):
            pass
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    cleanup_ok = True
    helper_finished = False
    observation_deadline = time.monotonic() + timeout
    try:
        try:
            os.setpgid(pid, pid)
        except OSError:
            pass
        ready, _, _ = select.select(
            [read_fd], [], [], max(0.0, observation_deadline - time.monotonic()))
        raw = os.read(read_fd, 33) if ready else b""
        if len(raw) == hashlib.sha256().digest_size:
            ready, _, _ = select.select(
                [read_fd], [], [], max(0.0, observation_deadline - time.monotonic()))
            helper_finished = bool(ready) and os.read(read_fd, 1) == b""
    except (OSError, ValueError):
        raw = b""
    finally:
        try:
            os.close(read_fd)
        except OSError:
            cleanup_ok = False
        try:
            if helper_finished:
                waited, _ = os.waitpid(pid, 0)
                cleanup_ok = cleanup_ok and waited == pid
            else:
                cleanup_ok = False
                waited, _ = os.waitpid(pid, os.WNOHANG)
            if not helper_finished and waited == 0:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    cleanup_ok = False
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        cleanup_ok = False
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited == 0:
                    _WORKTREE_HELPERS.add(pid)
                    cleanup_ok = False
        except (ChildProcessError, OSError):
            cleanup_ok = False
    return raw if cleanup_ok and len(raw) == hashlib.sha256().digest_size else None


def _worktree_path(value: object, working_dir: str) -> bool:
    if not isinstance(value, str) or not value or not working_dir:
        return False
    root = Path(working_dir).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


class _ProgressStream:
    """Incrementally decode one provider's durable stream into canonical Build facts."""

    def __init__(self, provider: str, working_dir: str) -> None:
        self.provider = provider
        self.working_dir = working_dir
        self.offset = 0
        self.partial = b""
        self.discarding_oversize = False
        self.may_have_unread = False
        self.calls: dict[str, tuple[str, object] | None] = {}
        self.seen: set[str] = set()
        self.active_tests: dict[str, float] = {}
        self.active_workers: set[str] = set()

    def _remember(self, call_id: object, value: tuple[str, object], now: float) -> None:
        if not isinstance(call_id, str) or not call_id:
            return
        fact = f"{self.provider}:{value[0]}:{call_id}"
        if fact in self.seen:
            return
        if call_id not in self.calls:
            self.calls[call_id] = value
            if value[0] == "test":
                self.active_tests[call_id] = now
            elif value[0] == "worker":
                self.active_workers.add(call_id)
        elif self.calls[call_id] != value:
            # A provider id is canonical. Reusing it for a different action makes both
            # records ambiguous, so neither can renew or retain test supervision.
            self.calls[call_id] = None
            self.active_tests.pop(call_id, None)
            self.active_workers.discard(call_id)

    def _complete(self, call_id: object, expected: tuple[str, object], success: bool) -> bool:
        if not isinstance(call_id, str) or self.calls.get(call_id) != expected:
            return False
        self.active_tests.pop(call_id, None)
        self.active_workers.discard(call_id)
        fact = f"{self.provider}:{expected[0]}:{call_id}"
        if not success or fact in self.seen:
            return False
        self.seen.add(fact)
        return True

    def _codex(self, event: dict[str, object], now: float) -> bool:
        event_type = event.get("type")
        item = event.get("item")
        if event_type not in {"item.started", "item.completed"} or not isinstance(item, dict):
            return False
        call_id = item.get("id")
        item_type = item.get("type")
        if item_type == "command_execution":
            command = item.get("command")
            kind = ("test" if _recognized_test(command, "codex") else
                    "worker" if _recognized_worker(command) else "")
            expected = (kind, command)
            if event_type == "item.started":
                if (item.get("status") == "in_progress" and item.get("exit_code") is None
                        and kind):
                    self._remember(call_id, expected, now)
                return False
            if not isinstance(call_id, str) or self.calls.get(call_id) != expected:
                if isinstance(call_id, str):
                    self.active_tests.pop(call_id, None)
                    self.active_workers.discard(call_id)
                return False
            exit_code = item.get("exit_code")
            valid = (item.get("status") == "completed"
                     and isinstance(exit_code, int) and not isinstance(exit_code, bool))
            if not valid:
                self.active_tests.pop(call_id, None)
                self.active_workers.discard(call_id)
                self.calls[call_id] = None
                return False
            if kind == "worker":
                self._complete(call_id, expected, False)
                return False
            return self._complete(call_id, expected, exit_code == 0)
        if event_type != "item.completed" or item_type != "file_change":
            return False
        changes = item.get("changes")
        valid = (item.get("status") == "completed" and isinstance(changes, list) and changes
                 and all(isinstance(change, dict)
                         and change.get("kind") in {"add", "update", "delete"}
                         and self._local_change(change.get("path")) for change in changes))
        if not valid:
            return False
        expected = ("edit", tuple((change["path"], change["kind"]) for change in changes))
        self._remember(call_id, expected, now)
        return self._complete(call_id, expected, True)

    def _local_change(self, value: object) -> bool:
        return _worktree_path(value, self.working_dir)

    def _claude(self, event: dict[str, object], now: float) -> bool:
        message = event.get("message")
        if not isinstance(message, dict) or message.get("type") != "message":
            return False
        event_type = event.get("type")
        role = message.get("role")
        if (event_type, role) not in {("assistant", "assistant"), ("user", "user")}:
            return False
        content = message.get("content")
        if not isinstance(content, list):
            return False
        renewed = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if event_type == "assistant" and block.get("type") == "tool_use":
                name, payload = block.get("name"), block.get("input")
                if name == "Bash" and isinstance(payload, dict):
                    command = payload.get("command")
                    if _recognized_test(command, "claude"):
                        self._remember(block.get("id"), ("test", command), now)
                elif name in {"Edit", "Write", "NotebookEdit"} and isinstance(payload, dict):
                    path = payload.get("notebook_path" if name == "NotebookEdit" else "file_path")
                    if self._local_change(path):
                        self._remember(block.get("id"), ("edit", (name, path)), now)
            elif event_type == "user" and block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                call = self.calls.get(call_id) if isinstance(call_id, str) else None
                if call is None:
                    continue
                if block.get("is_error") is not False:
                    self.active_tests.pop(call_id, None)
                    self.calls[call_id] = None
                    continue
                renewed = self._complete(call_id, call, True) or renewed
        return renewed

    def poll(self, events: Path, *, silent_deadline: float, test_timeout: float,
             absolute_deadline: float) -> tuple[bool, float, bool, bool, bool]:
        """Return progress, fresh clock, backlog, expiry, and worker-state transition.

        A provider owns the append rate, so one observation reads at most 64 KiB, considers at
        most 128 complete records, and cooperatively yields after 10 ms. A single record is
        limited to 1 MiB; anything larger is discarded through its newline and fails closed.
        The effective lease deadline is checked immediately before and after every JSON decode.
        """
        decoder = {"codex": self._codex, "claude": self._claude}.get(self.provider)
        if decoder is None:
            return False, time.monotonic(), False, False, False

        def result(progressed: bool, observed_at: float,
                   expired: bool = False,
                   worker_changed: bool = False) -> tuple[bool, float, bool, bool, bool]:
            pending = b"\n" in self.partial or self.may_have_unread
            return progressed, observed_at, pending, expired, worker_changed

        def effective_deadline() -> float:
            if self.active_tests:
                test_deadline = min(self.active_tests.values()) + test_timeout
                return min(absolute_deadline, test_deadline)
            return min(absolute_deadline, silent_deadline)

        now = time.monotonic()
        slice_deadline = now + _EVENT_POLL_SLICE_S
        if now >= effective_deadline():
            return result(False, now)

        # Retained complete records are consumed before reading more, so a provider cannot make
        # the in-memory backlog grow while decoding is deliberately yielding between slices.
        if b"\n" not in self.partial:
            try:
                with events.open("rb") as stream:
                    stream.seek(self.offset)
                    chunk = stream.read(_EVENT_READ_BYTES)
            except OSError:
                return result(False, time.monotonic())
            self.offset += len(chunk)
            self.partial += chunk
            self.may_have_unread = len(chunk) == _EVENT_READ_BYTES
            now = time.monotonic()
            if now >= effective_deadline():
                return result(False, now)

        records = 0
        while records < _EVENT_RECORDS_PER_POLL:
            now = time.monotonic()
            if now >= effective_deadline() or (records and now >= slice_deadline):
                break
            newline = self.partial.find(b"\n")
            if newline < 0:
                if len(self.partial) > _EVENT_RECORD_BYTES:
                    self.partial = b""
                    self.discarding_oversize = True
                break
            raw = self.partial[:newline]
            if self.discarding_oversize:
                self.partial = self.partial[newline + 1:]
                self.discarding_oversize = False
                records += 1
                continue
            if len(raw) > _EVENT_RECORD_BYTES:
                self.partial = self.partial[newline + 1:]
                records += 1
                continue
            try:
                event = json.loads(raw)
            # Provider bytes are a cross-process trust boundary. CPython's JSON decoder raises
            # RecursionError for structurally valid but pathologically deep values; treat that
            # decoder failure like malformed JSON, without hiding memory/system/programmer faults.
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                self.partial = self.partial[newline + 1:]
                records += 1
                now = time.monotonic()
                if now >= effective_deadline():
                    break
                continue
            now = time.monotonic()
            if now >= effective_deadline():
                break
            self.partial = self.partial[newline + 1:]
            records += 1
            if isinstance(event, dict):
                # Decoder work can itself cross a lease edge and mutate test supervision. The
                # caller immediately ends the attempt against this pre-decode deadline, so late
                # progress is rejected and the mutated process-local state is never reused.
                decode_deadline = effective_deadline()
                workers_before = self.active_workers.copy()
                progressed = decoder(event, now)
                observed_at = time.monotonic()
                if observed_at >= decode_deadline:
                    return result(False, observed_at, True)
                if self.active_workers != workers_before:
                    return result(progressed, observed_at, worker_changed=True)
                if progressed:
                    return result(True, observed_at)
        return result(False, time.monotonic())


def _mark_active(working_dir: str) -> Path | None:
    """Mirror the detached supervisor pid into the legacy worktree-liveness marker.

    Startup recovery still consults this current-format marker before coordinator
    reconciliation. Keeping it for the supervisor's lifetime prevents that recovery pass from
    removing a clean coordinator-owned worktree while its provider is alive.
    """
    if not working_dir:
        return None
    from agentflow.runner import _active_marker
    marker = _active_marker(Path(working_dir))
    if marker is not None:
        marker.write_text(str(os.getpid()))
    return marker


def _clear_active(marker: Path | None) -> None:
    if marker is None:
        return
    try:
        if marker.read_text().strip() == str(os.getpid()):
            marker.unlink()
    except OSError:
        pass


def main(args: list[str]) -> None:
    store_path, identity, token, timeout, *tail = args
    build_lease: tuple[float, float, float] | None = None
    progress_provider = ""
    if tail[:1] == ["--build-lease"]:
        progress_provider, silent, test_grace, absolute, *tail = tail[1:]
        build_lease = (float(silent), float(test_grace), float(absolute))
    launch_root, *provider = tail
    if launch_root == _INHERITED_WORKTREE:
        try:
            working_dir = os.getcwd()
        except OSError:
            return
    elif launch_root == _NO_WORKTREE:
        working_dir = ""
    else:
        return
    # Double-fork: the intermediate exits immediately so the daemon reaps it at once, while
    # the detached supervisor is reparented to init and cannot zombie under the daemon.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    stop_requested = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    # Once child_start publishes this pid, reconciliation may signal it immediately. Install
    # the handler before that family becomes externally visible; it only records intent so a
    # request arriving inside store or process startup cannot strand an async exception there.
    signal.signal(signal.SIGTERM, request_stop)
    store = Store(store_path)
    won = store.child_start(identity, token, os.getpid())
    if not won:
        # Our reservation is gone; starting a provider now would be unreserved. Role-override
        # generation only ever happens further down, immediately before this supervisor's own
        # Popen call — nothing has been generated yet at this point, so there is nothing to
        # clean up here.
        store.close()
        os._exit(0)
    marker = _mark_active(working_dir)
    if not provider:
        store.close()
        _clear_active(marker)
        os._exit(0)  # dormant: no provider to become; a started-then-ended attempt
    # Remain as the recorded supervisor while the provider runs in its own session. A small
    # bootstrap gate prevents provider code from executing until that separate process group is
    # part of the durable family; if this supervisor vanishes first, pipe EOF makes the gate exit.
    # Output streams directly to its durable artifact, so partial output survives a daemon crash.
    events = events_path(store_path, token)
    events.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with events.open("w") as output:
        try:
            process = _spawn_provider(provider, output)
        except OSError:
            store.close()
            # No provider family ever came into existence for this attempt.
            write_result(store_path, token, exit_status=None, signal=None, timed_out=False)
            _clear_active(marker)
            os._exit(0)
        attached = store.child_provider_group(identity, token, os.getpid(), process.pid)
        store.close()
        if not attached:
            process.refuse()
            _clear_active(marker)
            os._exit(0)
        try:
            process.release()
        except OSError:
            pass

        def stop_provider() -> int:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Do not claim this attempt ended while its provider may still be running.
                # The event artifact is the durable operator-facing record of why this
                # supervisor remains until it can observe the provider's real exit.
                print("agentflow: permission denied stopping provider process group; "
                      "waiting for provider exit", file=output, flush=True)
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    print("agentflow: permission denied force-stopping provider process group; "
                          "waiting for provider exit", file=output, flush=True)
                return process.wait()

        # Reconciliation signals this supervisor. Turn that request into the same orderly
        # process-group shutdown the deadline path uses, then keep the supervisor alive to write
        # the provider's durable end facts.
        started_at = time.monotonic()
        deadline = started_at + float(timeout)
        silent_deadline = started_at + build_lease[0] if build_lease else deadline
        absolute_deadline = started_at + build_lease[2] if build_lease else deadline
        last_head = _head(working_dir) if build_lease else None
        head_poll_s = min(5.0, build_lease[0] / 4) if build_lease else 0
        next_head_poll = started_at + head_poll_s
        progress_stream = (_ProgressStream(progress_provider, working_dir)
                           if build_lease else None)
        worker_snapshot: bytes | None = None
        next_worker_poll = started_at
        while True:
            if stop_requested:
                returncode = stop_provider()
                break
            now = time.monotonic()
            if build_lease:
                active_test_deadline = (min(progress_stream.active_tests.values())
                                        + build_lease[1]
                                        if progress_stream.active_tests else None)
                lease_deadline = (min(absolute_deadline, active_test_deadline)
                                  if active_test_deadline is not None
                                  else min(absolute_deadline, silent_deadline))
                if now >= lease_deadline:
                    timed_out = True
                    returncode = stop_provider()
                    break
                if now >= next_head_poll:
                    head = _head(working_dir)
                    now = time.monotonic()
                    if now >= lease_deadline:
                        timed_out = True
                        returncode = stop_provider()
                        break
                    if head is not None and head != last_head:
                        last_head = head
                        silent_deadline = now + build_lease[0]
                    next_head_poll = now + head_poll_s
                (event_progressed, now, events_pending, lease_expired,
                 worker_changed) = progress_stream.poll(
                    events, silent_deadline=silent_deadline, test_timeout=build_lease[1],
                    absolute_deadline=absolute_deadline)
                if lease_expired or now >= absolute_deadline:
                    timed_out = True
                    returncode = stop_provider()
                    break
                if event_progressed:
                    silent_deadline = now + build_lease[0]
                if (progress_stream.active_workers and
                        (worker_changed or now >= next_worker_poll)):
                    snapshot = _worktree_snapshot(working_dir)
                    now = time.monotonic()
                    (snapshot_progressed, now, events_pending, lease_expired,
                     snapshot_worker_changed) = progress_stream.poll(
                        events, silent_deadline=silent_deadline,
                        test_timeout=build_lease[1], absolute_deadline=absolute_deadline)
                    worker_changed = worker_changed or snapshot_worker_changed
                    active_test_deadline = (min(progress_stream.active_tests.values())
                                            + build_lease[1]
                                            if progress_stream.active_tests else None)
                    observation_deadline = (
                        min(absolute_deadline, active_test_deadline)
                        if active_test_deadline is not None
                        else min(absolute_deadline, silent_deadline))
                    if lease_expired or now >= observation_deadline:
                        timed_out = True
                        returncode = stop_provider()
                        break
                    if snapshot_progressed and not events_pending:
                        silent_deadline = now + build_lease[0]
                    if progress_stream.active_workers and not events_pending:
                        if (worker_snapshot is not None and snapshot is not None
                                and snapshot != worker_snapshot):
                            silent_deadline = now + build_lease[0]
                        worker_snapshot = snapshot
                    else:
                        worker_snapshot = None
                    next_worker_poll = now + head_poll_s
                if not progress_stream.active_workers and worker_changed:
                    worker_snapshot = None
                test_deadline = min(progress_stream.active_tests.values(), default=0) + build_lease[1]
                deadline = (min(absolute_deadline, test_deadline)
                            if progress_stream.active_tests
                            else min(absolute_deadline, silent_deadline))
                if now >= deadline:
                    timed_out = True
                    returncode = stop_provider()
                    break
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                returncode = stop_provider()
                break
            if build_lease and events_pending:
                returncode = process.poll()
                if returncode is not None:
                    now = time.monotonic()
                    if now >= deadline:
                        timed_out = True
                    break
                continue
            try:
                wait_timeout = min(remaining, 0.1)
                if build_lease and progress_stream.active_workers:
                    wait_timeout = min(wait_timeout, max(0.001, next_worker_poll - now))
                returncode = process.wait(timeout=wait_timeout)
                now = time.monotonic()
                if build_lease and now >= deadline:
                    timed_out = True
                break
            except subprocess.TimeoutExpired:
                pass
        output.flush()
        os.fsync(output.fileno())
    ended_by_signal = -returncode if returncode < 0 else None
    exit_status = returncode if returncode >= 0 else None
    write_result(store_path, token, exit_status=exit_status,
                 signal=ended_by_signal, timed_out=timed_out)
    _clear_active(marker)
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
