"""Publish a completed local candidate with durable, recoverable Git intent.

Publication is an explicit controller operation. It never starts a Harness,
reruns a Gate, mints Approval, or changes the local implementation candidate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from machinist.evidence import EvidenceError, TaskEvidence
from machinist.forge import (
    ForgeClient,
    ForgeError,
    PublishedChange,
    normalize_host,
    normalize_repository,
    publish_change,
    validate_sha,
)
from machinist.lifecycle import LifecycleError, Phase, RunStatus, TaskLifecycle

if TYPE_CHECKING:
    from machinist.local_tasks import LocalTask, LocalTaskStore
    from machinist.local_workspace import LocalWorkspace


class PublicationError(Exception):
    """The exact local candidate or remote publication cannot be proven."""


def publish_task(
    task_id: str | int,
    *,
    store: LocalTaskStore,
    workspace: LocalWorkspace,
    forge: ForgeClient,
    lifecycle: TaskLifecycle | None = None,
) -> LocalTask:
    """Publish one reviewed Task; retries reconcile the same intent and change."""
    with store.claim(task_id) as task:
        candidate = ready_candidate(task, workspace, lifecycle)
        origin = workspace.origin_url()
        binding = _publication_binding(task, forge, origin)
        previous = _previous_publication(task.publication, binding)
        remote = workspace.remote_sha(task.branch, origin_url=origin)
        expected_remote = _expected_remote(previous, candidate, remote)
        expected_number = _change_number(previous)
        existing = forge.find_change(task.branch)
        if existing is not None:
            _verify_existing(existing, binding, remote, expected_number)
        elif expected_number is not None:
            raise PublicationError(
                "publication change number is missing from the forge"
            )

        intent: dict[str, Any] = {
            **(previous or {}),
            **binding,
            "stage": "publishing",
            "intended_sha": candidate,
            "expected_remote_sha": expected_remote,
        }
        if existing is not None:
            intent["change_number"] = existing.number
        task = store.update(task, publication=intent)
        if remote != candidate:
            pushed = workspace.push_candidate(
                task.branch,
                expected_candidate_sha=candidate,
                expected_remote_sha=expected_remote,
                origin_url=origin,
            )
            if pushed != candidate:
                raise PublicationError("push did not deliver the reviewed candidate")
        _verify_delivery(workspace, task.branch, candidate, origin)

        change = publish_change(
            forge,
            branch=task.branch,
            base=task.base_branch,
            head_sha=candidate,
            title=task.title,
            body=_publication_body(task),
            draft=False,
            expected_number=intent.get("change_number"),
        )
        _verify_delivery(workspace, task.branch, candidate, origin)
        return store.update(
            task,
            publication={
                **intent,
                "stage": "published",
                "published_sha": candidate,
                "change_number": change.number,
                "url": change.url,
            },
        )


def ready_candidate(
    task: LocalTask, workspace: LocalWorkspace, lifecycle: TaskLifecycle | None
) -> str:
    if Path(task.repository) != workspace.repo_root:
        raise PublicationError("Task repository does not match the local Workshop")
    candidate = task.candidate_sha
    try:
        validate_sha(candidate)  # type: ignore[arg-type]
        validate_sha(task.spec_sha)  # type: ignore[arg-type]
    except ForgeError as exc:
        raise PublicationError("Task has no complete Spec and candidate") from exc
    approval = task.approval
    if not isinstance(approval, dict) or any(
        approval.get(key) != value
        for key, value in {
            "repository": task.repository,
            "task_id": task.id,
            "spec_sha": task.spec_sha,
        }.items()
    ):
        raise PublicationError(
            "Task needs Approval of its exact Spec before publication"
        )
    report = task.review_report
    if (
        not isinstance(report, dict)
        or report.get("completed") is not True
        or report.get("reviewed_sha") != candidate
    ):
        raise PublicationError("Task needs completed Review of its exact candidate")
    runs = lifecycle or TaskLifecycle(Path(task.repository) / ".machinist/runs/local")
    try:
        for phase in (Phase.EXECUTE, Phase.REVIEW):
            record = runs.record(task.number, phase)
            if record is None or record.status is not RunStatus.SUCCEEDED:
                raise PublicationError(
                    f"Task needs successful {phase.value.title()} Evidence"
                )
            evidence = TaskEvidence.load(record.evidence)
            if phase is Phase.EXECUTE:
                valid = (
                    evidence.implementation_sha == candidate
                    and evidence.approved_sha == task.spec_sha
                )
            else:
                valid = evidence.reviewed_sha == candidate
            if not valid:
                raise PublicationError(
                    f"{phase.value.title()} Evidence does not match the reviewed candidate"
                )
    except (LifecycleError, EvidenceError) as exc:
        raise PublicationError(f"cannot prove local Phase Evidence: {exc}") from exc
    if workspace.branch_sha(task.branch) != candidate:
        raise PublicationError("local candidate branch changed since Review")
    assert candidate is not None
    return candidate


def _publication_binding(
    task: LocalTask, forge: ForgeClient, origin: str
) -> dict[str, Any]:
    try:
        host, repository = origin_target(origin)
        if (
            host != normalize_host(forge.host)
            or repository != normalize_repository(forge.repository)
            or forge.provider not in {"github", "gitlab"}
        ):
            raise PublicationError("Git origin does not match the configured forge")
    except (ForgeError, ValueError) as exc:
        raise PublicationError(f"cannot bind publication to Git origin: {exc}") from exc
    return {
        "version": 1,
        "provider": forge.provider,
        "host": host,
        "repository": repository,
        "branch": task.branch,
        "base_branch": task.base_branch,
    }


def origin_target(origin: str) -> tuple[str, str]:
    """Resolve supported Git transports to a forge host and repository identity."""
    if not isinstance(origin, str) or any(ord(char) < 33 for char in origin):
        raise PublicationError("Git origin contains invalid characters")
    if "://" not in origin:
        match = re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?([^/:]+):(.+)", origin)
        if match is None:
            raise PublicationError("Git origin must use HTTPS, SSH, or SCP transport")
        raw_host, path = match.groups()
    else:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.query
            or parsed.fragment
            or parsed.password is not None
            or (parsed.scheme == "https" and parsed.username is not None)
            or parsed.hostname is None
        ):
            raise PublicationError("Git origin has unsafe transport or credentials")
        raw_host = parsed.hostname
        if parsed.port is not None:
            raw_host += f":{parsed.port}"
        path = parsed.path.removeprefix("/")
    return normalize_host(raw_host), normalize_repository(path)


def _previous_publication(
    publication: dict[str, Any] | None, binding: dict[str, Any]
) -> dict[str, Any] | None:
    if publication is None:
        return None
    if any(publication.get(key) != value for key, value in binding.items()):
        raise PublicationError(
            "publication binding changed; refusing a different destination"
        )
    if publication.get("stage") not in {"publishing", "published"}:
        raise PublicationError("publication intent has an invalid stage")
    try:
        validate_sha(publication.get("intended_sha"))
        for key in ("expected_remote_sha", "published_sha"):
            if publication.get(key) is not None:
                validate_sha(publication[key])
    except ForgeError as exc:
        raise PublicationError(
            "publication intent contains invalid commit Evidence"
        ) from exc
    return publication


def _expected_remote(
    previous: dict[str, Any] | None, candidate: str, remote: str | None
) -> str | None:
    if previous is None:
        if remote is not None:
            raise PublicationError("refusing to overwrite an unowned remote branch")
        return None
    if previous["stage"] == "published":
        published = previous.get("published_sha")
        if published is None or remote != published:
            raise PublicationError("remote branch changed since publication")
        return published
    if previous["intended_sha"] != candidate:
        raise PublicationError(
            "reconcile the pending publication before changing its candidate"
        )
    expected = previous.get("expected_remote_sha")
    if remote not in (expected, candidate):
        raise PublicationError("remote branch changed during publication")
    return expected


def _change_number(previous: dict[str, Any] | None) -> int | None:
    if previous is None or "change_number" not in previous:
        return None
    number = previous["change_number"]
    if type(number) is not int or number <= 0:
        raise PublicationError("publication intent contains an invalid change number")
    return number


def _verify_existing(
    change: PublishedChange,
    binding: dict[str, Any],
    remote: str | None,
    expected_number: int | None,
) -> None:
    expected = {
        "provider": binding["provider"],
        "host": binding["host"],
        "repository": binding["repository"],
        "branch": binding["branch"],
        "base": binding["base_branch"],
        "state": "OPEN",
        "head_sha": remote,
    }
    if expected_number is not None:
        expected["number"] = expected_number
    mismatches = [
        key for key, value in expected.items() if getattr(change, key) != value
    ]
    if mismatches:
        raise PublicationError("publication custody mismatch: " + ", ".join(mismatches))


def _verify_delivery(
    workspace: LocalWorkspace, branch: str, candidate: str, origin: str
) -> None:
    if workspace.branch_sha(branch) != candidate:
        raise PublicationError("local candidate branch changed during publication")
    if workspace.remote_sha(branch, origin_url=origin) != candidate:
        raise PublicationError("remote branch changed during publication")


def _publication_body(task: LocalTask) -> str:
    report = json.dumps(task.review_report, indent=2, ensure_ascii=False)
    return (
        f"{task.body.rstrip()}\n\n## AgentMachinist Evidence\n\n"
        f"Local Task {task.id}; approved Spec `{task.spec_sha}`.\n\n"
        f"Reviewed candidate: `{task.candidate_sha}`. Independent Review completed; "
        "findings remain advisory.\n\n"
        f"```json\n{report}\n```\n"
    )
