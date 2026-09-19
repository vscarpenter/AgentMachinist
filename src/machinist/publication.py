"""Publish a completed local candidate with durable, recoverable Git intent.

Publication is an explicit controller operation. It never starts a Harness,
reruns a Gate, mints Approval, or changes the local implementation candidate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from machinist.authorization import (
    AuthorizationError,
    authorization_evidence,
    validate_authorization,
)
from machinist.diagnostics import sanitize_diagnostic
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
    draft: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> LocalTask:
    """Publish one reviewed Task; retries reconcile the same intent and change.

    Cancellation is checked before external mutations. An already in-flight
    push or forge request cannot be undone; its saved intent remains recoverable.
    """
    if type(draft) is not bool:
        raise PublicationError("publication draft intent must be a boolean")

    def check_cancelled() -> bool:
        if cancel_check is not None and cancel_check():
            raise PublicationError("publication cancelled")
        return False

    with store.claim(task_id) as task:
        candidate = ready_candidate(task, workspace, lifecycle)
        if task.delegation is not None and not draft:
            raise PublicationError("delegated publication must begin as a draft")
        origin = workspace.origin_url()
        binding = _publication_binding(task, forge, origin)
        previous = _previous_publication(task.publication, binding)
        if (
            previous is not None
            and previous["stage"] == "publishing"
            and previous.get("draft", False) is not draft
        ):
            raise PublicationError("reconcile the pending publication draft intent")
        check_cancelled()
        workspace.bind_publication_auth(forge.provider, origin_url=origin)
        # Let the authenticated forge API refresh OAuth before Git reads the
        # stored credential. This is the same lookup needed for change custody.
        existing = forge.find_change(task.branch)
        remote = workspace.remote_sha(task.branch, origin_url=origin)
        expected_remote = _expected_remote(previous, candidate, remote)
        expected_number = _change_number(previous)
        if (
            previous is not None
            and previous["stage"] == "published"
            and expected_number is None
        ):
            raise PublicationError("published intent is missing its change number")
        if existing is not None:
            _verify_existing(existing, binding, remote, expected_number)
        elif expected_number is not None:
            raise PublicationError(
                "publication change number is missing from the forge"
            )

        if (
            previous is not None
            and previous["stage"] == "published"
            and previous.get("published_sha") == candidate
        ):
            # A separate CI coordinator or human may have made this PR ready.
            # Recovering completed publication must not reverse that transition.
            _verify_delivery(workspace, task.branch, candidate, origin)
            return task

        intent: dict[str, Any] = {
            **(previous or {}),
            **binding,
            "stage": "publishing",
            "intended_sha": candidate,
            "expected_remote_sha": expected_remote,
            "draft": draft,
        }
        if existing is not None:
            intent["change_number"] = existing.number
        task = store.update(task, publication=intent)
        if remote != candidate:
            check_cancelled()
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
            body=_publication_body(task, lifecycle),
            draft=draft,
            expected_number=intent.get("change_number"),
            cancel_check=check_cancelled if cancel_check is not None else None,
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
    authorization = None
    if task.delegation is not None:
        try:
            validate_authorization(task)
            authorization = authorization_evidence(task)
        except AuthorizationError as exc:
            raise PublicationError(f"Task authorization is invalid: {exc}") from exc
    elif not isinstance(approval, dict) or any(
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
            if (
                authorization is not None
                and evidence.as_dict().get("authorization") != authorization
            ):
                raise PublicationError(
                    f"{phase.value.title()} authorization Evidence does not match this delegation"
                )
            if phase is Phase.EXECUTE:
                valid = (
                    evidence.implementation_sha == candidate
                    and evidence.approved_sha == task.spec_sha
                )
                if authorization is not None:
                    _require_delegated_verification(evidence)
            else:
                valid = evidence.reviewed_sha == candidate
                if (
                    authorization is not None
                    and evidence.as_dict().get("review_report") != report
                ):
                    raise PublicationError(
                        "Review report does not match completed Phase Evidence"
                    )
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


def _require_delegated_verification(evidence: TaskEvidence) -> None:
    report = evidence.verification_report
    gates = report.get("gates") if report is not None else None
    if (
        report is None
        or report.get("success") is not True
        or not isinstance(gates, list)
        or not any(
            isinstance(gate, dict) and gate.get("required") is True for gate in gates
        )
        or any(
            not isinstance(gate, dict)
            or gate.get("blocking") is True
            or (gate.get("required") is True and gate.get("passed") is not True)
            for gate in gates
        )
    ):
        raise PublicationError(
            "delegated publication requires successful required Verification Evidence"
        )


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
        # An SSH transport port is not the forge's HTTPS API port. Git keeps
        # using the exact original URL; only API identity omits that SSH port.
        if parsed.scheme == "https" and parsed.port is not None:
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
    if "draft" in publication and type(publication["draft"]) is not bool:
        raise PublicationError("publication draft intent must be a boolean")
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


def _publication_body(task: LocalTask, lifecycle: TaskLifecycle | None = None) -> str:
    if task.delegation is not None:
        return _delegated_body(task, lifecycle)
    report = json.dumps(task.review_report, indent=2, ensure_ascii=False)
    return (
        f"{task.body.rstrip()}\n\n## AgentMachinist Evidence\n\n"
        f"Local Task {task.id}; approved Spec `{task.spec_sha}`.\n\n"
        f"Reviewed candidate: `{task.candidate_sha}`. Independent Review completed; "
        "findings remain advisory.\n\n"
        f"```json\n{report}\n```\n"
    )


def _delegated_body(task: LocalTask, lifecycle: TaskLifecycle | None) -> str:
    """Publish a bounded human handoff, keeping process logs and paths local."""
    assert task.delegation is not None
    report = task.review_report or {}
    runs = lifecycle or TaskLifecycle(Path(task.repository) / ".machinist/runs/local")
    execute = runs.record(task.number, Phase.EXECUTE)
    assert execute is not None  # ready_candidate proved the exact successful run.
    evidence = TaskEvidence.load(execute.evidence)
    verification = evidence.verification_report or {}

    def text(value: object, limit: int = 1200) -> str:
        rendered = (
            str(value)
            .replace(task.repository + "/", "")
            .replace(task.repository, "[repository]")
        )
        return sanitize_diagnostic(rendered, limit=limit)

    lines = [
        text(task.title),
        "",
        "## Local verification",
        "",
    ]
    gates = verification.get("gates")
    assert isinstance(gates, list)  # ready_candidate validated required gates.
    for gate in gates:
        if isinstance(gate, dict):
            kind = "required" if gate.get("required") is True else "advisory"
            lines.append(
                f"- {text(gate.get('name', 'Unnamed gate'), 120)} ({kind}): {text(gate.get('status', 'unknown'), 60)}."
            )
    lines.extend(
        [
            "",
            "Remote CI is evaluated separately on the exact candidate before this draft can become ready.",
            "",
            "## Automated review",
            "",
            text(report.get("summary", "Independent Review completed.")),
            "",
        ]
    )
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    if not findings:
        lines.append("No findings reported. Human review and merge remain required.")
    else:
        if any(
            isinstance(finding, dict) and finding.get("severity") == "high"
            for finding in findings
        ):
            lines.extend(
                ["High-severity findings need attention; this PR remains a draft.", ""]
            )
        for finding in findings[:20]:
            if not isinstance(finding, dict):
                continue
            location = text(finding.get("file", "unknown file"), 200)
            line = finding.get("line")
            if type(line) is int and line > 0:
                location += f":{line}"
            lines.append(
                f"- **{text(finding.get('severity', 'unknown'), 20)}** `{location}`: {text(finding.get('message', 'Review finding'), 800)}"
            )
            if finding.get("remediation"):
                lines.append(
                    f"  Suggested follow-up: {text(finding['remediation'], 500)}"
                )
        if len(findings) > 20:
            lines.append(
                f"- {len(findings) - 20} additional findings remain in the local Task record."
            )
        lines.extend(
            [
                "",
                "Findings are advisory evidence for human review; completing Review does not establish correctness.",
            ]
        )
    lines.extend(
        [
            "",
            "<details><summary>Task provenance</summary>",
            "",
            f"Task `{task.id}` delegated by {text(task.delegation['actor'], 100)}.",
            f"Internal Spec: `{task.spec_sha}`.",
            f"Verified and reviewed candidate: `{task.candidate_sha}`.",
            f"Policy digest: `{task.delegation['config_digest']}`.",
            "Full gate logs and detailed Phase Evidence remain in the local Task record.",
            "",
            "</details>",
            "",
        ]
    )
    return "\n".join(lines)
