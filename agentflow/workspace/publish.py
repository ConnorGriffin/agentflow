"""Publishing an approved Build-Issue Proposal as a real GitHub issue (ADR 0027/0033).

Publication is the **build handoff**: approving one exact content hash creates a normal GitHub
issue carrying that hash as provenance and **no** ``wayfinder:*`` label, so agentflow's own intake
grounds and builds it like any other issue — no conversation silently enters the pipeline.

The whole correctness story is **idempotency on the approved-hash provenance key** (ADR 0033
"verify the external effect"). On *every* attempt — including a crash-replay between "issue
created" and "receipt recorded" — the daemon searches GitHub for that key *before* it creates
anything: found → record the receipt only; not found → create, then record. There is never a
second issue for one hash. The content hash is computed here by the daemon, never supplied by the
sandboxed session, so the provenance key is trustworthy (ADR 0034).
"""

from __future__ import annotations

import hashlib
import json
import re

from agentflow.workspace.store import APPROVED, WorkspaceStore

# The provenance line stamped into every published issue body. It carries the approved content
# hash verbatim so a later publish attempt can find the issue by searching for that exact key.
_MARK_PREFIX = "agentflow-proposal-provenance:"


def content_hash(title: str, summary: str, acceptance: list[str], body: str) -> str:
    """The daemon-computed provenance key for a Proposal version: a stable ``sha256:`` over the
    canonical draft. Same content → same hash, so approval binds to exactly what the operator saw
    and publication de-duplicates on it (ADR 0033/0034)."""
    payload = json.dumps(
        {"title": title, "summary": summary, "acceptance": list(acceptance), "body": body},
        sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provenance_line(content_hash_value: str) -> str:
    return f"{_MARK_PREFIX} {content_hash_value}"


def render_issue_body(version, content_hash_value: str) -> str:
    """Render the published issue body: the draft's summary, its acceptance criteria, and the
    provenance footer. No ``wayfinder:*`` label is applied anywhere — publishing is the handoff, so
    intake picks this up like any other build issue (ADR 0027)."""
    parts = [version.summary.strip()]
    if version.acceptance:
        parts.append("## Acceptance criteria\n" + "\n".join(f"- [ ] {a}" for a in version.acceptance))
    if version.body.strip():
        parts.append(version.body.strip())
    parts.append(
        "---\n"
        f"{provenance_line(content_hash_value)}\n\n"
        "*Published from an agentflow project-workspace Ask (ADR 0027). This issue carries its "
        "approved proposal hash as provenance and enters intake like any other build issue.*")
    return "\n\n".join(p for p in parts if p)


def _default_gh(cmd: list[str]):
    from agentflow.runner import _run
    return _run(cmd)


def find_published(repo: str, content_hash_value: str, *, gh=_default_gh) -> dict | None:
    """The already-published issue carrying this provenance key, or ``None``. Searches GitHub for
    the hash and confirms the provenance line is really in the body — a fuzzy search hit that does
    not carry the marker is not our issue. This runs before every create, so a replay finds a
    just-created issue instead of filing a second one (ADR 0033)."""
    # Search by the bare hex digest: GitHub free-text search reads ``sha256:`` as a ``key:value``
    # qualifier, so the colon-free digest is what actually matches the marker in the body.
    term = content_hash_value.split(":", 1)[-1]
    r = gh(["gh", "issue", "list", "--repo", repo, "--state", "all", "--search",
            term, "--json", "number,url,body", "--limit", "50"])
    if r.returncode != 0:
        return None
    try:
        issues = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    marker = provenance_line(content_hash_value)
    for issue in issues:
        if marker in (issue.get("body") or ""):
            return {"number": issue.get("number"), "url": issue.get("url")}
    return None


def create_issue(repo: str, title: str, body: str, *, gh=_default_gh) -> dict | None:
    """Create the real GitHub build issue and return its number/url, or ``None`` if creation
    failed (the reconcile retries next cycle). No label is passed — no ``wayfinder:*`` marking, so
    intake grounds it normally (ADR 0027)."""
    r = gh(["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body])
    if r.returncode != 0:
        return None
    url = (r.stdout or "").strip().splitlines()[-1].strip() if r.stdout else ""
    m = re.search(r"/issues/(\d+)", url)
    if not m:
        return None
    return {"number": int(m.group(1)), "url": url}


def publish_approved(repo: str, conversation_id: str, *, store: WorkspaceStore, gh=_default_gh,
                     now: int = 0) -> dict | None:
    """Publish one approved Proposal idempotently on its approved-hash provenance key.

    Search-before-create is unconditional: found → record the receipt only; not found → create the
    issue, then record. A crash between create and record leaves the Proposal APPROVED, so the next
    cycle re-runs, finds the issue by its hash, and records the receipt — never a duplicate issue
    (ADR 0033). Returns the issue receipt, or ``None`` when there is nothing to do / a create
    failed and the publish should retry."""
    prop = store.proposal(conversation_id)
    if prop is None or prop.state != APPROVED or not prop.approved_hash:
        return None
    version = prop.version_hash(prop.approved_hash)
    if version is None:
        return None
    key = prop.approved_hash
    issue = find_published(repo, key, gh=gh)              # ALWAYS search first (idempotency)
    if issue is None:
        issue = create_issue(repo, version.title, render_issue_body(version, key), gh=gh)
        if issue is None:
            return None                                  # create failed — retry next cycle
    store.record_publication(conversation_id, key, issue_number=issue["number"],
                             issue_url=issue["url"], now=now)
    return issue


def reconcile_publications(repos: list, *, store_factory=WorkspaceStore, gh=_default_gh, now=0,
                           _log=None) -> list[dict]:
    """Publish every approved-but-unpublished Proposal across the enrolled repos. Called each
    workspace cycle after commands drain, so an approval taken this cycle publishes this cycle, and
    a crashed publish reconciles on the next one (ADR 0033)."""
    receipts = []
    for cfg in repos:
        store = store_factory(cfg.repo)
        try:
            for prop in store.proposals():
                if prop.state != APPROVED:
                    continue
                try:
                    issue = publish_approved(cfg.repo, prop.conversation_id, store=store, gh=gh,
                                             now=now)
                except Exception as e:  # noqa: BLE001 — one bad publish must not stall the rest
                    if _log:
                        _log(f"publish error {cfg.repo}#{prop.conversation_id}: "
                             f"{type(e).__name__}: {e}")
                    continue
                if issue is not None:
                    receipts.append({"repo": cfg.repo, "conversation_id": prop.conversation_id,
                                     "issue": issue})
        finally:
            store.close()
    return receipts
