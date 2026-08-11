"""The public AgentFlow command."""

from __future__ import annotations

import argparse
import json
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


def main(argv: list[str] | None = None) -> int | None:
    _configure_capacity_helper()
    from agentflow.config import ConfigurationError, load_config

    parser = argparse.ArgumentParser(prog="agentflow")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate runtime configuration")
    check.add_argument("--config", help="path to config.toml")
    doctor_command = commands.add_parser(
        "doctor", help="check a repository's AgentFlow capabilities"
    )
    doctor_command.add_argument("path", nargs="?", default=None)
    doctor_command.add_argument("--repo", dest="repo_path")
    doctor_command.add_argument("--json", action="store_true", dest="json_output")
    enroll_command = commands.add_parser(
        "enroll", help="configure a repository for reproducible AgentFlow use"
    )
    enroll_command.add_argument("path", nargs="?", default=".")
    enroll_command.add_argument("--apply", action="store_true")
    enroll_command.add_argument(
        "--profile",
        choices=("autonomous", "reviewed", "guarded"),
        default="reviewed",
    )
    enroll_command.add_argument(
        "--audit", action="store_true",
        help="print the fleet-wide UI-surface declaration census and exit",
    )
    enroll_command.add_argument(
        "--sync", action="store_true",
        help="sweep the configured fleet, converging drift (pass --apply to write and PR it)",
    )
    churn_command = commands.add_parser(
        "churn", help="rank fleet transcripts by token-burning churn"
    )
    churn_command.add_argument("--config", help="path to config.toml")
    churn_command.add_argument("--repo", dest="repo_filter",
                                help="only this configured owner/name repository")
    churn_command.add_argument("--top", type=int, default=25)
    churn_command.add_argument("--json", dest="json_path",
                                help="also write full per-session metrics to this file")
    churn_command.add_argument("--include-manual", action="store_true",
                                help="also include manual (non-fleet-spawned) worktree sessions")
    churn_command.add_argument("--excerpt", dest="excerpt_path",
                                help="print the error windows of one Claude session JSONL "
                                     "instead of the ranking")
    churn_command.add_argument("--max-chars", type=int, default=9000)
    churn_command.add_argument("--window", type=int, default=1)
    spend_command = commands.add_parser(
        "spend", help="report per-stage spend by attributed model")
    spend_command.add_argument("--from", dest="spend_from", required=True,
                               help="UTC start date, inclusive (YYYY-MM-DD)")
    spend_command.add_argument("--to", dest="spend_to", required=True,
                               help="UTC end date, exclusive (YYYY-MM-DD)")
    floodgates_command = commands.add_parser(
        "floodgates", help="fleet-wide headroom override (ADR 0025 amendment)"
    )
    floodgates_commands = floodgates_command.add_subparsers(
        dest="floodgates_command", required=True
    )
    floodgates_commands.add_parser("open", help="lift the weekly allowance and spend ceiling")
    floodgates_commands.add_parser("close", help="restore the ordinary headroom policy")
    floodgates_commands.add_parser("status", help="show whether floodgates is open")
    probe_command = commands.add_parser(
        "decision-map-probe",
        help="print the two Decision Map reads for one repository, with their point cost",
    )
    probe_command.add_argument("--repo", required=True, help="OWNER/REPO to read")
    daemon_command = commands.add_parser("daemon", help="run the fleet daemon")
    daemon_command.add_argument("--config", help="path to config.toml")
    daemon_command.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit",
    )
    commands.add_parser("resume", help="allow new cold submissions")
    commands.add_parser("pause", help="stop new cold submissions")
    from agentflow.pool_control import POOLS

    pool_command = commands.add_parser(
        "pool", help="pause or resume one provider pool"
    )
    pool_commands = pool_command.add_subparsers(
        dest="pool_command", required=True
    )
    for action in ("pause", "resume", "status"):
        pool_action = pool_commands.add_parser(
            action, help=f"{action} one provider pool"
        )
        pool_action.add_argument("pool", choices=POOLS)
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
    elif args.command == "doctor":
        from agentflow.enroll import doctor, print_doctor

        report = doctor(args.repo_path or args.path or ".")
        print_doctor(report, json_output=args.json_output)
        return 0 if report.ready else 1
    elif args.command == "churn":
        from agentflow.churn import collect_sessions, format_excerpt, format_report

        if args.excerpt_path:
            print(format_excerpt(args.excerpt_path, max_chars=args.max_chars,
                                  window=args.window))
            return 0
        try:
            config = load_config(args.config)
        except ConfigurationError as exc:
            parser.error(str(exc))
        repos = [
            cfg for cfg in config.repositories
            if not args.repo_filter or cfg.repo == args.repo_filter
        ]
        if not repos:
            parser.error(f"no configured repository matches {args.repo_filter!r}")
        sessions = collect_sessions(repos, include_manual=args.include_manual)
        print(format_report(sessions, top=args.top))
        if args.json_path:
            with open(args.json_path, "w") as fh:
                json.dump(sessions, fh, indent=1)
            print(f"wrote {args.json_path}")
        return 0
    elif args.command == "spend":
        from datetime import date

        from agentflow.coordinator.store import default_store_path
        from agentflow.coordinator.telemetry import format_spend_report, spend_report

        try:
            start = date.fromisoformat(args.spend_from)
            end = date.fromisoformat(args.spend_to)
            report = spend_report(default_store_path(), start=start, end=end)
        except ValueError as exc:
            parser.error(str(exc))
        print(format_spend_report(report))
        return 0
    elif args.command == "enroll":
        from agentflow.enroll import _audit_command, configured_repositories, enroll_repository, sync_fleet

        if args.audit:
            try:
                _audit_command()
            except ConfigurationError as exc:
                parser.error(str(exc))
            return 0
        if args.sync:
            try:
                repos = configured_repositories()
            except ConfigurationError as exc:
                parser.error(str(exc))
            return sync_fleet(repos, apply=args.apply)
        try:
            report = enroll_repository(
                args.path, apply=args.apply, profile=args.profile
            )
        except ValueError as exc:
            parser.error(str(exc))
        return 0 if report.ready else 1
    elif args.command == "decision-map-probe":
        from agentflow.operator_projection import decision_map_probe

        print(decision_map_probe(args.repo))
        return 0
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
    elif args.command == "pool":
        from agentflow.pool_control import pause_pool, pool_paused, resume_pool

        if args.pool_command == "pause":
            pause_pool(args.pool)
        elif args.pool_command == "resume":
            resume_pool(args.pool)
        state = "PAUSED" if pool_paused(args.pool) else "ACTIVE"
        print(f"{args.pool} pool {state}")
    elif args.command == "status":
        from agentflow.macos_service import probe_console

        state = _state_dir()
        print(
            "cold submission: "
            + ("enabled" if (state / "enabled").exists() else "paused")
        )
        print("daemon: " + ("running" if _daemon_running(state) else "stopped"))
        print("console: " + ("reachable" if probe_console() else "unreachable"))
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
    elif args.command == "floodgates":
        import time

        from agentflow.balancer import floodgates_active
        from agentflow.state import state_path

        flag = state_path("floodgates")
        if args.floodgates_command == "open":
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(f"{int(time.time())}\n")
            print("floodgates OPEN")
        elif args.floodgates_command == "close":
            flag.unlink(missing_ok=True)
            print("floodgates CLOSED")
        elif args.floodgates_command == "status":
            env = os.environ.get("AGENTFLOW_FLOODGATES", "").strip().lower() in (
                "1", "true", "yes")
            if not floodgates_active():
                print("floodgates CLOSED")
            else:
                print(f"floodgates OPEN (source: {'env' if env else 'file'})")
    elif args.command == "capacity" and args.capacity_command == "calibrate":
        from agentflow.capacity_helper import main as capacity_main

        os.environ["TRIAGE_AGENT"] = "claude"
        capacity_main(["calibrate"])


if __name__ == "__main__":
    main()
