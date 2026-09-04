"""Phase 3: approved spec -> implementation -> PR ready for review.

The controller owns every Git and GitHub transition. Harness and verification
processes may edit the Workshop, but Execute asserts custody before publishing,
records intent before external side effects, and can reconcile a crash after a
successful push without rerunning the harness.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from importlib.resources import files
from pathlib import Path, PurePosixPath
from string import Template
from typing import Any

from machinist.config import MachinistConfig, VerificationGateConfig
from machinist.evidence import EvidenceError, TaskEvidence
from machinist.github import PullRequest
from machinist.harness import harness_evidence
from machinist.managed_paths import ManagedPathError, read_managed_text
from machinist.phases.progress import bind_harness_progress, report_progress
from machinist.phases.workshop_cleanup import finish_workshop_cleanup
from machinist.repository_custody import (
    PullRequestExpectation,
    RepositoryCustodyError,
    bind_repository,
    same_repository_pr,
    validate_pr_base,
    verify_pull_request,
)
from machinist.runtime_paths import RuntimePathError, write_text_file
from machinist.verification import (
    GateStatus,
    VerificationError,
    VerificationFailed,
    VerificationReport,
    run_verification_gates,
)

_IMPLEMENT_PROMPT = files("machinist") / "templates" / "implement-prompt.md"
MAX_FEEDBACK_CHARS = 50_000
MAX_FEEDBACK_FILE_BYTES = MAX_FEEDBACK_CHARS * 4
_MAX_HARNESS_REPORT_CHARS = 2_000
_MAX_UTF8_BYTES_PER_CHAR = 4


class ExecutePhaseError(Exception):
    """Phase 3 refused to run or failed to produce a shippable change."""


class ExecutePhaseCancelled(ExecutePhaseError):
    """Execute stopped cooperatively during verification."""

    cancelled = True


# Deterministic heuristics for "this path is a test". Renames appear as a
# deletion plus an addition, so renaming a test file also trips the check;
# limits.allow_test_deletions is the per-repository escape hatch.
_TEST_DIRECTORY_SEGMENTS = frozenset({"tests", "test", "__tests__", "spec"})
_TEST_BASENAME_PATTERNS = ("test_*", "*_test.*", "*.test.*", "*.spec.*", "conftest.py")


def _is_test_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    if any(part in _TEST_DIRECTORY_SEGMENTS for part in path.parts[:-1]):
        return True
    return any(
        fnmatch.fnmatch(path.name, pattern) for pattern in _TEST_BASENAME_PATTERNS
    )


def render_implement_prompt(
    issue_number: int,
    spec_text: str,
    feedback: str | None = None,
    instructions: str = "",
    gates: Sequence[VerificationGateConfig] = (),
) -> str:
    feedback = normalize_operator_feedback(feedback)
    template = Template(_IMPLEMENT_PROMPT.read_text())
    prompt = template.safe_substitute(number=issue_number, spec=spec_text)
    if gates:
        listing = "\n".join(
            f"- {gate.name} "
            f"({'required' if gate.required else 'advisory'}): `{gate.command}`"
            for gate in gates
        )
        prompt += (
            "\n\n## Verifying your work\n\n"
            "This repository verifies the implementation with these gate "
            "commands before anything is committed:\n\n"
            f"{listing}\n\n"
            "Run each required gate command from the repository root and "
            "iterate until it passes before you finish. If a gate fails, fix "
            "the code — never delete, skip, or weaken a test to make it "
            "pass. Include each gate's final result in your final message.\n"
        )
    if feedback:
        prompt += (
            "\n\n## Operator feedback for this amendment\n\n"
            "--- BEGIN OPERATOR FEEDBACK ---\n"
            f"{feedback}\n"
            "--- END OPERATOR FEEDBACK ---\n"
        )
    if instructions:
        prompt += (
            "\n\n## Repository instructions\n\n"
            "--- BEGIN REPOSITORY INSTRUCTIONS ---\n"
            f"{instructions}\n"
            "--- END REPOSITORY INSTRUCTIONS ---\n"
        )
    return prompt


def normalize_operator_feedback(feedback: str | None) -> str | None:
    """Return meaningful bounded feedback or fail before a Task is claimed."""
    if feedback is None:
        return None
    normalized = feedback.strip()
    if not normalized:
        raise ExecutePhaseError("operator feedback must contain non-whitespace text")
    if len(normalized) > MAX_FEEDBACK_CHARS:
        raise ExecutePhaseError(
            f"operator feedback is too large ({len(normalized)} characters; "
            f"maximum {MAX_FEEDBACK_CHARS})"
        )
    return normalized


def run_execute_phase(
    issue_number: int,
    config: MachinistConfig,
    *,
    github,
    harness,
    workspace,
    test_runner=subprocess.run,
    force: bool = False,
    claim,
    resume: bool = False,
    feedback: str | None = None,
    cancel_check=None,
) -> PullRequest:
    """Implement an approved Task with durable retry and reconciliation state."""
    started_at = time.monotonic()
    report_progress(claim, "read approved Task", f"issue #{issue_number}")
    # Validate before performing any GitHub or filesystem operation.
    feedback = normalize_operator_feedback(feedback)
    if feedback and resume:
        raise ExecutePhaseError(
            "operator feedback requires a fresh implementation attempt; "
            "it cannot be applied while resuming completed harness output"
        )

    branch = f"{config.workspace.branch_prefix}issue-{issue_number}"
    repository_identity = _bind_repository(config, github, workspace)
    pr = next(
        (
            candidate
            for candidate in github.open_machinist_prs(config.workspace.branch_prefix)
            if candidate.branch == branch
            and same_repository_pr(candidate, repository_identity)
        ),
        None,
    )
    if pr is None:
        raise ExecutePhaseError(
            f"no open PR for branch '{branch}'; run 'machinist spec {issue_number}' first"
        )
    previous = TaskEvidence.load(claim.previous_evidence)
    try:
        base = previous.pr_base() or github.default_branch()
    except EvidenceError as exc:
        raise ExecutePhaseError(
            "prior Execute checkpoint has an invalid PR base"
        ) from exc
    _require_pr_base(base, source="GitHub default branch")
    if pr.base and pr.base != base:
        raise ExecutePhaseError(
            f"PR #{pr.number} targets base {pr.base!r}; expected {base!r}; "
            "approve a PR targeting the bound base branch"
        )
    claim.checkpoint(pr_base=base)
    approved_label = config.github.labels.approved
    if approved_label not in pr.labels:
        raise ExecutePhaseError(
            f"PR #{pr.number} is not approved; apply the '{approved_label}' label "
            f"or comment '/machinist-execute {pr.head_sha}' on it first"
        )

    approval_sha = github.approval_sha(pr.number)
    reconciled_sha = _reconciled_push(
        previous,
        approval_sha,
        pr,
        read_remote_sha=lambda: workspace.remote_sha(workspace.repo_root, branch),
        force=force or bool(feedback),
    )
    if reconciled_sha is not None:
        report_progress(claim, "reconcile PR delivery", reconciled_sha[:12])
        claim.checkpoint(push_observed_sha=reconciled_sha)
        _complete_delivery(
            issue_number,
            pr,
            github=github,
            claim=claim,
            implementation_sha=reconciled_sha,
            change_summary=previous.change_summary,
            verification_report=previous.verification_report,
            comment_id=previous.completion_comment_id,
            recovered=True,
            harness_details=previous.harness,
            harness_report_excerpt=previous.harness_report_excerpt,
            attempt=claim.attempt,
            duration_seconds=_duration(started_at),
            branch=branch,
            expected_base=base,
            cancel_check=cancel_check,
            review_enabled=config.review.enabled,
        )
        return pr

    if approval_sha is None:
        raise ExecutePhaseError(
            f"PR #{pr.number} has the approval label but no SHA-bound approval evidence; "
            f"re-apply the label or comment '/machinist-execute {pr.head_sha}'"
        )
    if approval_sha != pr.head_sha:
        raise ExecutePhaseError(
            f"PR #{pr.number} changed after approval; approve current head {pr.head_sha} again"
        )
    if not pr.is_draft and not force:
        raise ExecutePhaseError(
            f"PR #{pr.number} is already marked ready for review (implemented). "
            "Re-run with --force to implement again on top of this branch."
        )

    if resume:
        claim.checkpoint(
            feedback_supplied=previous.feedback_supplied,
            feedback_characters=previous.feedback_characters,
        )
    else:
        claim.checkpoint(
            feedback_supplied=bool(feedback),
            feedback_characters=len(feedback or ""),
        )

    if resume:
        report_progress(claim, "resume Workshop", branch)
        path, resume_stage = _resume_workspace(
            workspace,
            claim,
            previous,
            branch=branch,
            approval_sha=approval_sha,
        )
    else:
        report_progress(claim, "provision Workshop", branch)
        path = _provision_fresh_workspace(
            workspace,
            claim,
            previous,
            issue_number=issue_number,
            branch=branch,
            base_ref=f"origin/{base}",
            approval_sha=approval_sha,
        )
        resume_stage = "harness"

    comment_id = previous.completion_comment_id
    try:
        if resume_stage == "push":
            # A crash after the implementation commit: resume asserted that the
            # Workshop HEAD is the checkpointed implementation, so re-enter the
            # shared push step below without the Harness, gates, or commit.
            report_progress(claim, "resume push", branch)
            implementation_sha = previous.intended_push_sha
            assert implementation_sha is not None
            change_summary = previous.change_summary
            verification_report = previous.verification_report
            harness_details = previous.harness
            harness_report_excerpt = previous.harness_report_excerpt
        else:
            _assert_head(workspace, path, branch=branch, expected_sha=approval_sha)
            instructions = ""
            if resume_stage != "post_harness":
                instructions = config.resolve_instructions("execute", path)
                claim.checkpoint(
                    **config.instructions.evidence("execute", instructions)
                )
            if not resume:
                _reset_fresh_execution_evidence(claim)
            spec_relative = (
                Path(".machinist") / "specs" / f"issue-{issue_number}-spec.md"
            )
            try:
                spec_text = read_managed_text(
                    path,
                    spec_relative,
                    max_bytes=(config.limits.max_spec_chars * _MAX_UTF8_BYTES_PER_CHAR),
                )
            except ManagedPathError as exc:
                raise ExecutePhaseError(
                    f"cannot safely read approved Spec: {exc}"
                ) from exc
            if spec_text is None:
                raise ExecutePhaseError(
                    f"spec file {spec_relative} not found on branch '{branch}'"
                )
            if len(spec_text) > config.limits.max_spec_chars:
                raise ExecutePhaseError(
                    f"approved Spec has {len(spec_text)} characters; maximum is "
                    f"{config.limits.max_spec_chars}"
                )

            if resume_stage != "post_harness":
                harness_details = _harness_details(harness)
                harness_report_excerpt: str | None
                claim.checkpoint(
                    harness=harness_details,
                )
                gates = (
                    config.resolved_verification_gates()
                    if config.verification.harness_may_run_gates
                    else ()
                )
                harness.allowed_commands = tuple(gate.command for gate in gates)
                report_progress(claim, "implement", harness.name)
                bind_harness_progress(harness, claim, stage="implement")
                harness_report = harness.implement(
                    render_implement_prompt(
                        issue_number,
                        spec_text,
                        feedback,
                        instructions,
                        gates=gates,
                    ),
                    cwd=path,
                )
                harness_report_excerpt = _capture_harness_report(
                    claim,
                    harness_report,
                )
                _assert_head(
                    workspace,
                    path,
                    branch=branch,
                    expected_sha=approval_sha,
                    actor=harness.name,
                )
            else:
                harness_details = previous.harness or _harness_details(harness)
                harness_report_excerpt = previous.harness_report_excerpt

            if not workspace.has_changes(path):
                raise ExecutePhaseError(
                    f"{harness.name} made no changes for issue #{issue_number}"
                )

            change_summary = _enforce_change_limits(
                path,
                workspace=workspace,
                config=config,
            )
            claim.checkpoint(
                harness_completed=True,
                change_summary=change_summary,
            )

            try:
                report_progress(claim, "verification", "starting configured gates")
                verification_report = _run_verification(
                    path,
                    config=config,
                    workspace=workspace,
                    claim=claim,
                    test_runner=test_runner,
                    cancel_check=cancel_check,
                )
            except ExecutePhaseError:
                _assert_head(
                    workspace,
                    path,
                    branch=branch,
                    expected_sha=approval_sha,
                    actor="verification gate",
                )
                raise

            _assert_head(
                workspace,
                path,
                branch=branch,
                expected_sha=approval_sha,
                actor="verification gate",
            )
            if not workspace.has_changes(path):
                raise ExecutePhaseError(
                    "verification gates removed all implementation changes; "
                    "there is nothing to commit"
                )
            # Mutation-allowed gates may format or generate files. Reapply all
            # limits to the exact tree that will be committed and reported.
            change_summary = _enforce_change_limits(
                path,
                workspace=workspace,
                config=config,
            )
            claim.checkpoint(change_summary=change_summary)

            _raise_if_cancelled(cancel_check, "before commit")
            report_progress(claim, "commit implementation", branch)
            workspace.commit_all(
                path,
                f"feat(agent): implement issue #{issue_number} per approved spec",
            )
            if workspace.has_changes(path):
                raise ExecutePhaseError(
                    "the Workshop changed while AgentMachinist committed it; "
                    "refusing to push a partially captured implementation"
                )
            implementation_sha = workspace.head_sha(path)
            claim.checkpoint(
                approved_sha=approval_sha,
                implementation_sha=implementation_sha,
                push_intended_sha=implementation_sha,
                push_observed_sha=None,
            )
        # One leased push for fresh and resumed runs: a remote already at the
        # implementation is a no-op, one that moved elsewhere fails the lease.
        _raise_if_cancelled(cancel_check, "before push")
        report_progress(claim, "push implementation", branch)
        workspace.push(path, branch, expected_sha=approval_sha)
        observed_sha = workspace.remote_sha(path, branch)
        if observed_sha != implementation_sha:
            raise ExecutePhaseError(
                "push returned without publishing the intended implementation "
                f"({observed_sha or 'missing'} != {implementation_sha})"
            )
        claim.checkpoint(push_observed_sha=implementation_sha)
        _complete_delivery(
            issue_number,
            pr,
            github=github,
            claim=claim,
            implementation_sha=implementation_sha,
            change_summary=change_summary,
            verification_report=verification_report,
            comment_id=comment_id,
            recovered=resume_stage == "push",
            harness_details=harness_details,
            harness_report_excerpt=harness_report_excerpt,
            attempt=claim.attempt,
            duration_seconds=_duration(started_at),
            branch=branch,
            expected_base=base,
            cancel_check=cancel_check,
            review_enabled=config.review.enabled,
        )
    except BaseException as exc:
        cleanup_warning = finish_workshop_cleanup(
            workspace, path, success=False, claim=claim
        )
        if cleanup_warning is not None:
            exc.add_note(cleanup_warning)
        raise

    finish_workshop_cleanup(workspace, path, success=True, claim=claim)
    return pr


def _provision_fresh_workspace(
    workspace,
    claim,
    previous: TaskEvidence,
    *,
    issue_number: int,
    branch: str,
    base_ref: str,
    approval_sha: str,
) -> Path:
    attempt = claim.attempt if claim.attempt > 1 else None
    path = workspace.provision(
        f"issue-{issue_number}", branch, base_ref, attempt=attempt
    )

    prior_paths = previous.prior_workspace_paths
    previous_path = previous.workspace_path
    if (
        previous_path is not None
        and previous_path != str(path)
        and previous_path not in prior_paths
    ):
        prior_paths.append(previous_path)
    # The Workshop captured its custody token during provision; persist it so
    # a fresh process can hand it back on resume.
    claim.checkpoint(
        approved_sha=approval_sha,
        workspace_path=str(path),
        prior_workspace_paths=prior_paths,
        git_custody=workspace.git_custody(path),
    )
    return path


def _reset_fresh_execution_evidence(claim) -> None:
    """Clear stale stage evidence only after the approved head is revalidated."""
    claim.checkpoint(
        harness_completed=False,
        harness=None,
        harness_report_path=None,
        harness_report_excerpt=None,
        verification_report=None,
        change_summary=None,
        implementation_sha=None,
        push_intended_sha=None,
        push_observed_sha=None,
    )


def _resume_workspace(
    workspace,
    claim,
    previous: TaskEvidence,
    *,
    branch: str,
    approval_sha: str,
) -> tuple[Path, str]:
    resume_blocker = _verification_resume_blocker(previous.verification_report or {})
    if resume_blocker:
        raise ExecutePhaseError(
            "retained workspace is not eligible for --resume because "
            f"{resume_blocker}; start a fresh retry"
        )
    if previous.approved_sha != approval_sha:
        raise ExecutePhaseError(
            "retained workspace was created for a different approved SHA; "
            "start a fresh retry"
        )
    raw_path = previous.workspace_path
    if not raw_path:
        raise ExecutePhaseError(
            "previous Task Run has no retained workspace checkpoint to resume"
        )

    intended_sha = previous.intended_push_sha
    implementation_sha = previous.implementation_sha
    if intended_sha is not None and intended_sha == implementation_sha:
        expected_sha = intended_sha
        stage = "push"
    elif previous.harness_completed:
        expected_sha = approval_sha
        stage = "post_harness"
    else:
        expected_sha = approval_sha
        stage = "harness"

    try:
        path = workspace.resume(
            Path(raw_path),
            branch=branch,
            expected_sha=expected_sha,
            git_custody=previous.git_custody,
        )
    except Exception as exc:
        raise ExecutePhaseError(
            f"retained workspace {raw_path} cannot be resumed safely: {exc}"
        ) from exc
    claim.checkpoint(
        workspace_path=str(path),
    )
    return path, stage


def _assert_head(
    workspace,
    path: Path,
    *,
    branch: str,
    expected_sha: str,
    actor: str | None = None,
) -> None:
    """Prove the Workshop and remote heads are exactly ``expected_sha``.

    Every Workshop Git call re-asserts controller custody of the metadata
    itself; this helper only owns the head comparisons. ``actor`` names who
    ran between the checks; without it the message asks for fresh Approval.
    """
    actual_head = workspace.head_sha(path)
    if actual_head != expected_sha:
        raise ExecutePhaseError(
            f"{actor} created or changed a git commit; Git custody belongs to "
            "AgentMachinist"
            if actor
            else "provisioned Workshop HEAD no longer matches the approved SHA "
            f"({actual_head} != {expected_sha}); the remote Task branch moved after "
            "Approval. Run 'machinist inspect' and confirm the branch before "
            "approving a new head"
        )
    actual_remote = workspace.remote_sha(path, branch)
    if actual_remote != expected_sha:
        raise ExecutePhaseError(
            f"{actor} harness/gate changed the remote branch; refusing to continue "
            "after an uncontrolled push"
            if actor
            else "remote Task branch no longer matches the approved SHA "
            f"({actual_remote or 'missing'} != {expected_sha}); it moved after "
            "Approval. Run 'machinist inspect' and confirm the branch before "
            "approving a new head"
        )


def _raise_if_cancelled(cancel_check, stage: str) -> None:
    if cancel_check is not None and cancel_check():
        raise ExecutePhaseCancelled(f"execute cancelled {stage}")


def _capture_harness_report(claim, report: Any) -> str:
    text = "" if report is None else str(report)
    excerpt = text[-_MAX_HARNESS_REPORT_CHARS:]
    path = claim.log_path("harness-report.txt")
    try:
        write_text_file(path, text)
    except (OSError, RuntimePathError) as exc:
        raise ExecutePhaseError(
            f"could not persist harness report at {path}: {exc}"
        ) from exc
    claim.checkpoint(
        harness_report_path=str(path),
        harness_report_excerpt=excerpt,
    )
    return excerpt


def _enforce_change_limits(
    path: Path,
    *,
    workspace,
    config: MachinistConfig,
) -> dict[str, Any]:
    changed_files = tuple(sorted(set(workspace.changed_files(path))))
    if len(changed_files) > config.limits.max_changed_files:
        raise ExecutePhaseError(
            f"implementation changed {len(changed_files)} files; maximum is "
            f"{config.limits.max_changed_files}"
        )

    denied = [
        relative
        for relative in changed_files
        if (
            relative == ".machinist"
            or relative.startswith(".machinist/")
            or config.limits.path_is_denied(relative)
        )
    ]
    if denied:
        joined = ", ".join(denied[:10])
        raise ExecutePhaseError(
            f"implementation changed controller-owned or denied paths: {joined}"
        )

    changed_bytes = 0
    binary_files: list[str] = []
    deleted_files: list[str] = []
    for relative in changed_files:
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ExecutePhaseError(
                f"workspace returned unsafe changed path '{relative}'"
            )
        node = path.joinpath(*relative_path.parts)
        try:
            mode = node.lstat().st_mode
        except FileNotFoundError:
            # A deletion still counts toward the file limit; there is no new
            # content to charge against the byte or binary limit.
            deleted_files.append(relative)
            continue
        except OSError as exc:
            raise ExecutePhaseError(
                f"could not inspect changed path {relative}: {exc}"
            ) from exc

        if stat.S_ISLNK(mode):
            try:
                changed_bytes += len(os.fsencode(os.readlink(node)))
            except OSError as exc:
                raise ExecutePhaseError(
                    f"could not inspect changed symlink {relative}: {exc}"
                ) from exc
        elif stat.S_ISREG(mode):
            try:
                with node.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        changed_bytes += len(chunk)
                        if b"\0" in chunk and relative not in binary_files:
                            binary_files.append(relative)
                        if changed_bytes > config.limits.max_changed_bytes:
                            raise ExecutePhaseError(
                                "implementation changed content is too large "
                                f"({changed_bytes} bytes; maximum "
                                f"{config.limits.max_changed_bytes})"
                            )
            except OSError as exc:
                raise ExecutePhaseError(
                    f"could not read changed file {relative}: {exc}"
                ) from exc
        elif not stat.S_ISDIR(mode):
            raise ExecutePhaseError(
                f"implementation created unsupported file type at {relative}"
            )

        if changed_bytes > config.limits.max_changed_bytes:
            raise ExecutePhaseError(
                "implementation changed content is too large "
                f"({changed_bytes} bytes; maximum {config.limits.max_changed_bytes})"
            )

    if binary_files and not config.limits.allow_binary:
        joined = ", ".join(binary_files[:10])
        raise ExecutePhaseError(
            f"implementation changed binary file(s) while limits.allow_binary is false: {joined}"
        )

    if not config.limits.allow_test_deletions:
        deleted_tests = [
            relative for relative in deleted_files if _is_test_path(relative)
        ]
        if deleted_tests:
            joined = ", ".join(deleted_tests[:10])
            raise ExecutePhaseError(
                f"implementation deleted test file(s): {joined}; if the approved "
                "Spec requires this, set limits.allow_test_deletions true"
            )
    return {
        "files": list(changed_files),
        "file_count": len(changed_files),
        "bytes": changed_bytes,
        "binary_files": list(binary_files),
        "deleted_files": list(deleted_files),
    }


def _run_verification(
    path: Path,
    *,
    config: MachinistConfig,
    workspace,
    claim,
    test_runner,
    cancel_check,
) -> dict[str, Any]:
    gates = config.resolved_verification_gates()
    if not gates:
        report = VerificationReport(gates=(), duration_seconds=0.0).as_dict()
        claim.checkpoint(verification_report=report)
        return report

    return _invoke_verification_engine(
        path,
        gates,
        log_dir=claim.log_directory("verification-logs"),
        workspace=workspace,
        claim=claim,
        test_runner=test_runner,
        cancel_check=cancel_check,
    )


def _invoke_verification_engine(
    path: Path,
    gates,
    *,
    log_dir: Path,
    workspace,
    claim,
    test_runner,
    cancel_check,
) -> dict[str, Any]:
    try:
        report = run_verification_gates(
            path,
            gates,
            log_dir=log_dir,
            snapshotter=workspace.change_snapshot,
            runner=test_runner,
            cancel_check=cancel_check,
            on_progress=lambda index, total, name, status: report_progress(
                claim,
                f"verification {index}/{total}: {name}",
                status,
            ),
        )
    except VerificationFailed as exc:
        # The engine owns the report shape and the blocked message; Execute
        # only persists the Evidence and types the failure.
        claim.checkpoint(verification_report=exc.report.as_dict())
        if any(gate.status is GateStatus.CANCELLED for gate in exc.report.gates):
            raise ExecutePhaseCancelled(str(exc)) from exc
        raise ExecutePhaseError(str(exc)) from exc
    except VerificationError as exc:
        raise ExecutePhaseError(f"verification could not run safely: {exc}") from exc
    evidence = report.as_dict()
    claim.checkpoint(verification_report=evidence)
    return evidence


def _verification_resume_blocker(evidence: Mapping[str, Any]) -> str | None:
    """Explain why retained post-harness changes cannot be trusted on retry."""
    for gate in evidence.get("gates", []):
        if not isinstance(gate, Mapping):
            continue
        if gate.get("status") != "mutation_detected":
            continue
        name = gate.get("name")
        gate_name = name if isinstance(name, str) and name else "a verification gate"
        return f"mutation-forbidden gate {gate_name!r} changed it"
    return None


def _complete_delivery(
    issue_number: int,
    pr: PullRequest,
    *,
    github,
    claim,
    implementation_sha: str,
    change_summary: dict[str, Any] | None,
    verification_report: dict[str, Any] | None,
    comment_id: int | None,
    recovered: bool,
    harness_details: dict[str, Any] | None,
    harness_report_excerpt: str | None,
    attempt: int | None,
    duration_seconds: float,
    branch: str,
    expected_base: str,
    cancel_check,
    review_enabled: bool,
) -> None:
    delivery = (
        "deliver for independent review" if review_enabled else "deliver ready PR"
    )
    report_progress(claim, delivery, f"PR #{pr.number}")
    _raise_if_cancelled(cancel_check, "before completion delivery")
    body = _completion_comment(
        issue_number,
        implementation_sha,
        change_summary=change_summary,
        verification_report=verification_report,
        recovered=recovered,
        harness_details=harness_details,
        harness_report_excerpt=harness_report_excerpt,
        attempt=attempt,
        duration_seconds=duration_seconds,
        review_enabled=review_enabled,
    )
    current_pr = _delivery_pr_at_sha(
        github,
        original=pr,
        branch=branch,
        expected_base=expected_base,
        implementation_sha=implementation_sha,
    )
    _raise_if_cancelled(cancel_check, "before completion comment")
    saved_comment_id = github.upsert_pr_comment(
        current_pr.number,
        body,
        comment_id=comment_id,
    )
    claim.checkpoint(completion_comment_id=saved_comment_id)
    if review_enabled:
        if not current_pr.is_draft:
            raise ExecutePhaseError(
                f"GitHub PR #{current_pr.number} is already ready; independent Review "
                "requires the implementation to remain draft"
            )
        return
    current_pr = _delivery_pr_at_sha(
        github,
        original=current_pr,
        branch=branch,
        expected_base=expected_base,
        implementation_sha=implementation_sha,
    )
    _raise_if_cancelled(cancel_check, "before marking PR ready")
    if current_pr.is_draft:
        github.mark_ready(current_pr.number)
    report_progress(claim, "verify ready PR", f"PR #{current_pr.number}")
    observed_pr = _delivery_pr_at_sha(
        github,
        original=current_pr,
        branch=branch,
        expected_base=expected_base,
        implementation_sha=implementation_sha,
    )
    if observed_pr.is_draft:
        raise ExecutePhaseError(
            f"GitHub PR #{observed_pr.number} remained a draft after mark-ready; "
            "refusing to checkpoint completion"
        )


def _delivery_pr_at_sha(
    github,
    *,
    original: PullRequest,
    branch: str,
    expected_base: str,
    implementation_sha: str,
) -> PullRequest:
    """Re-read the exact PR so origin and GitHub cannot silently diverge."""
    current = github.pr_for_branch(branch)
    expected = PullRequestExpectation(
        number=original.number,
        branch=branch,
        base=expected_base,
        head_sha=implementation_sha,
        repository=github.repo,
    )
    try:
        return verify_pull_request(current, expected)
    except RepositoryCustodyError as exc:
        if "missing" in exc.reasons:
            raise ExecutePhaseError(
                f"PR #{original.number} for branch '{branch}' is no longer available"
            ) from exc
        if "head" in exc.reasons:
            observed = "missing" if current is None else current.head_sha or "missing"
            raise ExecutePhaseError(
                "GitHub PR head does not match the pushed implementation "
                f"({observed} != {implementation_sha}); refusing completion delivery"
            ) from exc
        raise ExecutePhaseError(
            "GitHub PR identity/state changed after implementation push; "
            "refusing completion delivery"
        ) from exc


def _bind_repository(config: MachinistConfig, github, workspace) -> str:
    try:
        return bind_repository(config, github, workspace).identity
    except RepositoryCustodyError as exc:
        raise ExecutePhaseError(str(exc)) from exc


def _require_pr_base(value: str, *, source: str) -> str:
    try:
        return validate_pr_base(value, source=source)
    except RepositoryCustodyError as exc:
        raise ExecutePhaseError(str(exc)) from exc


def _completion_comment(
    issue_number: int,
    implementation_sha: str,
    *,
    change_summary: dict[str, Any] | None,
    verification_report: dict[str, Any] | None,
    recovered: bool,
    harness_details: dict[str, Any] | None,
    harness_report_excerpt: str | None,
    attempt: int | None,
    duration_seconds: float,
    review_enabled: bool = False,
) -> str:
    qualifier = " (reconciled after interruption)" if recovered else ""
    lines = [
        f"<!-- agentmachinist:execute issue={issue_number} sha={implementation_sha} -->",
        f"## AgentMachinist Implementation complete{qualifier}",
        "",
        f"- Commit: `{implementation_sha}`",
    ]
    if attempt is not None:
        lines.append(f"- Task Run attempt: {attempt}")
    if review_enabled:
        lines.append(f"- Independent review pending: `machinist review {issue_number}`")
    lines.append(f"- Controller duration: {duration_seconds:.3f}s")
    if change_summary:
        lines.append(
            f"- Changes: {change_summary.get('file_count', 0)} files, "
            f"{change_summary.get('bytes', 0)} bytes"
        )
    lines.extend(["", "### Harness", ""])
    if harness_details:
        name = _markdown_cell(harness_details.get("name", "unknown"))
        model = _markdown_cell(harness_details.get("model") or "provider default")
        profile = _markdown_cell(harness_details.get("profile", "execute"))
        lines.append(f"- Effective harness: `{name}`")
        lines.append(f"- Model: `{model}`")
        lines.append(f"- Profile: `{profile}`")
    else:
        lines.append("Effective harness details were not checkpointed.")
    lines.extend(["", "#### Final report and deviations", ""])
    if harness_report_excerpt:
        safe_excerpt = (
            harness_report_excerpt[-_MAX_HARNESS_REPORT_CHARS:]
            .replace("\x00", "\\0")
            .replace("```", "` ` `")
        )
        lines.extend(["```text", safe_excerpt, "```"])
    else:
        lines.append("No final harness report text was returned.")

    lines.extend(["", "### Verification", ""])
    gates = verification_report.get("gates", []) if verification_report else []
    if not gates:
        lines.append("No verification gates were recorded.")
    else:
        lines.extend(["| Gate | Required | Result |", "| --- | --- | --- |"])
        for gate in gates:
            if not isinstance(gate, dict):
                continue
            name = _markdown_cell(gate.get("name", "gate"))
            required = "yes" if gate.get("required") else "no"
            status = _markdown_cell(gate.get("status", "unknown"))
            lines.append(f"| {name} | {required} | {status} |")
    lines.extend(
        ["", "Full harness and verification reports remain in local Task Run logs."]
    )
    return "\n".join(lines)


def _reconciled_push(
    previous: TaskEvidence,
    approval_sha: str | None,
    pr: PullRequest,
    *,
    read_remote_sha: Callable[[], str | None],
    force: bool,
) -> str | None:
    """Return the intended implementation SHA when a prior push already landed.

    GitHub's PR listing can lag a push the controller made just before it
    crashed, so the remote branch is consulted too; it is read only when a
    prior push intent exists, so first attempts pay no extra round trip.
    """
    if force or approval_sha is None or previous.approved_sha != approval_sha:
        return None
    intended = previous.intended_push_sha or previous.implementation_sha
    if intended is None:
        return None
    if intended == pr.head_sha:
        return intended
    remote = read_remote_sha()
    if intended == remote:
        return intended
    observed = previous.pushed_sha
    if observed is not None and observed not in (pr.head_sha, remote):
        raise ExecutePhaseError(
            "the previously observed implementation push no longer matches the PR "
            "head or the remote branch; inspect the remote branch before retrying"
        )
    return None


def _harness_details(harness) -> dict[str, Any]:
    return harness_evidence(harness, profile="execute")


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _duration(started_at: float) -> float:
    return round(max(0.0, time.monotonic() - started_at), 3)
