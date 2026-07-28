"""The public AgentFlow command."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def _state_dir() -> Path:
    return Path(os.environ.get("AGENTFLOW_STATE", "~/.agentflow")).expanduser()


def _daemon_running(state: Path) -> bool:
    try:
        pid = int((state / "daemon.lock" / "pid").read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return pid > 0


def _configure_capacity_helper() -> None:
    if os.environ.get("AGENTFLOW_CAPACITY_HELPER") or os.environ.get(
        "AGENTFLOW_TRIAGE_GATE"
    ):
        return
    adjacent = Path(sys.executable).with_name("agentflow-capacity-helper")
    found = str(adjacent) if adjacent.is_file() else shutil.which(
        "agentflow-capacity-helper"
    )
    if found:
        os.environ["AGENTFLOW_CAPACITY_HELPER"] = str(Path(found).resolve())


def main(argv: list[str] | None = None) -> None:
    _configure_capacity_helper()
    from agentflow.config import ConfigurationError, load_config

    parser = argparse.ArgumentParser(prog="agentflow")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate runtime configuration")
    check.add_argument("--config", help="path to config.toml")
    daemon_command = commands.add_parser("daemon", help="run the fleet daemon")
    daemon_command.add_argument("--config", help="path to config.toml")
    daemon_command.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit",
    )
    commands.add_parser("resume", help="allow new cold submissions")
    commands.add_parser("pause", help="stop new cold submissions")
    commands.add_parser("status", help="show submission and daemon state")
    commands.add_parser("console", help="serve the operator console")
    service = commands.add_parser("service", help="manage the macOS daemon service")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    service_install = service_commands.add_parser(
        "install", help="install or reload the daemon service"
    )
    service_install.add_argument("--config", help="path to config.toml")
    service_commands.add_parser("remove", help="stop and remove the daemon service")
    capacity = commands.add_parser("capacity", help="manage local provider facts")
    capacity_commands = capacity.add_subparsers(
        dest="capacity_command", required=True
    )
    capacity_commands.add_parser(
        "calibrate", help="calibrate Claude usage from local history"
    )
    args = parser.parse_args(argv)

    if args.command == "check":
        try:
            config = load_config(args.config)
        except ConfigurationError as exc:
            parser.error(str(exc))
        count = len(config.repositories)
        workspace_count = len(config.workspace_repositories)
        noun = "repository" if count == 1 else "repositories"
        print(
            f"configuration valid: {count} {noun} "
            f"({workspace_count} workspace)"
        )
    elif args.command == "daemon":
        from agentflow import daemon

        try:
            config = load_config(args.config)
        except ConfigurationError as exc:
            parser.error(str(exc))
        daemon.run(config, once=args.once)
    elif args.command == "resume":
        state = _state_dir()
        state.mkdir(parents=True, exist_ok=True)
        (state / "enabled").touch()
        print("cold submission resumed")
    elif args.command == "pause":
        (_state_dir() / "enabled").unlink(missing_ok=True)
        print("cold submission paused")
    elif args.command == "status":
        state = _state_dir()
        print(
            "cold submission: "
            + ("enabled" if (state / "enabled").exists() else "paused")
        )
        print("daemon: " + ("running" if _daemon_running(state) else "stopped"))
    elif args.command == "console":
        from agentflow import webapp

        webapp.main()
    elif args.command == "service" and args.service_command == "install":
        from agentflow.macos_service import ServiceError, install

        try:
            config = load_config(args.config)
            install(config.path)
        except (ConfigurationError, ServiceError) as exc:
            parser.error(str(exc))
        print("daemon service installed and running")
    elif args.command == "service" and args.service_command == "remove":
        from agentflow.macos_service import remove

        remove()
        print("daemon service removed")
    elif args.command == "capacity" and args.capacity_command == "calibrate":
        from agentflow.capacity_helper import main as capacity_main

        os.environ["TRIAGE_AGENT"] = "claude"
        capacity_main(["calibrate"])


if __name__ == "__main__":
    main()
