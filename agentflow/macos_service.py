"""Install the persistent daemon and read-only console as per-user macOS LaunchAgents.

Both processes come from the same installed checkout and environment, but stay separate
supervised jobs (ADR 0051's runtime-checkout model, extended to the console): the daemon
owns dispatch, the console only ever reads what the daemon publishes and binds to
``127.0.0.1:8788`` (ADR 0035, ADR 0026). Installing or removing the service always acts on
both jobs together; pausing cold submission does not touch either.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


DAEMON_LABEL = "agentflow.daemon"
CONSOLE_LABEL = "agentflow.console"
CONSOLE_HOST = "127.0.0.1"
CONSOLE_PORT = 8788


class ServiceError(RuntimeError):
    """The daemon service could not be installed."""


def _executable() -> Path:
    invoked = Path(sys.argv[0]).expanduser()
    if invoked.parent != Path("."):
        return invoked.resolve()
    found = shutil.which(str(invoked))
    if found is None:
        raise ServiceError(f"cannot resolve installed executable: {invoked}")
    return Path(found).resolve()


def _runtime_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _bootstrap(label: str, plist_path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", _target(label)],
        text=True,
        capture_output=True,
        check=False,
    )
    loaded = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if loaded.returncode != 0:
        detail = loaded.stderr.strip() or loaded.stdout.strip() or "unknown error"
        raise ServiceError(f"launchctl bootstrap failed for {label}: {detail}")


def _write_service(label: str, program_args: list[str], environment: dict, log_name: str) -> Path:
    logs = Path.home() / "Library" / "Logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist_path = _plist_path(label)
    service = {
        "Label": label,
        "ProgramArguments": program_args,
        "KeepAlive": True,
        "ProcessType": "Background",
        "EnvironmentVariables": environment,
        "StandardOutPath": str(logs / log_name),
        "StandardErrorPath": str(logs / log_name),
    }
    plist_path.write_bytes(plistlib.dumps(service))
    plist_path.chmod(0o644)
    return plist_path


def install(config: Path) -> None:
    """Write and reload the per-user daemon and console services with explicit runtime paths.

    Both jobs restart on unexpected exit (``KeepAlive``) and are reloaded together so a
    stale copy of either is never left running after an install."""
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    executable = str(_executable())
    state = str(_runtime_path("AGENTFLOW_STATE", "~/.agentflow"))
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")

    daemon_environment = {
        "AGENTFLOW_CONFIG": str(config.resolve()),
        "AGENTFLOW_STATE": state,
        "PATH": path,
    }
    helper = os.environ.get("AGENTFLOW_CAPACITY_HELPER")
    if helper:
        daemon_environment["AGENTFLOW_CAPACITY_HELPER"] = str(
            Path(helper).expanduser().resolve()
        )
    daemon_plist = _write_service(
        DAEMON_LABEL,
        [executable, "daemon"],
        daemon_environment,
        "agentflow.log",
    )

    console_environment = {
        "AGENTFLOW_STATE": state,
        "PATH": path,
    }
    console_plist = _write_service(
        CONSOLE_LABEL,
        [executable, "console"],
        console_environment,
        "agentflow-console.log",
    )

    _bootstrap(DAEMON_LABEL, daemon_plist)
    _bootstrap(CONSOLE_LABEL, console_plist)


def remove() -> None:
    """Stop both services and remove only their generated LaunchAgents."""
    for label in (DAEMON_LABEL, CONSOLE_LABEL):
        subprocess.run(
            ["launchctl", "bootout", _target(label)],
            text=True,
            capture_output=True,
            check=False,
        )
        _plist_path(label).unlink(missing_ok=True)


def probe_console(
    host: str = CONSOLE_HOST, port: int = CONSOLE_PORT, timeout: float = 2.0
) -> bool:
    """Confirm the console answers ``GET /api/snapshot`` on its loopback port.

    A distinct process-liveness fact from the daemon's dispatch pause/resume state: the
    console can be down while dispatch is either paused or resumed, and vice versa."""
    url = f"http://{host}:{port}/api/snapshot"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
