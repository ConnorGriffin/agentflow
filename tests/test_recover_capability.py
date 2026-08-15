from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from agentflow import daemon
from agentflow.cli import main
from agentflow.coordinator import Coordinator, Submission
from agentflow.coordinator.errors import StoreUnavailable
from agentflow.coordinator.record import COMPLETED, WAITING
from agentflow.coordinator.store import Store, default_store_path
from agentflow.coordinator.telemetry import read_attempts
from agentflow.worktree_ref import WorktreeKind, WorktreeRef


REPOSITORY = "owner/repo"
INCOMPATIBLE = "capability_environment_failure:incompatible"


def _configured_git_root(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"], check=True
    )
    (root / "AGENTS.md").write_text("profile: reviewed\nui-surfaces: none\n")
    subprocess.run(["git", "-C", str(root), "add", "AGENTS.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "fetch", "origin", "HEAD:refs/remotes/origin/main"],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "origin/main"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[[repositories]]\nrepo = "{REPOSITORY}"\nworkdir = "{root}"\n'
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    return root, revision


def _seed_incompatible_predecessors(tmp_path: Path) -> tuple[list[str], dict[str, Path]]:
    old_root = tmp_path / "detached-capability-checkout"
    old_root.mkdir()
    store = Store(default_store_path())
    coordinator = Coordinator(store=store)
    identities: list[str] = []
    old_dirs: dict[str, Path] = {}
    for issue in range(101, 107):
        pool = "claude" if issue % 2 else "codex"
        source = Path(WorktreeRef.for_intake(str(old_root), pool, issue).path)
        source.mkdir(parents=True)
        identity = coordinator.submit_stage(
            Submission(
                repo=REPOSITORY,
                subject=str(issue),
                stage="intake",
                target=f"issue-snapshot-{issue}",
                source=str(source),
                pool=pool,
                input_ptr=json.dumps(
                    {"issue": issue, "prompt": f"intake {issue}", "source_ref": "a" * 40},
                    sort_keys=True,
                ),
                capability_root=str(old_root),
                capability_context={"ui": True},
                subject_revision="a" * 40,
            )
        )
        identities.append(identity)
        old_dirs[identity] = source

    build_source = Path(
        WorktreeRef.for_build(str(old_root), "codex", 207, "preserve-this-slug").path
    )
    build_source.mkdir(parents=True)
    build_identity = coordinator.submit_stage(
        Submission(
            repo=REPOSITORY,
            subject="207",
            stage="build",
            target="frozen-build-target",
            source=str(build_source),
            pool="codex",
            complexity="deep",
            effort="extra",
            input_ptr="frozen build prompt",
            session_lead=True,
            builder_lineage="codex",
            branch_lineage="codex",
            builder_complexity="deep",
            builder_effort="high",
            capability_root=str(old_root),
            capability_context={"ui": True},
            subject_revision="a" * 40,
            continuation=True,
            floodgates=True,
        )
    )
    identities.append(build_identity)
    old_dirs[build_identity] = build_source

    for identity in identities:
        record = coordinator.stage_record(identity)
        assert record is not None
        record.refusal = INCOMPATIBLE
        assert store.upsert(record)
    store.close()
    return sorted(identities), old_dirs


def _seed_unsupported_candidate(tmp_path: Path) -> str:
    store = Store(default_store_path())
    coordinator = Coordinator(store=store)
    identity = coordinator.submit_stage(
        Submission(
            repo=REPOSITORY,
            subject="999",
            stage="review",
            target="b" * 40,
            source=str(tmp_path / "unsupported"),
            pool="claude",
            complexity="deep",
            builder_complexity="deep",
            subject_revision="b" * 40,
        )
    )
    record = coordinator.stage_record(identity)
    assert record is not None
    record.refusal = INCOMPATIBLE
    assert store.upsert(record)
    store.close()
    return identity


def _seed_unrelated_record() -> tuple[str, object]:
    store = Store(default_store_path())
    coordinator = Coordinator(store=store)
    identity = coordinator.submit_stage(
        Submission(
            repo=REPOSITORY,
            subject="808",
            stage="intake",
            target="unrelated-snapshot",
            pool="claude",
            input_ptr=json.dumps({"issue": 808, "source_ref": "c" * 40}),
            subject_revision="c" * 40,
        )
    )
    record = coordinator.stage_record(identity)
    assert record is not None and record.refusal == ""
    store.close()
    return identity, record


def test_public_cli_recovers_the_seven_capability_held_stages_once(
    tmp_path, monkeypatch, capsys
):
    root, current_revision = _configured_git_root(tmp_path, monkeypatch)
    predecessors, old_dirs = _seed_incompatible_predecessors(tmp_path)
    unrelated_identity, unrelated_before = _seed_unrelated_record()
    seeded_store = Store(default_store_path())
    seeded = seeded_store.load()
    assert len(predecessors) == 7
    assert [seeded[identity].stage for identity in predecessors].count("intake") == 6
    assert [seeded[identity].stage for identity in predecessors].count("build") == 1
    assert all(
        seeded[identity].refusal == INCOMPATIBLE
        and seeded[identity].state == WAITING
        and seeded[identity].claim
        and not seeded[identity].retired
        and seeded[identity].attempts == 0
        and seeded[identity].attempt_committed is False
        and seeded[identity].family is None
        and seeded[identity].process_alive is False
        and WorktreeRef.parse(seeded[identity].source) is not None
        and old_dirs[identity].is_dir()
        for identity in predecessors
    )
    seeded_store.close()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recovery crossed a provider, GitHub, cycle, or deletion seam")

    monkeypatch.setattr(Coordinator, "cycle", forbidden)
    monkeypatch.setattr("agentflow.github.api", forbidden)
    monkeypatch.setattr("shutil.rmtree", forbidden)
    monkeypatch.setattr(
        "agentflow.capability_contracts.preflight",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True),
    )

    assert main(["recover-capability", REPOSITORY]) == 0
    first = json.loads(capsys.readouterr().out)

    assert first == {
        "repository": REPOSITORY,
        "revision": current_revision,
        "results": [
            {
                "predecessor": predecessor,
                "successor": f"{predecessor}|s1",
                "status": "recovered",
                "reason": "recovered",
            }
            for predecessor in predecessors
        ],
    }
    store = Store(default_store_path())
    records = store.load()
    assert len(records) == 15
    assert records[unrelated_identity] == unrelated_before
    for predecessor_identity in predecessors:
        predecessor = records[predecessor_identity]
        successor = records[f"{predecessor_identity}|s1"]
        assert (
            predecessor.state,
            predecessor.retired,
            predecessor.claim,
            predecessor.attempts,
        ) == (COMPLETED, True, False, 0)
        assert (
            successor.state,
            successor.retired,
            successor.claim,
            successor.attempts,
        ) == (WAITING, False, True, 0)
        assert successor.subject_revision == current_revision
        assert successor.capability_root == str(root)
        assert json.loads(successor.capability_context) == {"ui": False}
        assert successor.route_id.startswith("production/")
        assert len(successor.route_cell_digest) == 64
        assert len(successor.launch_config_digest) == 64
        assert old_dirs[predecessor_identity].is_dir()
        assert successor.target == predecessor.target
        assert successor.pool == predecessor.pool
        assert successor.complexity == predecessor.complexity
        assert successor.effort == predecessor.effort
        if successor.stage == "intake":
            old_payload = json.loads(predecessor.input_ptr)
            new_payload = json.loads(successor.input_ptr)
            assert new_payload["source_ref"] == current_revision
            assert new_payload["issue"] == old_payload["issue"]
            assert new_payload["prompt"] == old_payload["prompt"]
            parsed = WorktreeRef.parse(successor.source)
            assert parsed is not None and parsed.kind is WorktreeKind.INTAKE
            assert parsed.workdir == str(root)
        else:
            assert successor.input_ptr == "frozen build prompt"
            assert successor.branch_lineage == "codex-recovery-s1"
            assert successor.builder_lineage == "codex"
            assert successor.builder_complexity == "deep"
            assert successor.builder_effort == "high"
            assert successor.session_lead is True
            assert successor.continuation is True
            assert successor.floodgates is True
            parsed = WorktreeRef.parse(successor.source)
            assert parsed is not None and parsed.kind is WorktreeKind.BUILD
            assert parsed.workdir == str(root)
            assert parsed.tool == "codex-recovery-s1"
            assert parsed.slug == "preserve-this-slug"
            assert parsed.branch == (
                "agentflow/codex-recovery-s1/issue-207-preserve-this-slug"
            )
            assert successor.source != predecessor.source
    assert store.permits_used("claude") == store.permits_used("codex") == 0
    assert read_attempts(store.path) == []
    before_records = {
        identity: (record.revision, record)
        for identity, record in records.items()
    }
    before_database = store.path.read_bytes()
    store.close()

    assert main(["recover-capability", REPOSITORY]) == 0
    second = json.loads(capsys.readouterr().out)

    assert second == {
        "repository": REPOSITORY,
        "revision": current_revision,
        "results": [
            {
                "predecessor": predecessor,
                "successor": f"{predecessor}|s1",
                "status": "already_recovered",
                "reason": "already-recovered",
            }
            for predecessor in predecessors
        ],
    }
    reopened = Store(default_store_path())
    after_records = reopened.load()
    assert {
        identity: (record.revision, record)
        for identity, record in after_records.items()
    } == before_records
    assert reopened.path.read_bytes() == before_database
    assert reopened.permits_used("claude") == reopened.permits_used("codex") == 0
    assert read_attempts(reopened.path) == []
    assert all(path.is_dir() for path in old_dirs.values())
    reopened.close()


def test_public_cli_holds_daemon_exclusion_through_validation_and_transfer(
    tmp_path, monkeypatch, capsys
):
    _configured_git_root(tmp_path, monkeypatch)
    _seed_incompatible_predecessors(tmp_path)
    observations = []

    def ready(*_args, **_kwargs):
        observations.append((
            daemon.LOCK.is_dir(),
            (daemon.LOCK / "pid").read_text().strip(),
            daemon._acquire_lock(),
        ))
        return SimpleNamespace(ready=True)

    monkeypatch.setattr("agentflow.capability_contracts.preflight", ready)

    assert main(["recover-capability", REPOSITORY]) == 0

    assert json.loads(capsys.readouterr().out)["repository"] == REPOSITORY
    assert observations == [(True, str(os.getpid()), False)] * 7
    assert not daemon.LOCK.exists()


def test_public_cli_refuses_a_live_daemon_without_removing_its_lock(
    tmp_path, monkeypatch, capsys
):
    _configured_git_root(tmp_path, monkeypatch)
    owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"]
    )
    daemon.LOCK.mkdir()
    (daemon.LOCK / "pid").write_text(str(owner.pid))
    try:
        assert main(["recover-capability", REPOSITORY]) == 1
        assert json.loads(capsys.readouterr().out) == {
            "repository": REPOSITORY,
            "revision": "",
            "results": [{
                "predecessor": "",
                "successor": "",
                "status": "skipped",
                "reason": "daemon-running",
            }],
        }
        assert daemon.LOCK.is_dir()
        assert (daemon.LOCK / "pid").read_text().strip() == str(owner.pid)
    finally:
        owner.terminate()
        owner.wait()
        if daemon.LOCK.exists():
            (daemon.LOCK / "pid").unlink(missing_ok=True)
            daemon.LOCK.rmdir()


