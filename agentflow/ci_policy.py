"""Read-only GitHub Actions CI-policy audit.

Workflow content is repository-owned.  This module only parses it and reports
policy drift; it never creates, rewrites, or otherwise changes a workflow.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
import fnmatch
import re

import yaml


_PUBLISH_ACTIONS = {
    "cycjimmy/semantic-release-action",
    "goreleaser/goreleaser-action",
    "ncipollo/release-action",
    "pypa/gh-action-pypi-publish",
    "softprops/action-gh-release",
}
_PUBLISH_COMMANDS = (
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+publish\b", re.IGNORECASE),
    re.compile(r"\b(?:python\s+-m\s+)?twine\s+upload\b", re.IGNORECASE),
    re.compile(r"\b(?:poetry|uv|maturin|cargo)\s+publish\b", re.IGNORECASE),
    re.compile(r"\b(?:docker|podman)\s+push\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+buildx\s+build\b[^\n]*\s--push\b", re.IGNORECASE),
    re.compile(r"\bgh\s+release\s+create\b", re.IGNORECASE),
)
_PER_RUN_GROUP_VALUES = (
    "github.sha",
    "github.run_id",
    "github.run_number",
    "github.event.after",
    "github.event.head_commit.id",
)


class _GitHubWorkflowLoader(yaml.SafeLoader):
    """Keep GitHub's YAML 1.2-style ``on`` key from becoming boolean ``True``."""


_GitHubWorkflowLoader.yaml_implicit_resolvers = {
    key: [item for item in values if item[0] != "tag:yaml.org,2002:bool"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def audit_workflows(workdir: str | Path) -> list[str]:
    """Return CI-policy findings for a repository without modifying it.

    Each returned finding is scoped to one workflow, so repositories with several
    pull-request workflows do not collapse into a single pass/fail result.
    """
    root = Path(workdir)
    workflows = root / ".github" / "workflows"
    findings: list[str] = []
    if not workflows.is_dir():
        return findings
    paths = sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml")))
    for path in paths:
        relative = path.relative_to(root)
        try:
            document = yaml.load(path.read_text(), Loader=_GitHubWorkflowLoader)
        except (OSError, yaml.YAMLError) as exc:
            findings.append(f"WARN: {relative}: cannot parse workflow YAML ({exc})")
            continue
        if not isinstance(document, Mapping):
            findings.append(f"WARN: {relative}: workflow YAML is not a mapping")
            continue
        findings.extend(_workflow_findings(relative, document))
    return findings


def _workflow_findings(path: Path, workflow: Mapping) -> list[str]:
    triggers = workflow.get("on")
    if not _triggers(triggers, "pull_request"):
        return []

    concurrency = workflow.get("concurrency")
    if concurrency is None:
        return [f"WARN: {path}: pull-request workflow has no top-level concurrency policy"]
    if not isinstance(concurrency, Mapping):
        return [f"WARN: {path}: pull-request concurrency has no cancellation policy"]

    cancellation = concurrency.get("cancel-in-progress")
    if _is_pr_only_cancellation(cancellation):
        return []
    if not _is_true(cancellation):
        return [f"WARN: {path}: pull-request workflow does not cancel superseded runs"]

    if not _pushes_to_main(triggers) or not _main_pushes_share_group(concurrency.get("group")):
        return []
    publishing = _publishing_evidence(workflow)
    if publishing:
        return [
            f"WARN: {path}: main publish can be cancelled by a later main push "
            f"({', '.join(publishing)})"
        ]
    return [
        f"note: {path}: blanket cancellation also covers main, but no recognised "
        "publish step was found"
    ]


def _triggers(triggers: object, event: str) -> bool:
    if isinstance(triggers, str):
        return triggers == event
    if isinstance(triggers, Iterable) and not isinstance(triggers, Mapping):
        return event in triggers
    return isinstance(triggers, Mapping) and event in triggers


def _pushes_to_main(triggers: object) -> bool:
    if not isinstance(triggers, Mapping) or "push" not in triggers:
        return False
    push = triggers["push"]
    if not isinstance(push, Mapping):
        return True
    branches = _values(push.get("branches"))
    if branches:
        return any(fnmatch.fnmatchcase("main", branch) for branch in branches)
    return not any(fnmatch.fnmatchcase("main", branch)
                   for branch in _values(push.get("branches-ignore")))


def _values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable) and not isinstance(value, Mapping):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _is_pr_only_cancellation(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.split()).replace('"', "'")
    return normalized == "${{ github.event_name == 'pull_request' }}"


def _is_true(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _main_pushes_share_group(group: object) -> bool:
    if not isinstance(group, str):
        return True
    lowered = group.lower()
    return not any(value in lowered for value in _PER_RUN_GROUP_VALUES)


def _publishing_evidence(workflow: Mapping) -> list[str]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, Mapping):
        return []
    evidence: list[str] = []
    for job in jobs.values():
        if not isinstance(job, Mapping):
            continue
        steps = job.get("steps")
        if not isinstance(steps, Iterable) or isinstance(steps, (str, Mapping)):
            continue
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            evidence.extend(_step_publishing_evidence(step))
    return evidence


def _step_publishing_evidence(step: Mapping) -> list[str]:
    uses = step.get("uses")
    if isinstance(uses, str):
        action = uses.split("@", maxsplit=1)[0].lower()
        if action in _PUBLISH_ACTIONS:
            return [f"uses: {uses}"]
        if action == "docker/build-push-action" and _is_true(
            step.get("with", {}).get("push") if isinstance(step.get("with"), Mapping) else None
        ):
            return [f"uses: {uses} with push: true"]
    run = step.get("run")
    if isinstance(run, str) and any(pattern.search(run) for pattern in _PUBLISH_COMMANDS):
        return [f"run: {run.splitlines()[0]}"]
    return []
