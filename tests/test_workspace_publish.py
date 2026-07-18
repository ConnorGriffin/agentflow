"""Publishing an approved Build-Issue Proposal as a real GitHub issue (ADR 0027/0033), driven
through the public reconcile seam with a fake GitHub client.

The whole correctness story is idempotency on the approved-hash provenance key. The proofs that
matter: an approved Proposal is published exactly once and stamped with its hash and no
``wayfinder:*`` label; a crash-replay between "issue created" and "receipt recorded" finds the
existing issue and records the receipt only — never a second issue for one hash.
"""

from __future__ import annotations

import subprocess

import pytest

from agentflow.workspace import publish
from agentflow.workspace.store import FAILED, PUBLISHED, WorkspaceStore


class FakeGitHub:
    """A stand-in ``gh`` that models issue create + search over a shared repo of issues, so the
    idempotency contract can be exercised without touching real GitHub."""

    def __init__(self):
        self.issues = []           # each: {number, url, body, title}
        self.created = 0
        self._next = 100
        self.fail_create = False   # when True, `gh issue create` reports failure (failed path)
        self.create_stderr = "boom"  # the stderr a failed create reports (drives the plain reason)

    def seed_issue(self, body):
        """Inject an already-existing issue (e.g. one a prior attempt filed) so a search-before
        -create finds it — used to prove Retry never files a duplicate."""
        number = self._next
        self._next += 1
        url = f"https://github.com/ConnorGriffin/agentflow/issues/{number}"
        self.issues.append({"number": number, "url": url, "body": body, "title": ""})
        return number

    def __call__(self, cmd):
        if cmd[:2] == ["gh", "issue"] and cmd[2] == "list":
            term = cmd[cmd.index("--search") + 1]
            hits = [i for i in self.issues if term in i["body"]]
            import json
            return _ok(json.dumps(hits))
        if cmd[:2] == ["gh", "issue"] and cmd[2] == "create":
            if self.fail_create:
                return _fail(self.create_stderr)
            self.created += 1
            number = self._next
            self._next += 1
            title = cmd[cmd.index("--title") + 1]
            body = cmd[cmd.index("--body") + 1]
            url = f"https://github.com/ConnorGriffin/agentflow/issues/{number}"
            self.issues.append({"number": number, "url": url, "body": body, "title": title})
            return _ok(url)
        return _fail()


def _ok(stdout):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr="boom"):
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


@pytest.fixture
def store(tmp_path):
    s = WorkspaceStore("ConnorGriffin/agentflow", path=tmp_path / "agentflow.db")
    yield s
    s.close()


def _approved(store, cid="conv-1", hash_="sha256:v1", title="Add a Publish button"):
    store.open_conversation(title="Ask", conversation_id=cid, idempotency_key=f"{cid}:o", now=1)
    store.stage_proposal(cid, title=title, summary="the operator wants a publish button",
                         acceptance=["clicking it publishes", "it is idempotent"], body="",
                         content_hash=hash_, idempotency_key="s1", now=2)
    store.approve_proposal(cid, hash_, idempotency_key="a1", now=3)
    return cid


REPO = "ConnorGriffin/agentflow"


def test_approving_publishes_one_issue_stamped_with_provenance_and_no_wayfinder_label(store):
    cid = _approved(store)
    gh = FakeGitHub()
    issue = publish.publish_approved(REPO, cid, store=store, gh=gh, now=10)
    assert gh.created == 1 and issue["number"] == 100
    body = gh.issues[0]["body"]
    assert publish.provenance_line("sha256:v1") in body       # the hash is the provenance key
    assert "wayfinder:" not in body                            # no planning label — it's a handoff
    prop = store.proposal(cid)
    assert prop.state == PUBLISHED and prop.published_issue == 100


def test_a_replay_between_create_and_receipt_never_files_a_duplicate(store):
    """The crash-recovery proof (AC #3): the daemon created the issue but crashed before recording
    the receipt, so the Proposal is still APPROVED. The next reconcile MUST search the provenance
    key before creating — it finds the existing issue and records the receipt only. Exactly one
    issue for the hash. Would fail if the pre-create search regressed to create-first."""
    cid = _approved(store)
    gh = FakeGitHub()

    # First attempt: create the issue, then crash before recording the receipt.
    def crash_after_create(*a, **k):
        raise RuntimeError("crash between create and receipt")

    original = store.record_publication
    store.record_publication = crash_after_create
    with pytest.raises(RuntimeError):
        publish.publish_approved(REPO, cid, store=store, gh=gh, now=10)
    assert gh.created == 1                                     # the issue exists on GitHub...
    assert store.proposal(cid).state == "approved"            # ...but no receipt was recorded

    # Recovery: the reconcile runs again. It searches first, finds the just-created issue, and
    # records the receipt without creating a second one.
    store.record_publication = original
    issue = publish.publish_approved(REPO, cid, store=store, gh=gh, now=11)
    assert gh.created == 1                                     # STILL exactly one issue for the hash
    assert issue["number"] == 100
    assert store.proposal(cid).state == PUBLISHED


