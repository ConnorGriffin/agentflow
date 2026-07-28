"""The public AgentFlow command."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from agentflow import daemon
from agentflow.config import ConfigurationError, load_config


def _state_dir() -> Path:
    return Path(os.environ.get("AGENTFLOW_STATE", "~/.agentflow")).expanduser()


def _daemon_running(state: Path) -> bool:
    try:
        pid = int((state / "daemon.lock" / "pid").read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return pid > 0


def main(argv: list[str] | None = None) -> None:
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


if __name__ == "__main__":
    main()