def test_public_cli_reclaims_a_dead_daemon_lock_without_polluting_json(
    tmp_path, monkeypatch, capsys
):
    _configured_git_root(tmp_path, monkeypatch)
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    daemon.LOCK.mkdir()
    (daemon.LOCK / "pid").write_text(str(dead_owner.pid))

    assert main(["recover-capability", REPOSITORY]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "repository": REPOSITORY,
        "revision": subprocess.run(
            ["git", "-C", str(tmp_path / "repo"), "rev-parse", "origin/main"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip(),
        "results": [],
    }
    assert not daemon.LOCK.exists()
    assert list(tmp_path.glob("daemon.lock.stale.*")) == []


def test_public_cli_cleans_up_its_lock_when_the_pid_stamp_fails(
    tmp_path, monkeypatch, capsys
):
    _configured_git_root(tmp_path, monkeypatch)
    write_text = Path.write_text

    def fail_pid_stamp(path, *args, **kwargs):
        if path == daemon.LOCK / "pid":
            raise OSError("pid stamp unavailable")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_pid_stamp)

    assert main(["recover-capability", REPOSITORY]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "repository": REPOSITORY,
        "revision": "",
        "results": [{
            "predecessor": "",
            "successor": "",
            "status": "skipped",
            "reason": "daemon-running",
        }],
    }
    assert not daemon.LOCK.exists()


def test_public_cli_reclaims_an_aged_malformed_lock_without_polluting_json(
    tmp_path, monkeypatch, capsys
):
    _configured_git_root(tmp_path, monkeypatch)
    daemon.LOCK.mkdir()
    (daemon.LOCK / "pid").write_text("malformed")
    stale_time = time.time() - daemon.STALE_SECONDS - 1
    os.utime(daemon.LOCK, (stale_time, stale_time))

    assert main(["recover-capability", REPOSITORY]) == 0

    assert json.loads(capsys.readouterr().out)["results"] == []
    assert not daemon.LOCK.exists()
    assert list(tmp_path.glob("daemon.lock.stale.*")) == []


def test_public_cli_rejects_a_configured_symlink_before_opening_the_store(
    tmp_path, monkeypatch, capsys
):
    root, _revision = _configured_git_root(tmp_path, monkeypatch)
    declared = tmp_path / "configured-link"
    declared.symlink_to(root, target_is_directory=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[[repositories]]\nrepo = "{REPOSITORY}"\nworkdir = "{declared}"\n'
    )

    assert main(["recover-capability", REPOSITORY]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "repository": REPOSITORY,
        "revision": "",
        "results": [{
            "predecessor": "",
            "successor": "",
            "status": "skipped",
            "reason": "root-unusable",
        }],
    }
    assert not default_store_path().exists()
    assert not daemon.LOCK.exists()


def test_public_cli_prevalidates_the_whole_matching_batch_before_any_transfer(
    tmp_path, monkeypatch, capsys
):
    _configured_git_root(tmp_path, monkeypatch)
    predecessors, _old_dirs = _seed_incompatible_predecessors(tmp_path)
    unsupported = _seed_unsupported_candidate(tmp_path)
    store = Store(default_store_path())
    by_subject = {record.subject: record for record in store.load().values()}
    invalid_reasons = {
        by_subject["101"].identity: "ineligible-state",
        by_subject["102"].identity: "started",
        by_subject["103"].identity: "input-unreadable",
        by_subject["104"].identity: "source-unreadable",
        by_subject["207"].identity: "capability-not-ready",
        unsupported: "unsupported-stage",
    }
    by_subject["101"].claim = False
    by_subject["102"].attempts = 1
    by_subject["103"].input_ptr = "{"
    by_subject["104"].source = WorktreeRef.for_intake(
        str(tmp_path / "missing-old-root"), by_subject["104"].pool, 104
    ).path
    for subject in ("101", "102", "103", "104"):
        assert store.upsert(by_subject[subject])
    store.close()
    monkeypatch.setattr(
        "agentflow.capability_contracts.preflight",
        lambda _root, stage, *_args, **_kwargs:
            SimpleNamespace(ready=stage != "build"),
    )
    before_store = Store(default_store_path())
    before = before_store.load()
    before_bytes = before_store.path.read_bytes()
    before_store.close()

    assert main(["recover-capability", REPOSITORY]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["results"] == [
        {
            "predecessor": identity,
            "successor": "",
            "status": "skipped",
            "reason": invalid_reasons[identity],
        }
        for identity in sorted(invalid_reasons)
    ]
    after_store = Store(default_store_path())
    after = after_store.load()
    assert after == before
    assert after_store.path.read_bytes() == before_bytes
    assert all(
        f"{identity}|s1" not in after
        for identity in predecessors
    )
    assert all(
        after[identity].state == WAITING
        and after[identity].claim
        and not after[identity].retired
        and after[identity].attempts == 0
        for identity in predecessors
        if after[identity].subject in {"105", "106"}
    )
    after_store.close()


def test_public_cli_skips_a_malformed_durable_subject_without_writing(
    tmp_path, monkeypatch, capsys
):
    _root, current_revision = _configured_git_root(tmp_path, monkeypatch)
    old_root = tmp_path / "detached-capability-checkout"
    source = Path(WorktreeRef.for_intake(str(old_root), "claude", 909).path)
    source.mkdir(parents=True)
    store = Store(default_store_path())
    coordinator = Coordinator(store=store)
    identity = coordinator.submit_stage(
        Submission(
            repo=REPOSITORY,
            subject="909",
            stage="intake",
            target="issue-snapshot-909",
            source=str(source),
            pool="claude",
            input_ptr=json.dumps({"issue": 909, "source_ref": "a" * 40}),
            capability_root=str(old_root),
            capability_context={"ui": True},
            subject_revision="a" * 40,
        )
    )
    prior = coordinator.stage_record(identity)
    assert prior is not None
    prior.subject = "issue-x"
    prior.refusal = INCOMPATIBLE
    assert prior.state == WAITING
    assert prior.claim and not prior.retired and prior.attempts == 0
    assert store.upsert(prior)
    before = store.load()
    before_bytes = store.path.read_bytes()
    store.close()
    monkeypatch.setattr(
        "agentflow.capability_contracts.preflight",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True),
    )

    assert main(["recover-capability", REPOSITORY]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "repository": REPOSITORY,
        "revision": current_revision,
        "results": [{
            "predecessor": identity,
            "successor": "",
            "status": "skipped",
            "reason": "input-unreadable",
        }],
    }
    reopened = Store(default_store_path())
    assert reopened.load() == before
    assert reopened.path.read_bytes() == before_bytes
    reopened.close()
    assert not daemon.LOCK.exists()


def test_public_cli_refuses_an_immutable_rerun_mismatch_without_writing(
    tmp_path, monkeypatch, capsys
):
    _configured_git_root(tmp_path, monkeypatch)
    predecessors, _old_dirs = _seed_incompatible_predecessors(tmp_path)
    monkeypatch.setattr(
        "agentflow.capability_contracts.preflight",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True),
    )
    assert main(["recover-capability", REPOSITORY]) == 0
    capsys.readouterr()

    predecessor = predecessors[0]
    store = Store(default_store_path())
    successor = store.record_of(f"{predecessor}|s1")
    assert successor is not None
    successor.source = str(tmp_path / "foreign-successor")
    assert store.upsert(successor)
    before = store.load()
    before_bytes = store.path.read_bytes()
    store.close()

    assert main(["recover-capability", REPOSITORY]) == 2

    report = json.loads(capsys.readouterr().out)
    assert report["results"] == [
        {
            "predecessor": predecessor,
            "successor": "",
            "status": "skipped",
            "reason": "successor-conflict",
        }
    ]
    reopened = Store(default_store_path())
    assert reopened.load() == before
    assert reopened.path.read_bytes() == before_bytes
    reopened.close()