def test_a_hard_create_failure_parks_failed_with_a_plain_reason_and_stops_auto_retry(store):
    """AC #3/regression: a hard ``gh issue create`` failure records a durable FAILED disposition
    with a plain human reason and ``failed_at``, keeps the approval bound to its hash, and is NOT
    silently re-attempted. Fails on the old code, which stayed APPROVED and republished every cycle.
    """
    cid = _approved(store)
    gh = FakeGitHub()
    gh.fail_create = True
    gh.create_stderr = "HTTP 401: Bad credentials (https://api.github.com/repos/...)"

    assert publish.publish_approved(REPO, cid, store=store, gh=gh, now=10) is None
    prop = store.proposal(cid)
    assert prop.state == FAILED                               # durable failed disposition, not approved
    assert prop.fail_reason == "GitHub rejected the request — bad credentials."  # plain, not stderr
    assert prop.failed_at == 10
    assert prop.approved_hash == "sha256:v1"                  # still bound to the exact version

    # No silent re-attempt: a parked failure is not APPROVED, so a following publish pass is a
    # no-op that files nothing and leaves it failed until an operator Retry re-authorizes it.
    gh.fail_create = False
    assert publish.publish_approved(REPO, cid, store=store, gh=gh, now=20) is None
    assert gh.created == 0 and store.proposal(cid).state == FAILED


def test_retry_after_a_failure_republishes_the_same_version_without_a_duplicate(store):
    """AC #5/#7: Retry = the operator re-issuing ``approve_proposal`` for the same hash. It clears
    the failure and re-authorizes; the search-before-create idempotency still holds, so even if an
    issue for that hash already exists it records the receipt without a second ``create_issue``.
    """
    cid = _approved(store)
    gh = FakeGitHub()
    gh.fail_create = True
    assert publish.publish_approved(REPO, cid, store=store, gh=gh, now=10) is None
    assert store.proposal(cid).state == FAILED

    # An issue for this exact hash already exists on GitHub (e.g. a prior attempt that raced).
    existing = gh.seed_issue(publish.render_issue_body(store.proposal(cid).versions[-1], "sha256:v1"))

    # Retry: the operator re-approves the same hash. Credentials are fixed for good measure.
    gh.fail_create = False
    store.approve_proposal(cid, "sha256:v1", idempotency_key="retry-1", now=11)
    assert store.proposal(cid).state == "approved"           # failure cleared; re-authorized
    assert store.proposal(cid).fail_reason is None

    issue = publish.publish_approved(REPO, cid, store=store, gh=gh, now=12)
    assert gh.created == 0                                    # search-before-create found it — no dup
    assert issue["number"] == existing
    assert store.proposal(cid).state == PUBLISHED


def test_retry_after_a_failure_creates_exactly_one_issue_when_none_exists(store):
    """The common Retry path: the first attempt created nothing, so Retry files exactly one issue."""
    cid = _approved(store)
    gh = FakeGitHub()
    gh.fail_create = True
    publish.publish_approved(REPO, cid, store=store, gh=gh, now=10)
    assert store.proposal(cid).state == FAILED

    gh.fail_create = False
    store.approve_proposal(cid, "sha256:v1", idempotency_key="retry-1", now=11)
    publish.publish_approved(REPO, cid, store=store, gh=gh, now=12)
    assert gh.created == 1 and store.proposal(cid).state == PUBLISHED


def test_reconcile_publishes_every_approved_proposal_and_skips_the_rest(tmp_path):
    path = tmp_path / "agentflow.db"
    seed = WorkspaceStore(REPO, path=path)
    approved = _approved(seed, cid="conv-approved", hash_="sha256:aaa")
    # A staged-but-unapproved Proposal must not be published.
    seed.open_conversation(title="Ask", conversation_id="conv-staged", idempotency_key="o2", now=1)
    seed.stage_proposal("conv-staged", title="Not yet", summary="s", acceptance=["x"], body="",
                        content_hash="sha256:bbb", idempotency_key="s2", now=2)
    seed.close()

    cfg = type("Cfg", (), {"repo": REPO})()
    factory = lambda repo: WorkspaceStore(repo, path=path)  # noqa: E731
    gh = FakeGitHub()
    receipts = publish.reconcile_publications([cfg], store_factory=factory, gh=gh, now=10)
    assert gh.created == 1                                     # only the approved one published
    assert [r["conversation_id"] for r in receipts] == [approved]
    after = factory(REPO)
    try:
        assert after.proposal("conv-staged").state == "staged"
        assert after.proposal("conv-approved").state == PUBLISHED
    finally:
        after.close()


def test_reconcile_is_idempotent_across_cycles(tmp_path):
    """Once published, a later reconcile is a no-op that never files a second issue (the daemon
    runs this every cycle)."""
    path = tmp_path / "agentflow.db"
    seed = WorkspaceStore(REPO, path=path)
    _approved(seed)
    seed.close()
    cfg = type("Cfg", (), {"repo": REPO})()
    factory = lambda repo: WorkspaceStore(repo, path=path)  # noqa: E731
    gh = FakeGitHub()
    publish.reconcile_publications([cfg], store_factory=factory, gh=gh, now=10)
    publish.reconcile_publications([cfg], store_factory=factory, gh=gh, now=20)
    assert gh.created == 1
