"""Read-only inspection of one local Task and its exact saved candidate.

This view reads committed files and controller-owned Evidence. It never adopts
configuration, provisions a Workshop, invokes a Harness, or contacts a forge.
Display text is bounded and sanitized; it is not a replacement for the exact
committed source or raw logs.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any

from machinist.config import ConfigError
from machinist.diagnostics import sanitize_diagnostic
from machinist.evidence import EvidenceError, TaskEvidence
from machinist.lifecycle import (
    LifecycleError,
    Phase,
    RunRecord,
    RunStatus,
    TaskLifecycle,
)
from machinist.local_setup import load_local_config
from machinist.local_tasks import LocalTask, LocalTaskError, LocalTaskStore
from machinist.local_workspace import LocalWorkspace
from machinist.observability import build_run_report
from machinist.phases.review import ReviewPhaseError, parse_review_report
from machinist.process import run_supervised
from machinist.publication import PublicationError, ready_candidate
from machinist.transitions import classify_local_task
from machinist.verification import GateStatus
from machinist.workspace import Workspace, WorkspaceError

_MAX_DIFF_BYTES = 2 * 1024 * 1024
_MAX_SPEC_BYTES = 8 * 1024 * 1024
_MAX_SPEC_CHARS = 20_000
_MAX_DIFF_CHARS = 40_000
_MAX_ITEMS = 30
_MAX_HISTORY = 50


class LocalInspectionError(Exception):
    """A local Task cannot be safely read for inspection."""


def build_local_inspection(repo_root: Path, task_id: str) -> dict[str, Any]:
    """Build a bounded, JSON-safe decision view without changing local state.

    Incomplete and corrupt Evidence stays visible. A changed branch, mismatched
    Evidence, or failed read removes delivery eligibility from this view rather
    than showing an actionable integration command for an unproven candidate.
    """
    try:
        root = Path(repo_root).resolve()
        store = LocalTaskStore(root)
        task = store.get(task_id)
        config = load_local_config(root)
        lifecycle = TaskLifecycle(root / ".machinist/runs/local", repo_root=root)
        runs = build_run_report(lifecycle, issue=task.number)
        records: dict[Phase, RunRecord | None] = {phase: None for phase in Phase}
        records.update({record.phase: record for record in runs.current})
        decision = classify_local_task(
            task, records=records, claim_held=lifecycle.claim_held(task.number)
        )
        workspace = LocalWorkspace(root, config.workspace)
        issues: list[str] = []
        spec = _saved_spec(task, records[Phase.SPEC], workspace, issues)
        candidate: dict[str, Any] = {"sha": None, "verified": False, "reviewed": False}
        verification = _verification(records[Phase.SPEC], "baseline")
        execute = records[Phase.EXECUTE]
        if (
            _current_approval(task)
            and execute is not None
            and TaskEvidence.load(execute.evidence).approved_sha == task.spec_sha
            and isinstance(execute.evidence.get("verification_report"), dict)
        ):
            verification = _verification(execute, "execute")
        diff = None
        review = None

        if task.candidate_sha and _current_approval(task):
            candidate["sha"] = task.candidate_sha
            if _bound_execute(task, execute) and spec["verified"]:
                verification = _verification(execute, "execute")
                if verification is None or verification["success"] is not True:
                    issues.append("Candidate has no successful Verification report.")
                elif workspace.branch_sha(task.branch) != task.candidate_sha:
                    issues.append("Local candidate branch changed since Execute.")
                else:
                    candidate["verified"] = True
                    try:
                        diff = _exact_diff(root, workspace, task)
                    except WorkspaceError as exc:
                        issues.append(f"Cannot inspect the exact candidate diff: {exc}")
                    review = _exact_review(task, records[Phase.REVIEW], issues)
                    if review is not None and not issues:
                        ready_candidate(task, workspace, lifecycle)
                        candidate["reviewed"] = True
            else:
                issues.append(
                    "Execute Evidence does not match this Spec and candidate."
                )
        elif task.spec_sha and spec["verified"]:
            if workspace.branch_sha(task.branch) != task.spec_sha:
                issues.append("Local Spec branch changed since the saved plan.")

        # Recheck after collecting text: a concurrent Phase or Task change must
        # not combine the old plan with a new candidate or findings.
        if store.get(task.id) != task:
            issues.append("Task changed during inspection; inspect it again.")
        try:
            if any(
                lifecycle.record(task.number, phase) != record
                for phase, record in records.items()
            ):
                issues.append(
                    "Phase Evidence changed during inspection; inspect again."
                )
        except LifecycleError:
            issues.append("Current Phase Evidence is corrupt or unavailable.")
        expected_branch = task.candidate_sha if candidate["verified"] else task.spec_sha
        if expected_branch and workspace.branch_sha(task.branch) != expected_branch:
            issues.append("Local Task branch changed; inspect its commit history.")
            diff = None
            candidate["verified"] = False
        if task.candidate_sha and candidate["reviewed"] and not issues:
            ready_candidate(store.get(task.id), workspace, lifecycle)

        corrupt = [
            {
                "path": _text(artifact.path),
                "kind": _text(artifact.kind),
                "phase": artifact.phase,
                "attempt": artifact.attempt,
            }
            for artifact in runs.corrupt[:_MAX_ITEMS]
        ]
        if any(artifact.kind == "projection" for artifact in runs.corrupt):
            issues.append(
                "A current Task Run projection is corrupt; restore valid Evidence."
            )
        if issues:
            candidate["reviewed"] = False
        recovery_action = decision.next_action.startswith(
            (
                "machinist retry --task ",
                "machinist continue ",
                "machinist cancel --task ",
            )
        )
        state = (
            "evidence unavailable" if issues and not recovery_action else decision.state
        )
        action = (
            shlex.join(["git", "log", "--oneline", task.branch])
            if issues and not recovery_action
            else decision.next_action
        )
        return {
            "schema_version": 1,
            "task_id": task.id,
            "title": _text(task.title, limit=500),
            "objective": _text(task.body, limit=4_000),
            "state": state,
            "spec": spec,
            "candidate": candidate,
            "diff": diff,
            "verification": verification,
            "review": review,
            "history": [
                _attempt(record, lifecycle) for record in runs.history[-_MAX_HISTORY:]
            ],
            "history_total": len(runs.history),
            "corrupt": corrupt,
            "corrupt_total": len(runs.corrupt),
            "issues": [_text(issue) for issue in dict.fromkeys(issues)],
            "next_action": _text(action),
        }
    except (
        ConfigError,
        LocalTaskError,
        LifecycleError,
        WorkspaceError,
        PublicationError,
        EvidenceError,
        OSError,
        ValueError,
    ) as exc:
        raise LocalInspectionError(
            sanitize_diagnostic(f"Cannot inspect local Task {task_id}: {exc}")
        ) from exc


def render_local_inspection(
    repo_root: Path, task_id: str, *, as_json: bool = False
) -> str:
    """Return human text or JSON for the CLI to print; no files are written."""
    payload = build_local_inspection(repo_root, task_id)
    if as_json:
        return json.dumps(payload, indent=2, sort_keys=True)
    lines = [
        f"{payload['task_id']}: {payload['title']}",
        f"State: {payload['state']}",
        "",
        "Objective",
        payload["objective"],
        "",
        "Saved plan",
    ]
    spec = payload["spec"]
    if spec["sha"]:
        lines.append(f"Spec commit: {spec['sha']}")
    lines.append(spec["text"] or "No verified saved plan is available yet.")
    if spec["truncated"] and spec["command"]:
        lines.extend(
            [
                "Read the complete plan before approving; this preview is truncated.",
                f"Full plan: {spec['command']}",
            ]
        )
    candidate = payload["candidate"]
    if candidate["sha"]:
        lines.extend(["", f"Candidate commit: {candidate['sha']}"])
    diff = payload["diff"]
    if diff is not None:
        lines.extend(
            [
                "",
                "Changed files",
                *diff["changed_files"],
                "",
                "Candidate diff",
                diff["text"],
            ]
        )
        if diff["truncated"]:
            lines.append(f"Full diff: {diff['command']}")
    verification = payload["verification"]
    lines.extend(["", "Check results"])
    if verification is None:
        lines.append("No Verification report is available for this Task yet.")
    else:
        lines.append(f"Source: {verification['phase']}")
        for gate in verification["gates"]:
            requirement = "required" if gate["required"] else "advisory"
            lines.append(f"  {gate['name']}: {gate['status']} ({requirement})")
            if gate.get("error"):
                lines.append(f"    {gate['error']}")
            if gate.get("stderr_excerpt"):
                lines.append(f"    {gate['stderr_excerpt']}")
    lines.extend(["", "Independent Review"])
    review = payload["review"]
    if review is None:
        lines.append("No completed Review is bound to this exact candidate.")
    else:
        lines.extend([f"Reviewed commit: {review['reviewed_sha']}", review["summary"]])
        for finding in review["findings"]:
            lines.extend(
                [
                    f"  {finding['severity']} / {finding['confidence']} confidence: {finding['file']}:{finding['line']}",
                    f"    Requirement: {finding['requirement']}",
                    f"    {finding['message']}",
                    f"    Suggested fix: {finding['remediation']}",
                ]
            )
        if not review["findings"]:
            lines.append("No findings reported.")
        if review["finding_count"] > len(review["findings"]):
            lines.append(
                f"Showing the first {len(review['findings'])} of {review['finding_count']} findings."
            )
        lines.append(
            "Findings are advisory; accepting this change remains your decision."
        )
    lines.extend(["", "Attempt history"])
    for attempt in payload["history"]:
        lines.append(
            f"  {attempt['phase']} attempt {attempt['attempt']}: {attempt['status']}"
        )
        if attempt["phase"] == "spec":
            if attempt["spec_sha"]:
                lines.append(f"    Plan commit: {attempt['spec_sha']}")
            if attempt["local_feedback"]:
                lines.append(f"    Requested plan change: {attempt['local_feedback']}")
        if attempt["error"]:
            lines.extend([f"    {attempt['error']}", f"    Logs: {attempt['log_dir']}"])
    if payload["history_total"] > len(payload["history"]):
        lines.append(
            f"Showing the latest {len(payload['history'])} of {payload['history_total']} attempts."
        )
    for artifact in payload["corrupt"]:
        lines.append(
            f"Corrupt Task Run artifact ({artifact['kind']}): {artifact['path']}"
        )
    for issue in payload["issues"]:
        lines.append(f"Inspection issue: {issue}")
    lines.extend(["", f"Next: {payload['next_action']}"])
    return "\n".join(lines)


def _text(value: object, *, limit: int = 1_000) -> str:
    return sanitize_diagnostic(value, limit=limit)


def _saved_spec(
    task: LocalTask,
    record: RunRecord | None,
    workspace: LocalWorkspace,
    issues: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sha": task.spec_sha,
        "verified": False,
        "text": None,
        "truncated": False,
        "command": None,
    }
    if task.spec_sha is None:
        return result
    if record is None or TaskEvidence.load(record.evidence).spec_sha != task.spec_sha:
        issues.append("Saved Spec does not match successful Spec Evidence.")
        return result
    if record.status is not RunStatus.SUCCEEDED:
        issues.append(
            "Saved Spec delivery is incomplete; retry the Spec Phase before Approval."
        )
    try:
        raw = workspace.read_at_commit(
            task.spec_sha,
            f".machinist/specs/task-{task.number}-spec.md",
            max_bytes=_MAX_SPEC_BYTES,
        )
    except WorkspaceError as exc:
        issues.append(f"Cannot read the exact saved Spec: {exc}")
        return result
    result.update(
        verified=record.status is RunStatus.SUCCEEDED,
        text=_text(raw, limit=_MAX_SPEC_CHARS),
        truncated=len(sanitize_diagnostic(raw, limit=len(raw))) > _MAX_SPEC_CHARS,
        command=shlex.join(
            [
                "git",
                "show",
                f"{task.spec_sha}:.machinist/specs/task-{task.number}-spec.md",
            ]
        ),
    )
    return result


def _current_approval(task: LocalTask) -> bool:
    approval = task.approval
    if approval is None:
        return False
    return all(
        approval.get(key) == value
        for key, value in {
            "repository": task.repository,
            "task_id": task.id,
            "spec_sha": task.spec_sha,
        }.items()
    )


def _bound_execute(task: LocalTask, record: RunRecord | None) -> bool:
    if record is None or record.status is not RunStatus.SUCCEEDED:
        return False
    evidence = TaskEvidence.load(record.evidence)
    return (
        evidence.approved_sha == task.spec_sha
        and evidence.implementation_sha == task.candidate_sha
    )


def _exact_diff(root: Path, local: LocalWorkspace, task: LocalTask) -> dict[str, Any]:
    assert task.spec_sha and task.candidate_sha
    # Resolution validates exact commit references through the Workshop's
    # custody boundary. Git receives no caller-supplied flags or external tools.
    for sha in (task.spec_sha, task.candidate_sha):
        if local.resolve_commit(sha) != sha:
            raise WorkspaceError("Task diff reference is not its exact commit")
    git = Workspace(
        root,
        local.config,
        runner=partial(run_supervised, max_output_bytes=_MAX_DIFF_BYTES),
    )
    controls = (
        "--no-optional-locks",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
    )
    refs = (task.spec_sha, task.candidate_sha, "--")
    names = git._git(root, *controls, "--name-only", "-z", *refs)
    raw = git._git(root, *controls, "--no-renames", *refs)
    changed_files = [name for name in names.split("\0") if name]
    text = _text(raw, limit=_MAX_DIFF_CHARS)
    return {
        "base_sha": task.spec_sha,
        "candidate_sha": task.candidate_sha,
        "changed_files": [
            _text(name, limit=500) for name in changed_files[:_MAX_ITEMS]
        ],
        "changed_file_count": len(changed_files),
        "text": text,
        "truncated": len(sanitize_diagnostic(raw, limit=len(raw))) > _MAX_DIFF_CHARS,
        "command": shlex.join(["git", "diff", "--no-ext-diff", "--no-textconv", *refs]),
    }


def _verification(record: RunRecord | None, phase: str) -> dict[str, Any] | None:
    if record is None:
        return None
    evidence = TaskEvidence.load(record.evidence)
    report = (
        evidence.verification_report
        if phase == "execute"
        else record.evidence.get("baseline_report")
    )
    if not isinstance(report, dict):
        return None
    gates = report.get("gates")
    if not isinstance(gates, list) or not gates:
        return None
    displayed = []
    success = report.get("success") is True
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            return None
        name = gate.get("name")
        command = gate.get("command")
        status_value = gate.get("status")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(command, str)
            or type(gate.get("required")) is not bool
            or not isinstance(status_value, str)
        ):
            return None
        try:
            status = GateStatus(status_value)
        except ValueError:
            return None
        if gate.get("blocking") is True:
            success = False
        if gate["required"] and status is not GateStatus.PASSED:
            success = False
        if index >= _MAX_ITEMS:
            continue
        displayed.append(
            {
                key: _text(gate[key])
                for key in (
                    "name",
                    "command",
                    "status",
                    "error",
                    "stdout_excerpt",
                    "stderr_excerpt",
                )
                if isinstance(gate.get(key), str)
            }
            | {"required": gate.get("required") is True}
        )
    return {
        "phase": phase,
        "success": success,
        "gates": displayed,
        "gate_count": len(gates),
    }


def _exact_review(
    task: LocalTask, record: RunRecord | None, issues: list[str]
) -> dict[str, Any] | None:
    if record is None or record.status is not RunStatus.SUCCEEDED:
        return None
    evidence = TaskEvidence.load(record.evidence)
    raw = record.evidence.get("review_report")
    if not isinstance(raw, dict):
        issues.append("Review report is missing from successful Review Evidence.")
        return None
    if (
        evidence.reviewed_sha != task.candidate_sha
        or raw.get("reviewed_sha") != task.candidate_sha
        or raw.get("completed") is not True
        or raw != task.review_report
    ):
        issues.append(
            "Review report does not match successful exact-candidate Evidence."
        )
        return None
    try:
        parsed = parse_review_report(json.dumps(raw))
    except ReviewPhaseError as exc:
        issues.append(f"Stored Review report is invalid: {exc}")
        return None
    findings = []
    for finding in parsed.findings[:_MAX_ITEMS]:
        findings.append(
            {
                key: _text(value) if isinstance(value, str) else value
                for key, value in asdict(finding).items()
            }
        )
    return {
        "completed": True,
        "reviewed_sha": task.candidate_sha,
        "summary": _text(parsed.summary, limit=2_000),
        "findings": findings,
        "finding_count": len(parsed.findings),
    }


def _attempt(record: RunRecord, lifecycle: TaskLifecycle) -> dict[str, Any]:
    evidence = TaskEvidence.load(record.evidence)
    feedback = record.evidence.get("local_feedback")
    return {
        "phase": record.phase.value,
        "attempt": record.attempt,
        "status": record.status.value,
        "started_at": _text(record.started_at, limit=100),
        "updated_at": _text(record.updated_at, limit=100),
        "error": _text(record.error, limit=700) if record.error else None,
        "spec_sha": evidence.spec_sha,
        "local_feedback": _text(feedback) if isinstance(feedback, str) else None,
        "log_dir": _text(
            lifecycle.attempt_log_directory(record.issue, record.phase, record.attempt),
            limit=500,
        ),
    }
