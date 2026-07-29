"""Install the persistent daemon as a per-user macOS LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


LABEL = "agentflow.daemon"


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


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def install(config: Path) -> None:
    """Write and reload the per-user daemon service using explicit runtime paths."""
    home = Path.home()
    agents = home / "Library" / "LaunchAgents"
    logs = home / "Library" / "Logs"
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    plist_path = _plist_path()

    environment = {
        "AGENTFLOW_CONFIG": str(config.resolve()),
        "AGENTFLOW_STATE": str(_runtime_path("AGENTFLOW_STATE", "~/.agentflow")),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
    }
    helper = os.environ.get("AGENTFLOW_CAPACITY_HELPER")
    if helper:
        environment["AGENTFLOW_CAPACITY_HELPER"] = str(
            Path(helper).expanduser().resolve()
        )

    service = {
        "Label": LABEL,
        "ProgramArguments": [str(_executable()), "daemon"],
        "KeepAlive": True,
        "ProcessType": "Background",
        "EnvironmentVariables": environment,
        "StandardOutPath": str(logs / "agentflow.log"),
        "StandardErrorPath": str(logs / "agentflow.log"),
    }
    plist_path.write_bytes(plistlib.dumps(service))
    plist_path.chmod(0o644)

    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", _target()],
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
        raise ServiceError(f"launchctl bootstrap failed: {detail}")


def remove() -> None:
    """Stop the daemon service and remove only its generated LaunchAgent."""
    subprocess.run(
        ["launchctl", "bootout", _target()],
        text=True,
        capture_output=True,
        check=False,
    )
    _plist_path().unlink(missing_ok=True)
