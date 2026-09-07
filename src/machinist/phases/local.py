"""Claimed local Phases using the production custody and Verification modules.

Local delivery retains an exact Git ref and Task projection. Durable checkpoints
separate paid Harness work from ref/report delivery so retry can reconcile the
latter without repeating completed work.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from machinist.config import MachinistConfig
from machinist.evidence import TaskEvidence
from machinist.harness import harness_evidence
from machinist.lifecycle import Phase, RunStatus, TaskLifecycle
from machinist.local_tasks import LocalTask, LocalTaskStore
from machinist.local_workspace import LocalWorkspace
from machinist.managed_paths import read_managed_text, write_managed_text
from machinist.phases.execute import (
    _capture_harness_report,
    _enforce_change_limits,
    _run_verification,
    _verification_resume_blocker,
    render_implement_prompt,
)
from machinist.phases.progress import bind_harness_progress, report_progress
from machinist.phases.review import (
    _MAX_DIFF_BYTES,
    _prompt_sections,
    parse_review_report,
)
from machinist.phases.spec import render_spec_prompt
from machinist.phases.workshop_cleanup import finish_workshop_cleanup
from machinist.process import run_supervised
from machinist.verification import VerificationFailed, run_verification_gates

_FULL_SHA = re.compile(r"[0-9a-f]{40}")


class LocalPhaseError(Exception):
    """A local Phase could not prove its input or safely deliver its result."""


class LocalPhaseCancelled(LocalPhaseError):
    """A local Phase stopped at a controller-owned cancellation point."""

    cancelled = True


def run_local_spec(
    task: LocalTask,
    config: MachinistConfig,
    *,
    store: LocalTaskStore,
    workspace: LocalWorkspace,
    harness,
    claim,
    cancel_check=None,
    test_runner=run_supervised,
) -> LocalTask:
    """Baseline-check, generate and retain a Spec without a forge dependency."""
    start = task.spec_base_sha or task.candidate_sha or task.base_sha
    previous = _begin(
        task, config, store, workspace, harness, claim, Phase.SPEC, start, cancel_check
    )
    expected_ref = task.candidate_sha
    recovered_sha = _sha(previous.get("spec_sha"))
    if not recovered_sha and previous.get("local_commit_intent"):
        baseline = previous.get("baseline_report")
        if not isinstance(baseline, dict) or baseline.get("success") is not True:
            raise LocalPhaseError(
                "Spec commit recovery lacks successful baseline Evidence"
            )
        if not isinstance(previous.get("local_spec_digest"), str):
            raise LocalPhaseError("Spec commit recovery lacks its content digest")
        recovered_sha = _recover_commit(workspace, task, previous, start, cancel_check)
        claim.checkpoint(spec_sha=recovered_sha)
    if recovered_sha:
        path = _recover_delivery(workspace, task, previous, recovered_sha, expected_ref)
        spec = workspace.read_at_commit(
            recovered_sha, spec_path(task), max_bytes=config.limits.max_spec_chars * 4
        )
        _validate_spec(spec, config)
        if _digest(spec) != previous.get("local_spec_digest"):
            raise LocalPhaseError(
                "checkpointed Spec content changed; refusing recovery"
            )
        _cancel(cancel_check, "before Spec Task delivery")
        delivered = _deliver_spec(store, task, recovered_sha)
        claim.checkpoint(spec_sha=recovered_sha, local_delivery_completed=True)
        _cleanup(workspace, path, claim)
        return delivered
    if workspace.branch_sha(task.branch) != expected_ref:
        raise LocalPhaseError(
            "local Task branch changed before Spec; refusing overwrite"
        )
    if len(task.title) > 500 or len(task.body) > config.limits.max_issue_body_chars:
        raise LocalPhaseError("local Task input exceeds the configured Spec limits")
    path = _provision(workspace, task, claim, Phase.SPEC, start)
    # Failed Phases deliberately retain the Workshop, even under cleanup=always.
    # It is the only recoverable home for a clone's not-yet-delivered objects.
    _baseline(path, config, workspace, claim, test_runner, cancel_check)
    instructions = config.resolve_instructions("spec", path)
    claim.checkpoint(**config.instructions.evidence("spec", instructions))
    before = workspace.capture_harness_state(path)
    _cancel(cancel_check, "before Spec Harness")
    claim.checkpoint(harness=harness_evidence(harness, profile="spec"))
    report_progress(claim, "generate local Spec", task.id)
    bind_harness_progress(harness, claim, stage="spec")
    # The template contract is shared; only its forge-specific target wording changes.
    prompt = (
        render_spec_prompt(cast(Any, task), instructions)
        .replace(f"GitHub issue #{task.number}", f"Local Task {task.id}")
        .replace(f"(#{task.number})", f"({task.id})")
    )
    if task.feedback:
        prompt += f"\n\n## Human amendment feedback\n\n{task.feedback}\n"
    try:
        spec = harness.generate_spec(prompt, cwd=path)
    finally:
        workspace.assert_harness_state(path, before, read_only=True)
    _validate_spec(spec, config)
    _cancel(cancel_check, "before committing Spec")
    write_managed_text(path, spec_path(task), spec)
    message = f"docs(spec): local Task {task.id} implementation contract"
    intent = _prepare_commit(
        workspace, path, claim, start, message, local_spec_digest=_digest(spec)
    )
    workspace.commit_all(path, message)
    _verify_commit(workspace, path, intent)
    if workspace.has_changes(path):
        raise LocalPhaseError("Workshop changed while committing the local Spec")
    committed = workspace.head_sha(path)
    claim.checkpoint(spec_sha=committed, local_spec_digest=_digest(spec))
    _cancel(cancel_check, "before retaining Spec")
    workspace.retain_candidate(path, task.branch, expected_sha=expected_ref)
    if workspace.branch_sha(task.branch) != committed:
        raise LocalPhaseError("local Spec ref does not match its committed SHA")
    delivered = _deliver_spec(store, task, committed)
    claim.checkpoint(local_delivery_completed=True)
    _cleanup(workspace, path, claim)
    return delivered


def run_local_execute(
    task: LocalTask,
    config: MachinistConfig,
    *,
    store: LocalTaskStore,
    workspace: LocalWorkspace,
    harness,
    claim,
    cancel_check=None,
    test_runner=run_supervised,
    resume: bool = False,
) -> LocalTask:
    """Implement one exact local Approval with custody, limits and Verification."""
    _require_approval(task)
    assert task.spec_sha is not None
    spec_sha = task.spec_sha
    previous = _begin(
        task,
        config,
        store,
        workspace,
        harness,
        claim,
        Phase.EXECUTE,
        spec_sha,
        cancel_check,
    )
    claim.checkpoint(approved_sha=spec_sha)
    spec = workspace.read_at_commit(
        spec_sha, spec_path(task), max_bytes=config.limits.max_spec_chars * 4
    )
    _validate_spec(spec, config)
    implementation_sha = _sha(previous.get("implementation_sha"))
    if not implementation_sha and previous.get("local_commit_intent"):
        report = previous.get("verification_report")
        if not isinstance(report, dict) or report.get("success") is not True:
            raise LocalPhaseError(
                "commit recovery lacks successful Verification Evidence"
            )
        implementation_sha = _recover_commit(
            workspace, task, previous, spec_sha, cancel_check
        )
        claim.checkpoint(implementation_sha=implementation_sha)
    if implementation_sha:
        report = previous.get("verification_report")
        if not isinstance(report, dict) or report.get("success") is not True:
            raise LocalPhaseError(
                "committed implementation lacks successful Verification Evidence"
            )
        path = _recover_delivery(
            workspace, task, previous, implementation_sha, spec_sha
        )
        if (
            workspace.read_at_commit(
                implementation_sha,
                spec_path(task),
                max_bytes=config.limits.max_spec_chars * 4,
            )
            != spec
        ):
            raise LocalPhaseError("recovered implementation changed its approved Spec")
        _cancel(cancel_check, "before candidate Task delivery")
        delivered = _deliver_candidate(store, task, implementation_sha)
        claim.checkpoint(
            implementation_sha=implementation_sha, local_delivery_completed=True
        )
        _cleanup(workspace, path, claim)
        return delivered
    if workspace.branch_sha(task.branch) != spec_sha:
        raise LocalPhaseError("Spec branch changed after Approval")
    if resume:
        if not previous:
            raise LocalPhaseError(
                "no matching Execute checkpoint is available to resume"
            )
        path = _resume(workspace, task, previous, spec_sha)
        if workspace.change_snapshot(path) != previous.get("local_changes_snapshot"):
            raise LocalPhaseError(
                "retained Workshop bytes changed since the failed Task Run"
            )
        old_report = previous.get("verification_report")
        if isinstance(old_report, dict) and (
            blocker := _verification_resume_blocker(old_report)
        ):
            raise LocalPhaseError(
                f"cannot resume retained Workshop: {blocker}; use a fresh retry"
            )
        claim.checkpoint(
            workspace_path=str(path), git_custody=workspace.git_custody(path)
        )
    else:
        path = _provision(workspace, task, claim, Phase.EXECUTE, spec_sha)
        previous = {}
        claim.checkpoint(
            harness_completed=False,
            verification_report=None,
            local_verified_snapshot=None,
        )
    if _read_spec(path, task, config) != spec:
        raise LocalPhaseError(
            "retained Workshop does not contain the exact approved Spec"
        )
    instructions = config.resolve_instructions("execute", path)
    claim.checkpoint(**config.instructions.evidence("execute", instructions))
    if previous.get("harness_completed") is not True:
        allowed = (
            config.resolved_verification_gates()
            if config.verification.harness_may_run_gates
            else ()
        )
        harness.allowed_commands = tuple(gate.command for gate in allowed)
        before = workspace.capture_harness_state(path)
        _cancel(cancel_check, "before Execute Harness")
        claim.checkpoint(harness=harness_evidence(harness, profile="execute"))
        report_progress(claim, "implement local Task", task.id)
        bind_harness_progress(harness, claim, stage="execute")
        prompt = render_implement_prompt(
            task.number, spec, task.feedback, instructions, gates=allowed
        ).replace(f"GitHub issue #{task.number}", f"Local Task {task.id}")
        try:
            output = harness.implement(prompt, cwd=path)
        except BaseException:
            workspace.assert_harness_state(path, before)
            claim.checkpoint(
                local_changes_snapshot=workspace.change_snapshot(path),
                harness_completed=False,
            )
            raise
        workspace.assert_harness_state(path, before)
        _capture_harness_report(claim, output)
        if not workspace.has_changes(path):
            raise LocalPhaseError("Execute Harness made no implementation changes")
        summary = _enforce_change_limits(path, workspace=workspace, config=config)
        claim.checkpoint(
            harness_completed=True,
            change_summary=summary,
            local_changes_snapshot=workspace.change_snapshot(path),
        )
    if not workspace.has_changes(path):
        raise LocalPhaseError("no implementation changes remain to verify")
    _enforce_change_limits(path, workspace=workspace, config=config)
    verified = (
        previous.get("harness_completed") is True
        and isinstance(previous.get("verification_report"), dict)
        and previous["verification_report"].get("success") is True
        and previous.get("local_verified_snapshot") == workspace.change_snapshot(path)
    )
    if not verified:
        before = workspace.capture_harness_state(path)
        _cancel(cancel_check, "before Verification")
        try:
            _run_verification(
                path,
                config=config,
                workspace=workspace,
                claim=claim,
                test_runner=test_runner,
                cancel_check=cancel_check,
            )
        finally:
            workspace.assert_harness_state(path, before)
            claim.checkpoint(local_changes_snapshot=workspace.change_snapshot(path))
        if not workspace.has_changes(path):
            raise LocalPhaseError("Verification removed all implementation changes")
        summary = _enforce_change_limits(path, workspace=workspace, config=config)
        claim.checkpoint(
            change_summary=summary,
            local_verified_snapshot=workspace.change_snapshot(path),
        )
    _cancel(cancel_check, "before committing implementation")
    message = f"feat(agent): implement local Task {task.id} per approved Spec"
    intent = _prepare_commit(workspace, path, claim, spec_sha, message)
    workspace.commit_all(path, message)
    _verify_commit(workspace, path, intent)
    if workspace.has_changes(path):
        raise LocalPhaseError(
            "Workshop changed while committing the local implementation"
        )
    implementation_sha = workspace.head_sha(path)
    claim.checkpoint(approved_sha=spec_sha, implementation_sha=implementation_sha)
    _cancel(cancel_check, "before retaining local candidate")
    workspace.retain_candidate(path, task.branch, expected_sha=spec_sha)
    if workspace.branch_sha(task.branch) != implementation_sha:
        raise LocalPhaseError(
            "local candidate ref does not match the verified implementation"
        )
    delivered = _deliver_candidate(store, task, implementation_sha)
    claim.checkpoint(local_delivery_completed=True)
    _cleanup(workspace, path, claim)
    return delivered


def run_local_review(
    task: LocalTask,
    config: MachinistConfig,
    *,
    store: LocalTaskStore,
    workspace: LocalWorkspace,
    harness,
    claim,
    cancel_check=None,
    test_runner=run_supervised,
    execute_evidence: dict[str, Any] | None = None,
) -> LocalTask:
    """Review the exact local candidate; findings stay visible and advisory."""
    _require_approval(task)
    candidate = _sha(task.candidate_sha)
    if candidate is None:
        raise LocalPhaseError("local Review requires a delivered candidate")
    previous = _begin(
        task,
        config,
        store,
        workspace,
        harness,
        claim,
        Phase.REVIEW,
        candidate,
        cancel_check,
    )
    if workspace.branch_sha(task.branch) != candidate:
        raise LocalPhaseError("local candidate changed before Review")
    evidence = _execute_evidence(task, store, execute_evidence)
    old_report = previous.get("review_report")
    if previous.get("reviewed_sha") == candidate and isinstance(old_report, dict):
        report = _report_payload(parse_review_report(json.dumps(old_report)), candidate)
        path = _recover_delivery(workspace, task, previous, candidate, candidate)
        _cancel(cancel_check, "before Review report delivery")
        delivered = _deliver_review(store, task, report, claim)
        _cleanup(workspace, path, claim)
        return delivered
    path = _provision(workspace, task, claim, Phase.REVIEW, candidate)
    spec = _read_spec(path, task, config)
    assert task.spec_sha is not None
    if spec != workspace.read_at_commit(
        task.spec_sha, spec_path(task), max_bytes=config.limits.max_spec_chars * 4
    ):
        raise LocalPhaseError("local candidate changed its approved Spec")
    diff = workspace.diff_against(path, task.spec_sha, max_bytes=_MAX_DIFF_BYTES)
    instructions = config.resolve_instructions("review", path)
    claim.checkpoint(**config.instructions.evidence("review", instructions))
    prompt = _prompt_sections(
        task.number, task, spec, json.dumps(evidence, sort_keys=True), diff
    ).replace(f"## Task #{task.number}:", f"## Local Task {task.id}:")
    if instructions:
        prompt += f"\n\n## Repository Review instructions\n\n{instructions}"
    before = workspace.capture_harness_state(path)
    _cancel(cancel_check, "before independent Review")
    claim.checkpoint(harness=harness_evidence(harness, profile="review"))
    report_progress(claim, "independent local Review", task.id)
    bind_harness_progress(harness, claim, stage="review")
    try:
        output = harness.review(prompt, path)
    finally:
        workspace.assert_harness_state(path, before, read_only=True)
    report = _report_payload(parse_review_report(output), candidate)
    claim.checkpoint(
        reviewed_sha=candidate,
        review_report=report,
        finding_counts={
            level: sum(item["severity"] == level for item in report["findings"])
            for level in ("high", "medium", "low")
        },
    )
    if workspace.branch_sha(task.branch) != candidate:
        raise LocalPhaseError("local candidate changed during Review")
    _cancel(cancel_check, "before Review report delivery")
    delivered = _deliver_review(store, task, report, claim)
    _cleanup(workspace, path, claim)
    return delivered


def spec_path(task: LocalTask) -> str:
    return f".machinist/specs/task-{task.number}-spec.md"


def _begin(
    task, config, store, workspace, harness, claim, phase, input_sha, cancel_check
):
    if (
        task.repository != str(store.repo_root)
        or workspace.repo_root != store.repo_root
    ):
        raise LocalPhaseError(
            "Task, store and Workshop must belong to the same repository"
        )
    if claim.issue != task.number or claim.phase is not phase:
        raise LocalPhaseError("local Phase requires the exact Task and Phase Claim")
    current = store.get(task.id)
    if current != task:
        raise LocalPhaseError("local Task changed before the Phase began")
    if _sha(input_sha) is None:
        raise LocalPhaseError("local Phase input must be an exact commit SHA")
    workspace.cancel_check = cancel_check
    harness.cancel_check = cancel_check
    _cancel(cancel_check, "before local Phase")
    request_hash = _digest(
        json.dumps(
            {
                "task_id": task.id,
                "repository": task.repository,
                "branch": task.branch,
                "phase": phase.value,
                "input_sha": input_sha,
                "title": task.title,
                "body": task.body,
                "feedback": task.feedback,
                "verification": [
                    gate.model_dump(mode="json")
                    for gate in config.resolved_verification_gates()
                ],
                "limits": config.limits.model_dump(mode="json"),
                "instructions": config.instructions.model_dump(mode="json"),
            },
            sort_keys=True,
        )
    )
    previous = TaskEvidence.load(claim.previous_evidence).as_dict()
    if previous.get("local_request_hash") != request_hash:
        previous = {}
        clearing = {
            "workspace_path": None,
            "git_custody": None,
            "local_changes_snapshot": None,
            "local_verified_snapshot": None,
            "local_delivery_completed": False,
            "local_commit_intent": None,
            "local_commit_snapshot": None,
            "harness": None,
        }
        if phase is Phase.SPEC:
            clearing.update(spec_sha=None, local_spec_digest=None, baseline_report=None)
        elif phase is Phase.EXECUTE:
            clearing.update(
                implementation_sha=None,
                verification_report=None,
                harness_completed=False,
                change_summary=None,
            )
        else:
            clearing.update(reviewed_sha=None, review_report=None, finding_counts=None)
        claim.checkpoint(**clearing)
    claim.checkpoint(
        local_task_id=task.id,
        local_repository=task.repository,
        local_input_sha=input_sha,
        local_request_hash=request_hash,
    )
    return previous


def _provision(workspace, task, claim, phase, start):
    report_progress(claim, f"provision local {phase.value} Workshop", task.id)
    path = workspace.provision(
        f"task-{task.number}-{phase.value}", task.branch, start, attempt=claim.attempt
    )
    claim.checkpoint(
        workspace_path=str(path),
        git_custody=workspace.git_custody(path),
        local_changes_snapshot=workspace.change_snapshot(path),
    )
    return path


def _resume(workspace, task, previous, sha):
    path = previous.get("workspace_path")
    custody = previous.get("git_custody")
    if not isinstance(path, str) or not isinstance(custody, dict):
        raise LocalPhaseError("retained Workshop has no valid custody checkpoint")
    return workspace.resume(
        Path(path), branch=task.branch, expected_sha=sha, git_custody=custody
    )


def _prepare_commit(workspace, path, claim, parent, message, **evidence):
    intent = {
        "parent_sha": parent,
        "tree_sha": workspace.prepare_commit(path),
        "message": message,
    }
    claim.checkpoint(
        local_commit_intent=intent,
        local_commit_snapshot=workspace.change_snapshot(path),
        **evidence,
    )
    return intent


def _verify_commit(workspace, path, intent):
    identity = workspace.commit_identity(path)
    if (
        identity["parents"] != [intent["parent_sha"]]
        or identity["tree"] != intent["tree_sha"]
        or identity["message"] != intent["message"]
        or workspace.has_changes(path)
    ):
        raise LocalPhaseError(
            "controller commit does not match its exact parent, tree and message"
        )
    return identity["sha"]


def _recover_commit(workspace, task, previous, parent, cancel_check):
    intent = previous.get("local_commit_intent")
    raw_path = previous.get("workspace_path")
    custody = previous.get("git_custody")
    if (
        not isinstance(intent, dict)
        or intent.get("parent_sha") != parent
        or _sha(intent.get("tree_sha")) is None
        or not isinstance(intent.get("message"), str)
        or not isinstance(raw_path, str)
        or not isinstance(custody, dict)
    ):
        raise LocalPhaseError(
            "controller commit recovery has invalid checkpoint Evidence"
        )
    path = Path(raw_path)
    # Authenticate raw retained metadata before asking Git about an advanced HEAD.
    workspace.assert_git_custody(path, custody)
    observed = workspace.head_sha(path)
    workspace.resume(
        path, branch=task.branch, expected_sha=observed, git_custody=custody
    )
    if observed == parent:
        if workspace.change_snapshot(path) != previous.get("local_commit_snapshot"):
            raise LocalPhaseError("prepared commit bytes changed since Verification")
        if workspace.prepare_commit(path) != intent["tree_sha"]:
            raise LocalPhaseError("prepared commit tree changed since Verification")
        _cancel(cancel_check, "before recovering prepared commit")
        workspace.commit_all(path, intent["message"])
    return _verify_commit(workspace, path, intent)


def _recover_delivery(workspace, task, previous, sha, expected_ref):
    """Prove an immutable committed result before reconciling its local ref."""
    current_ref = workspace.branch_sha(task.branch)
    if current_ref not in (expected_ref, sha):
        raise LocalPhaseError("local delivery branch changed since its checkpoint")
    raw_path = previous.get("workspace_path")
    if isinstance(raw_path, str) and (
        Path(raw_path).exists() or Path(raw_path).is_symlink()
    ):
        path = _resume(workspace, task, previous, sha)
        if workspace.has_changes(path):
            raise LocalPhaseError("committed recovery Workshop has uncommitted changes")
        workspace.retain_candidate(path, task.branch, expected_sha=expected_ref)
        return path
    if current_ref != sha:
        raise LocalPhaseError(
            "committed Workshop is missing before local delivery; refusing to repeat paid work"
        )
    if workspace.resolve_commit(sha) != sha:
        raise LocalPhaseError("local delivered commit cannot be resolved")
    return None


def _baseline(path, config, workspace, claim, runner, cancel_check):
    report_progress(claim, "baseline Verification", "before Spec Harness")
    before = workspace.capture_harness_state(path)
    try:
        report = run_verification_gates(
            path,
            config.resolved_verification_gates(),
            log_dir=claim.log_directory("baseline-verification"),
            snapshotter=workspace.change_snapshot,
            runner=runner,
            cancel_check=cancel_check,
            on_progress=lambda index, total, name, status: report_progress(
                claim, f"baseline {index}/{total}: {name}", status
            ),
        )
    except VerificationFailed as exc:
        claim.checkpoint(baseline_report=exc.report.as_dict())
        raise
    else:
        claim.checkpoint(baseline_report=report.as_dict())
    finally:
        workspace.assert_harness_state(path, before, read_only=True)


def _require_approval(task):
    approval = task.approval
    if (
        not task.spec_sha
        or not isinstance(approval, dict)
        or any(
            approval.get(key) != value
            for key, value in {
                "task_id": task.id,
                "repository": task.repository,
                "spec_sha": task.spec_sha,
            }.items()
        )
    ):
        raise LocalPhaseError(
            "Approval must identify this repository, Task and exact Spec SHA"
        )
    actor, timestamp = approval.get("actor"), approval.get("approved_at")
    try:
        if (
            not isinstance(actor, str)
            or not actor.strip()
            or not isinstance(timestamp, str)
        ):
            raise ValueError("missing actor or time")
        if datetime.fromisoformat(timestamp).utcoffset() is None:
            raise ValueError("missing timezone")
    except ValueError as exc:
        raise LocalPhaseError("Approval must record its human actor and time") from exc


def _read_spec(path, task, config):
    value = read_managed_text(
        path, spec_path(task), max_bytes=config.limits.max_spec_chars * 4
    )
    _validate_spec(value, config)
    return value


def _validate_spec(value, config):
    if not isinstance(value, str) or not value.strip():
        raise LocalPhaseError("Spec Harness returned no usable Markdown")
    if len(value) > config.limits.max_spec_chars:
        raise LocalPhaseError("Spec exceeds the configured character limit")


def _deliver_spec(store, task, sha):
    if task.spec_sha == sha:
        return task
    return store.update(
        task,
        spec_sha=sha,
        approval=None,
        review_report=None,
        integration=None,
    )


def _deliver_candidate(store, task, sha):
    if task.candidate_sha == sha:
        return task
    return store.update(task, candidate_sha=sha, review_report=None, integration=None)


def _execute_evidence(task, store, supplied):
    if supplied is None:
        lifecycle = TaskLifecycle(store.tasks_dir.parent, repo_root=store.repo_root)
        record = lifecycle.record(task.number, Phase.EXECUTE)
        if record is None or record.status is not RunStatus.SUCCEEDED:
            raise LocalPhaseError("Review requires a successful Execute Task Run")
        supplied = record.evidence
    evidence = TaskEvidence.load(supplied)
    if (
        evidence.approved_sha != task.spec_sha
        or evidence.implementation_sha != task.candidate_sha
    ):
        raise LocalPhaseError("Execute Evidence does not match this Spec and candidate")
    report = evidence.verification_report
    if not isinstance(report, dict) or report.get("success") is not True:
        raise LocalPhaseError("Review requires successful Verification Evidence")
    return {
        "approved_sha": evidence.approved_sha,
        "implementation_sha": evidence.implementation_sha,
        "change_summary": evidence.change_summary,
        "verification_report": report,
    }


def _report_payload(report, sha):
    return {
        "version": report.version,
        "summary": report.summary,
        "findings": [asdict(item) for item in report.findings],
        "reviewed_sha": sha,
        "completed": True,
    }


def _deliver_review(store, task, report, claim):
    if task.review_report != report:
        task = store.update(task, review_report=report)
    lines = [
        f"# Local Task {task.id}: {task.title}",
        "",
        f"Candidate: `{task.candidate_sha}`",
        "",
        "Independent Review completed. Findings are advisory; human integration is a separate decision.",
        "",
        report["summary"],
        "",
        f"## Findings ({len(report['findings'])})",
    ]
    for finding in report["findings"]:
        lines.extend(
            [
                "",
                f"### {finding['severity']}: {finding['file']}:{finding['line']}",
                "",
                finding["message"],
                "",
                f"Remediation: {finding['remediation']}",
            ]
        )
    if not report["findings"]:
        lines.extend(["", "No findings reported."])
    path = store.save_report(task, "\n".join(lines) + "\n")
    claim.checkpoint(
        reviewed_sha=task.candidate_sha,
        review_report=report,
        local_report_path=str(path),
        local_delivery_completed=True,
    )
    return task


def _cleanup(workspace, path, claim):
    if path is not None:
        finish_workshop_cleanup(workspace, path, success=True, claim=claim)


def _cancel(check, stage):
    if check is not None and check():
        raise LocalPhaseCancelled(f"local Phase cancelled {stage}")


def _sha(value):
    return value if isinstance(value, str) and _FULL_SHA.fullmatch(value) else None


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