def test_public_cli_reports_a_noncollision_store_failure_as_transfer_failed(
    tmp_path, monkeypatch, capsys
):
    _configured_git_root(tmp_path, monkeypatch)
    predecessors, _old_dirs = _seed_incompatible_predecessors(tmp_path)
    monkeypatch.setattr(
        "agentflow.capability_contracts.preflight",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True),
    )
    before_store = Store(default_store_path())
    before = before_store.load()
    before_store.close()

    def fail_transfer(*_args, **_kwargs):
        raise StoreUnavailable("injected transaction failure")

    monkeypatch.setattr(Store, "submit", fail_transfer)
    assert main(["recover-capability", REPOSITORY]) == 2
    assert not daemon.LOCK.exists()

    report = json.loads(capsys.readouterr().out)
    assert report["results"] == [
        {
            "predecessor": predecessors[0],
            "successor": "",
            "status": "skipped",
            "reason": "transfer-failed",
        }
    ]
    reopened = Store(default_store_path())
    assert reopened.load() == before
    reopened.close()


def test_recover_capability_empty_repository_is_an_explicit_json_noop(
    tmp_path, monkeypatch, capsys
):
    _root, revision = _configured_git_root(tmp_path, monkeypatch)

    assert main(["recover-capability", REPOSITORY]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "repository": REPOSITORY,
        "revision": revision,
        "results": [],
    }
