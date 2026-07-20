"""Coordinator-only dispatch: submission, pause/drain, and deletion guards (issue #109)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import dispatch, loop
from agentflow.loop import RepoConfig


def test_stage_caps_remain_named_inputs_to_the_coordinator_gate():
    assert dispatch.STAGE_CAPS == {"triage": 3, "build": 2, "mockup": 1, "respond": 1,
                                   "research": 1}
    assert dispatch.MACHINE_CEILING > 0


def test_paused_cycle_submits_nothing_but_still_reconciles(monkeypatch):
    monkeypatch.setattr(dispatch, "_submit_repo", lambda *a: pytest.fail(
        "pause may not submit cold work"))
    reconciled = []
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project",
                        lambda coord, _log=None: reconciled.append(coord))
    claims = []
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_orphaned_claims",
                        lambda cfg, _log=None: claims.append(cfg.repo))
    coord = object()

    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], submit_new=False,
                       coordinator=coord, _log=lambda _line: None)

    assert reconciled == [coord]
    assert claims == ["o/r"]


def test_active_cycle_submits_each_repo_then_reconciles_once(monkeypatch):
    submitted = []
    monkeypatch.setattr(dispatch, "_submit_repo",
                        lambda cfg, coord, log: submitted.append((cfg.repo, coord)))
    reconciled = []
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project",
                        lambda coord, _log=None: reconciled.append(coord))
    claims = []
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_orphaned_claims",
                        lambda cfg, _log=None: claims.append(cfg.repo))
    coord = object()

    dispatch.run_cycle([RepoConfig("o/a", "/a"), RepoConfig("o/b", "/b")],
                       coordinator=coord, _log=lambda _line: None)

    assert sorted(submitted) == [("o/a", coord), ("o/b", coord)]
    assert reconciled == [coord]
    assert sorted(claims) == ["o/a", "o/b"]


def test_orphaned_claim_is_cleared_only_after_durable_reconciliation(monkeypatch):
    from agentflow import coordinated_build, github

    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])
    # The four claim lanes are listed in order (building, triaging, drawing, resolving); only the
    # building lane holds a stale-claimed issue. The proof read back shows the label gone.
    listings = iter([[{"number": 7, "updatedAt": "2020-01-01T00:00:00Z"}], [], [], []])
    monkeypatch.setattr(github, "api", lambda args, *, parse_json=False: next(listings))
    removed = []
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: removed.append((issue, label)) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert removed == [(7, "agentflow:building")]


def test_unreadable_coordinator_state_clears_no_claim(monkeypatch):
    from agentflow import coordinated_build, github
    from agentflow.coordinator.store import StoreUnavailable

    monkeypatch.setattr(coordinated_build.tracer, "load_records",
                        lambda: (_ for _ in ()).throw(StoreUnavailable("locked")))
    monkeypatch.setattr(github, "api",
                        lambda *a, **k: pytest.fail("must not inspect or clear claims"))
    monkeypatch.setattr(github, "remove_label",
                        lambda *a, **k: pytest.fail("must not clear claims"))

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 0


def test_waiting_owner_retains_claim_but_settled_hold_does_not(monkeypatch):
    from agentflow import coordinated_build, github
    from agentflow.coordinator.record import HELD, WAITING, Record

    waiting = Record(identity="wait", stage="build", pool="claude", demand=5,
                     repo="o/r", subject="7", state=WAITING, claim=True)
    held = Record(identity="held", stage="review", pool="codex", demand=2,
                  repo="o/r", subject="8", state=HELD, claim=False)
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [waiting, held])
    # The building lane lists both issues; #7 is shielded by the live waiting build, #8 is not.
    listings = iter([[{"number": 7, "updatedAt": "2020-01-01T00:00:00Z"},
                      {"number": 8, "updatedAt": "2020-01-01T00:00:00Z"}], [], [], []])
    monkeypatch.setattr(github, "api", lambda args, *, parse_json=False: next(listings))
    removed = []
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: removed.append(issue) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert removed == [8]


def test_build_submission_enters_the_coordinator_then_claims_runnable_work(monkeypatch):
    from agentflow.coordinator.record import Record, WAITING

    issue = {"number": 7, "title": "Do it", "body": "brief",
             "labels": [{"name": "ready-for-agent"},
                        {"name": "agentflow:complexity:deep"},
                        {"name": "agentflow:effort:high"}]}
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: issue)
    builder = SimpleNamespace(tool="claude")
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (builder, None, ""))
    events = []
    monkeypatch.setattr(loop, "_claim", lambda repo, number: events.append("claim") or True)
    waiting = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                     state=WAITING)
    coord = SimpleNamespace(
        submit_stage=lambda submission: events.append(submission.stage) or "o/r|7|build|-",
        stage_record=lambda identity: waiting)

    assert "submitted" in dispatch._submit_coordinated_build(
        RepoConfig("o/r", "/tmp"), coord, None)
    # The submission enters the coordinator first; the issue is claimed only once admission has a
    # runnable record — never before, so a held no-op never stamps a false building claim (#245).
    assert events == ["build", "claim"]


def test_daemon_does_not_claim_or_launch_when_the_build_stays_held(monkeypatch):
    # After a maintainer `pickup` relabels an exhausted issue back to `ready-for-agent`, the daemon
    # can pick it — but it must not auto-resume the terminal held Build. An ordinary resubmission
    # reuses the held record, so the daemon claims nothing and reports the held state (#245).
    from agentflow.coordinator.record import Record, HELD

    issue = {"number": 7, "title": "Do it", "body": "brief",
             "labels": [{"name": "ready-for-agent"},
                        {"name": "agentflow:complexity:deep"},
                        {"name": "agentflow:effort:high"}]}
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: issue)
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(loop, "_claim", lambda *a: pytest.fail("must not claim a held no-op"))
    held = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                  state=HELD, claim=False)
    coord = SimpleNamespace(
        submit_stage=lambda submission: "o/r|7|build|-",
        stage_record=lambda identity: held)

    result = dispatch._submit_coordinated_build(RepoConfig("o/r", "/tmp"), coord, None)
    assert "held" in result and "submitted" not in result


def test_respond_waits_while_a_prior_change_record_owns_the_claim(monkeypatch):
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix", "please adjust", "cid-1", "base"))
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues",
                        lambda cfg, lane=None: {7})
    monkeypatch.setattr(loop, "_claim", lambda *a: pytest.fail("must not double-claim"))

    result = dispatch._submit_coordinated_respond(
        RepoConfig("o/r", "/tmp"), SimpleNamespace(), None)
    assert "prior change stage" in result


def test_intake_skips_an_issue_a_live_pipeline_stage_already_owns(monkeypatch):
    # A mid-pipeline issue whose triaging label was stripped by the reconciler but whose
    # downstream record still owns it must not be re-claimed by intake — the ownership guard
    # catches the label-already-stripped window (#201).
    from agentflow import coordinated_intake

    def candidate(cfg, reserved=frozenset()):
        return None if 42 in reserved else ({"number": 42, "labels": []}, "")

    monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues",
                        lambda cfg, lane=None: {42})
    monkeypatch.setattr(dispatch, "pick_pair",
                        lambda: pytest.fail("must not pick a pool for an owned issue"))
    monkeypatch.setattr(loop, "_claim_triage", lambda *a: pytest.fail("must not re-claim"))
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda *a, **k: pytest.fail("must not submit an owned issue"))

    result = dispatch._submit_coordinated_intake(RepoConfig("o/r", "/tmp"), SimpleNamespace(), None)
    assert result == "no un-triaged issues"


def test_intake_still_claims_a_genuinely_new_issue(monkeypatch):
    from agentflow import coordinated_intake

    def candidate(cfg, reserved=frozenset()):
        return None if 42 in reserved else ({"number": 42, "labels": []}, "")

    monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda *a, **k: SimpleNamespace(pool="claude"))
    claimed = []
    monkeypatch.setattr(loop, "_claim_triage", lambda repo, n: claimed.append(n) or True)
    coord = SimpleNamespace(submit_stage=lambda submission: None)

    result = dispatch._submit_coordinated_intake(RepoConfig("o/r", "/tmp"), coord, None)
    assert claimed == [42]
    assert "#42 → claude" in result


def test_live_board_is_overwritten_from_the_durable_projection(tmp_path, monkeypatch):
    from agentflow import live

    monkeypatch.setattr(live, "LIVE_FILE", tmp_path / "live.json")
    live.replace_projection([{"number": 9, "stage": "building"}])
    live.replace_projection([{"number": 10, "stage": "reviewing"}])
    assert live.running() == [{"number": 10, "stage": "reviewing"}]


def test_production_dispatch_has_no_legacy_bypass_or_second_counter():
    source = inspect.getsource(dispatch)
    assert "class Governor" not in source
    assert "launch_legacy" not in source
    assert "produce_once" not in source
    assert "respond_once" not in source
    assert "run_once" not in source
    assert "_live =" not in source and "_per_stage" not in source


def test_no_rollout_switch_or_direct_provider_call_survives_in_production_orchestration():
    root = Path(__file__).parents[1] / "agentflow"
    assert not (root / "coordinator" / "rollout.py").exists()
    production = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert ".launch(" not in production
    assert ".build(" not in production
    assert "MODE_LEGACY" not in production
    assert "class Governor" not in production
    assert "running_strict" not in production

    allowed_spawners = {root / "coordinator" / "launcher.py",
                        root / "coordinator" / "_launch_child.py"}
    allowed_subprocess_run = {root / "balancer.py", root / "notify.py", root / "runner.py"}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr == "Popen"):
                    assert path in allowed_spawners, f"provider-capable spawn outside launcher: {path}"
                if (isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr == "run"):
                    assert path in allowed_subprocess_run, f"subprocess.run outside adapters: {path}"
                if (isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                        and (node.func.attr.startswith("exec") or node.func.attr.startswith("spawn")
                             or node.func.attr == "fork")):
                    assert path in allowed_spawners, f"process start outside launcher: {path}"
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.List):
                first = node.args[0].elts[0] if node.args[0].elts else None
                if isinstance(first, ast.Constant) and first.value in {"claude", "codex"}:
                    assert path == root / "runner.py", f"direct provider command execution: {path}"
            if isinstance(node, ast.Call):
                counter_name = None
                if isinstance(node.func, ast.Name):
                    counter_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    counter_name = node.func.attr
                if counter_name in {"Semaphore", "BoundedSemaphore"}:
                    raise AssertionError(f"second capacity ledger primitive: {path}:{node.lineno}")
                if counter_name == "Counter":
                    assert path in {root / "coordinated_build.py", root / "dashboard_data.py"}, (
                        f"counter outside pacing/projection owners: {path}:{node.lineno}")
            if "coordinator" not in path.parts and isinstance(
                    node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [item.id for target in targets for item in ast.walk(target)
                         if isinstance(item, ast.Name)]
                assert not any("permit" in name.lower() for name in names), (
                    f"second permit ledger outside coordinator: {path}:{node.lineno}")
