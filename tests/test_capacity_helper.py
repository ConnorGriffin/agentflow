from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_builtin_helper_reports_the_latest_codex_limit_windows(tmp_path):
    sessions = tmp_path / "home" / ".codex" / "sessions" / "2026" / "07" / "28"
    sessions.mkdir(parents=True)
    (sessions / "rollout.jsonl").write_text(
        "\n".join(
            [
                "not JSON",  # ignored like a damaged Codex rollout record in production
                json.dumps(
                    {
                        "timestamp": "2026-07-28T11:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 10,
                                    "window_minutes": 300,
                                    "resets_at": 200,
                                }
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-28T12:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 25,
                                    "window_minutes": 300,
                                    "resets_at": 300,
                                },
                                "secondary": {
                                    "used_percent": 40,
                                    "window_minutes": 10080,
                                    "resets_at": 900,
                                },
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    result = subprocess.run(
        ["uv", "run", "agentflow-capacity-helper", "limits"],
        cwd=ROOT,
        env=os.environ
        | {
            "HOME": str(tmp_path / "home"),
            "TRIAGE_AGENT": "codex",
        },
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "observed_at": 1785240000.0,
        "windows": [
            {
                "resets_at": 300,
                "used_percent": 25,
                "window_minutes": 300,
            },
            {
                "resets_at": 900,
                "used_percent": 40,
                "window_minutes": 10080,
            },
        ],
    }


def test_builtin_helper_calibrates_and_reports_claude_five_hour_usage(tmp_path):
    projects = tmp_path / "home" / ".claude" / "projects" / "project"
    projects.mkdir(parents=True)
    now = time.time()

    def event(seconds_ago: int, tokens: int) -> str:
        timestamp = datetime.fromtimestamp(now - seconds_ago, UTC).isoformat()
        return json.dumps(
            {
                "timestamp": timestamp,
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": tokens,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 0,
                    }
                },
            }
        )

    (projects / "session.jsonl").write_text(
        "\n".join(
            [
                event(12 * 3600, 100),
                event(11 * 3600, 100),
                event(1 * 3600, 100),
            ]
        )
        + "\n"
    )
    state = tmp_path / "state"
    env = os.environ | {
        "HOME": str(tmp_path / "home"),
        "AGENTFLOW_STATE": str(state),
        "TRIAGE_AGENT": "claude",
        "TRIAGE_SKIP_ACTIVITY": "1",
    }

    calibrated = subprocess.run(
        ["uv", "run", "agentflow", "capacity", "calibrate"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    checked = subprocess.run(
        ["uv", "run", "agentflow-capacity-helper", "check"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert calibrated.returncode == 0, calibrated.stderr
    assert calibrated.stdout.strip() == "calibrated: Claude five-hour peak = 200"
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == (
        "clear: trailing-5h spend at 50% of calibrated peak"
    )
    assert json.loads((state / "capacity.json").read_text()) == {
        "claude_peak_5h": 200
    }


def test_builtin_helper_distinguishes_recent_claude_operator_activity(tmp_path):
    projects = tmp_path / "home" / ".claude" / "projects"
    history = projects / "old-project"
    history.mkdir(parents=True)
    timestamp = datetime.fromtimestamp(time.time() - 3600, UTC).isoformat()
    transcript = history / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 0,
                    }
                },
            }
        )
        + "\n"
    )
    old = time.time() - 3600
    os.utime(transcript, (old, old))
    active = projects / "operator-project"
    active.mkdir()
    (active / "session.jsonl").write_text("{}\n")
    env = os.environ | {
        "HOME": str(tmp_path / "home"),
        "AGENTFLOW_STATE": str(tmp_path / "state"),
        "TRIAGE_AGENT": "claude",
    }
    subprocess.run(
        ["uv", "run", "agentflow-capacity-helper", "calibrate"],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )

    blocked = subprocess.run(
        ["uv", "run", "agentflow-capacity-helper", "check"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    visible = subprocess.run(
        ["uv", "run", "agentflow-capacity-helper", "check"],
        cwd=ROOT,
        env=env | {"TRIAGE_SKIP_ACTIVITY": "1"},
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert blocked.returncode == 1
    assert blocked.stdout.strip() == "blocked: a Claude session was recently active"
    assert visible.returncode == 0, visible.stderr
    assert visible.stdout.startswith("clear: trailing-5h spend at ")


def test_builtin_helper_ignores_agentflow_codex_sessions_but_blocks_the_operator(
    tmp_path,
):
    sessions = tmp_path / "home" / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    timestamp = datetime.now(UTC).isoformat()

    def write_session(path: Path, cwd: str) -> None:
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "type": "session_meta",
                            "payload": {"cwd": cwd},
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "rate_limits": {
                                    "primary": {
                                        "used_percent": 20,
                                        "window_minutes": 300,
                                        "resets_at": time.time() + 3600,
                                    }
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )

    own = sessions / "agentflow.jsonl"
    operator = sessions / "operator.jsonl"
    write_session(own, "/repo/.agentflow/worktrees/codex/issue-1")
    write_session(operator, "/repo")
    env = os.environ | {
        "HOME": str(tmp_path / "home"),
        "TRIAGE_AGENT": "codex",
    }

    blocked = subprocess.run(
        ["uv", "run", "agentflow-capacity-helper", "check"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    operator.unlink()
    clear = subprocess.run(
        ["uv", "run", "agentflow-capacity-helper", "check"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert blocked.returncode == 1
    assert blocked.stdout.strip() == "blocked: a Codex session was recently active"
    assert clear.returncode == 0, clear.stderr
    assert clear.stdout.strip() == "clear: Codex limit facts available"


def test_builtin_helper_explains_missing_claude_calibration(tmp_path):
    result = subprocess.run(
        ["uv", "run", "agentflow-capacity-helper", "check"],
        cwd=ROOT,
        env=os.environ
        | {
            "HOME": str(tmp_path / "home"),
            "AGENTFLOW_STATE": str(tmp_path / "state"),
            "TRIAGE_AGENT": "claude",
            "TRIAGE_SKIP_ACTIVITY": "1",
        },
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stdout.strip() == (
        "blocked: Claude capacity is not calibrated "
        "(run: agentflow capacity calibrate)"
    )
