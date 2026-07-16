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
    assert dispatch.STAGE_CAPS == {"triage": 3, "build": 2, "mockup": 1, "respond": 1}
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
    from agentflow import coordinated_build

    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[1:3] == ["issue", "list"]:
            label = argv[argv.index("--label") + 1]
            payload = ('[{"number":7,"updatedAt":"2020-01-01T00:00:00Z"}]'
                       if label == "agentflow:building" else "[]")
            return SimpleNamespace(returncode=0, stdout=payload)
        if argv[1:3] == ["issue", "view"]:
            return SimpleNamespace(returncode=0, stdout='{"labels":[]}')
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(loop, "_run", run)

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert any(argv[1:3] == ["issue", "edit"] for argv in calls)


def test_unreadable_coordinator_state_clears_no_claim(monkeypatch):
    from agentflow import coordinated_build
    from agentflow.coordinator.store import StoreUnavailable

    monkeypatch.setattr(coordinated_build.tracer, "load_records",
                        lambda: (_ for _ in ()).throw(StoreUnavailable("locked")))
    monkeypatch.setattr(loop, "_run", lambda argv: pytest.fail("must not inspect or clear claims"))

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 0


def test_waiting_owner_retains_claim_but_settled_hold_does_not(monkeypatch):
    from agentflow import coordinated_build
    from agentflow.coordinator.record import HELD, WAITING, Record

    waiting = Record(identity="wait", stage="build", pool="claude", demand=5,
                     repo="o/r", subject="7", state=WAITING, claim=True)
    held = Record(identity="held", stage="review", pool="codex", demand=2,
                  repo="o/r", subject="8", state=HELD, claim=False)
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [waiting, held])
    edited = []

    def run(argv):
        if argv[1:3] == ["issue", "list"]:
            label = argv[argv.index("--label") + 1]
            payload = ('[{"number":7,"updatedAt":"2020-01-01T00:00:00Z"},'
                       '{"number":8,"updatedAt":"2020-01-01T00:00:00Z"}]'
                       if label == "agentflow:building" else "[]")
            return SimpleNamespace(returncode=0, stdout=payload)
        if argv[1:3] == ["issue", "edit"]:
            edited.append(int(argv[3]))
            return SimpleNamespace(returncode=0, stdout="")
        if argv[1:3] == ["issue", "view"]:
            return SimpleNamespace(returncode=0, stdout='{"labels":[]}')
        raise AssertionError(argv)

    monkeypatch.setattr(loop, "_run", run)

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert edited == [8]


def test_build_submission_claims_then_enters_the_coordinator(monkeypatch):
    issue = {"number": 7, "title": "Do it", "body": "brief",
             "labels": [{"name": "ready-for-agent"},
                        {"name": "agentflow:complexity:deep"},
                        {"name": "agentflow:effort:high"}]}
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: issue)
    builder = SimpleNamespace(tool="claude")
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (builder, None, ""))
    events = []
    monkeypatch.setattr(loop, "_claim", lambda repo, number: events.append("claim") or True)
    coord = SimpleNamespace(submit_stage=lambda submission: events.append(submission.stage))

    assert "submitted" in dispatch._submit_coordinated_build(
        RepoConfig("o/r", "/tmp"), coord, None)
    assert events == ["claim", "build"]


def test_respond_waits_while_a_prior_change_record_owns_the_claim(monkeypatch):
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix", "please adjust", "cid-1", "base"))
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues",
                        lambda cfg, lane=None: {7})
    monkeypatch.setattr(loop, "_claim", lambda *a: pytest.fail("must not double-claim"))

    result = dispatch._submit_coordinated_respond(
        RepoConfig("o/r", "/tmp"), SimpleNamespace(), None)
    assert "prior change stage" in result


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
