"""Phase 3: approved spec -> implementation -> PR ready for review.

The controller owns every Git and GitHub transition. Harness and verification
processes may edit the Workshop, but Execute asserts custody before publishing,
records intent before external side effects, and can reconcile a crash after a
successful push without rerunning the harness.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from string import Template
from typing import Any

from machinist.config import GateMutationPolicy, MachinistConfig
from machinist.github import PullRequest

_IMPLEMENT_PROMPT = files("machinist") / "templates" / "implement-prompt.md"
_RECOVERY_MODES = frozenset({"fresh", "resume"})
_FULL_SHA_LENGTH = 40
_MAX_FEEDBACK_CHARS = 50_000
_MAX_HARNESS_REPORT_CHARS = 2_000


class ExecutePhaseError(Exception):
    """Phase 3 refused to run or failed to produce a shippable change."""


class ExecutePhaseCancelled(ExecutePhaseError):
    """Execute stopped cooperatively during verification."""

    cancelled = True


@dataclass(frozen=True)
class _ChangeSummary:
    files: tuple[str, ...]
    changed_bytes: int
    binary_files: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "file_count": len(self.files),
            "bytes": self.changed_bytes,
            "binary_files": list(self.binary_files),
        }


def render_implement_prompt(
    issue_number: int,
    spec_text: str,
    feedback: str | None = None,
    instructions: str = "",
) -> str:
    if feedback is not None and len(feedback) > _MAX_FEEDBACK_CHARS:
        raise ExecutePhaseError(
            f"operator feedback is too large ({len(feedback)} characters; "
            f"maximum {_MAX_FEEDBACK_CHARS})"
        )
    template = Template(_IMPLEMENT_PROMPT.read_text())
    prompt = template.safe_substitute(number=issue_number, spec=spec_text)
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


def run_execute_phase(
    issue_number: int,
    config: MachinistConfig,
    *,
    github,
    harness,
    workspace,
    test_runner=subprocess.run,
    force: bool = False,
    claim=None,
    recovery: str = "fresh",
    feedback: str | None = None,
    cancel_check=None,
) -> PullRequest:
    """Implement an approved Task with durable retry and reconciliation state."""
    started_at = time.monotonic()
    # Validate before performing any GitHub or filesystem operation.
    render_implement_prompt(issue_number, "", feedback)
    repo_root = getattr(workspace, "repo_root", Path.cwd())
    instructions = config.resolve_instructions("execute", repo_root)
    if recovery not in _RECOVERY_MODES:
        raise ExecutePhaseError(
            f"unknown recovery mode '{recovery}'; expected 'fresh' or 'resume'"
        )
    if feedback and recovery == "resume":
        raise ExecutePhaseError(
            "operator feedback requires a fresh implementation attempt; "
            "it cannot be applied while resuming completed harness output"
        )

    branch = f"{config.workspace.branch_prefix}issue-{issue_number}"
    pr = next(
        (
            candidate
            for candidate in github.open_machinist_prs(config.workspace.branch_prefix)
            if candidate.branch == branch
        ),
        None,
    )
    if pr is None:
        raise ExecutePhaseError(
            f"no open PR for branch '{branch}'; run 'machinist spec {issue_number}' first"
        )
    approved_label = config.github.labels.approved
    if approved_label not in pr.labels:
        raise ExecutePhaseError(
            f"PR #{pr.number} is not approved; apply the '{approved_label}' label "
            "(or comment /machinist-execute on it) first"
        )

    approval_sha = github.approval_sha(pr.number)
    previous = dict(getattr(claim, "previous_evidence", {}) or {})
    reconciled_sha = _reconciled_push(
        previous,
        approval_sha,
        pr,
        force=force or bool(feedback),
    )
    if reconciled_sha is not None:
        _checkpoint(claim, push_observed_sha=reconciled_sha)
        _complete_delivery(
            issue_number,
            pr,
            github=github,
            claim=claim,
            implementation_sha=reconciled_sha,
            change_summary=_mapping(previous.get("change_summary")),
            verification_report=_mapping(previous.get("verification_report")),
            comment_id=_positive_int(previous.get("completion_comment_id")),
            recovered=True,
            harness_details=_mapping(previous.get("harness")),
            harness_report_excerpt=_string(previous.get("harness_report_excerpt")),
            attempt=getattr(claim, "attempt", None),
            duration_seconds=_duration(started_at),
        )
        return pr

    if approval_sha is None:
        raise ExecutePhaseError(
            f"PR #{pr.number} has the approval label but no SHA-bound approval evidence; "
            "approve the current spec again"
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

    if recovery == "resume":
        _checkpoint(
            claim,
            feedback_supplied=previous.get("feedback_supplied") is True,
            feedback_characters=(
                previous.get("feedback_characters")
                if isinstance(previous.get("feedback_characters"), int)
                else 0
            ),
        )
    else:
        _checkpoint(
            claim,
            feedback_supplied=bool(feedback),
            feedback_characters=len(feedback or ""),
        )

    base = github.default_branch()
    if recovery == "resume":
        path, resume_stage = _resume_workspace(
            workspace,
            claim,
            previous,
            branch=branch,
            approval_sha=approval_sha,
        )
    else:
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

    comment_id = _positive_int(previous.get("completion_comment_id"))
    try:
        if resume_stage == "push":
            implementation_sha = str(previous["push_intended_sha"])
            _retry_intended_push(
                workspace,
                path,
                branch=branch,
                approval_sha=approval_sha,
                implementation_sha=implementation_sha,
                claim=claim,
            )
            _complete_delivery(
                issue_number,
                pr,
                github=github,
                claim=claim,
                implementation_sha=implementation_sha,
                change_summary=_mapping(previous.get("change_summary")),
                verification_report=_mapping(previous.get("verification_report")),
                comment_id=comment_id,
                recovered=True,
                harness_details=_mapping(previous.get("harness")),
                harness_report_excerpt=_string(previous.get("harness_report_excerpt")),
                attempt=getattr(claim, "attempt", None),
                duration_seconds=_duration(started_at),
            )
        else:
            _assert_approved_head(
                workspace,
                path,
                branch=branch,
                approved_sha=approval_sha,
            )
            if recovery == "fresh":
                _reset_fresh_execution_evidence(claim)
            spec_file = path / ".machinist" / "specs" / f"issue-{issue_number}-spec.md"
            if not spec_file.exists():
                raise ExecutePhaseError(
                    f"spec file .machinist/specs/{spec_file.name} not found on "
                    f"branch '{branch}'"
                )

            if resume_stage != "post_harness":
                harness_details = _harness_details(harness)
                _checkpoint(
                    claim,
                    harness=harness_details,
                )
                harness_report = harness.implement(
                    render_implement_prompt(
                        issue_number,
                        spec_file.read_text(),
                        feedback,
                        instructions,
                    ),
                    cwd=path,
                )
                harness_report_excerpt = _capture_harness_report(
                    claim,
                    harness_report,
                )
                _checkpoint(
                    claim,
                    instructions_supplied=bool(instructions),
                    instruction_characters=len(instructions),
                )
                _assert_git_custody(
                    workspace,
                    path,
                    branch=branch,
                    expected_sha=approval_sha,
                    actor=harness.name,
                )
            else:
                harness_details = _mapping(previous.get("harness")) or _harness_details(
                    harness
                )
                harness_report_excerpt = _string(previous.get("harness_report_excerpt"))

            if not workspace.has_changes(path):
                raise ExecutePhaseError(
                    f"{harness.name} made no changes for issue #{issue_number}"
                )

            change_summary = _enforce_change_limits(
                path,
                workspace=workspace,
                config=config,
            )
            _checkpoint(
                claim,
                harness_completed=True,
                workspace_head=approval_sha,
                change_summary=change_summary.as_dict(),
            )

            try:
                verification_report = _run_verification(
                    path,
                    config=config,
                    workspace=workspace,
                    claim=claim,
                    test_runner=test_runner,
                    cancel_check=cancel_check,
                )
            except ExecutePhaseError:
                _assert_git_custody(
                    workspace,
                    path,
                    branch=branch,
                    expected_sha=approval_sha,
                    actor="verification gate",
                )
                raise

            _assert_git_custody(
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
            _checkpoint(claim, change_summary=change_summary.as_dict())

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
            _checkpoint(
                claim,
                approved_sha=approval_sha,
                implementation_sha=implementation_sha,
                push_intended_sha=implementation_sha,
                push_observed_sha=None,
                workspace_head=implementation_sha,
            )
            workspace.push(path, branch, expected_sha=approval_sha)
            observed_sha = workspace.remote_sha(path, branch)
            if observed_sha != implementation_sha:
                raise ExecutePhaseError(
                    "push returned without publishing the intended implementation "
                    f"({observed_sha or 'missing'} != {implementation_sha})"
                )
            _checkpoint(claim, push_observed_sha=implementation_sha)
            _complete_delivery(
                issue_number,
                pr,
                github=github,
                claim=claim,
                implementation_sha=implementation_sha,
                change_summary=change_summary.as_dict(),
                verification_report=verification_report,
                comment_id=comment_id,
                recovered=False,
                harness_details=harness_details,
                harness_report_excerpt=harness_report_excerpt,
                attempt=getattr(claim, "attempt", None),
                duration_seconds=_duration(started_at),
            )
    except Exception:
        workspace.cleanup(path, success=False)
        raise

    workspace.cleanup(path, success=True)
    return pr


def _provision_fresh_workspace(
    workspace,
    claim,
    previous: dict[str, Any],
    *,
    issue_number: int,
    branch: str,
    base_ref: str,
    approval_sha: str,
) -> Path:
    claim_attempt = getattr(claim, "attempt", None)
    attempt = (
        claim_attempt if isinstance(claim_attempt, int) and claim_attempt > 1 else None
    )
    if attempt is None:
        path = workspace.provision(f"issue-{issue_number}", branch, base_ref)
    else:
        path = workspace.provision(
            f"issue-{issue_number}",
            branch,
            base_ref,
            attempt=attempt,
        )

    raw_prior_paths = previous.get("prior_workspace_paths")
    prior_paths = (
        [value for value in raw_prior_paths if isinstance(value, str)]
        if isinstance(raw_prior_paths, list)
        else []
    )
    previous_path = previous.get("workspace_path")
    if (
        isinstance(previous_path, str)
        and previous_path != str(path)
        and previous_path not in prior_paths
    ):
        prior_paths.append(previous_path)
    _checkpoint(
        claim,
        approved_sha=approval_sha,
        recovery_mode="fresh",
        workspace_path=str(path),
        workspace_head=workspace.head_sha(path),
        prior_workspace_paths=prior_paths,
    )
    return path


def _reset_fresh_execution_evidence(claim) -> None:
    """Clear stale stage evidence only after the approved head is revalidated."""
    _checkpoint(
        claim,
        harness_completed=False,
        harness=None,
        harness_report_path=None,
        harness_report_excerpt=None,
        verification_report=None,
        change_summary=None,
        implementation_sha=None,
        push_intended_sha=None,
        push_observed_sha=None,
        completion_comment_intended_sha=None,
        completion_comment_observed_sha=None,
        ready_intended_sha=None,
        ready_observed_sha=None,
    )


def _resume_workspace(
    workspace,
    claim,
    previous: dict[str, Any],
    *,
    branch: str,
    approval_sha: str,
) -> tuple[Path, str]:
    if claim is None:
        raise ExecutePhaseError("--resume requires a claimed Task Run")
    if previous.get("approved_sha") != approval_sha:
        raise ExecutePhaseError(
            "retained workspace was created for a different approved SHA; "
            "start a fresh retry"
        )
    raw_path = previous.get("workspace_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ExecutePhaseError(
            "previous Task Run has no retained workspace checkpoint to resume"
        )

    intended_sha = previous.get("push_intended_sha")
    implementation_sha = previous.get("implementation_sha")
    if _is_full_sha(intended_sha) and intended_sha == implementation_sha:
        expected_sha = str(intended_sha)
        stage = "push"
    elif previous.get("harness_completed") is True:
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
        )
    except Exception as exc:
        raise ExecutePhaseError(
            f"retained workspace {raw_path} cannot be resumed safely: {exc}"
        ) from exc
    _checkpoint(
        claim,
        recovery_mode="resume",
        workspace_path=str(path),
        workspace_head=expected_sha,
    )
    return path, stage


def _retry_intended_push(
    workspace,
    path: Path,
    *,
    branch: str,
    approval_sha: str,
    implementation_sha: str,
    claim,
) -> None:
    remote_sha = workspace.remote_sha(path, branch)
    if remote_sha == implementation_sha:
        _checkpoint(claim, push_observed_sha=implementation_sha)
        return
    if remote_sha != approval_sha:
        raise ExecutePhaseError(
            "cannot resume the intended push because the remote branch moved "
            f"to {remote_sha or 'missing'}"
        )
    _checkpoint(
        claim,
        approved_sha=approval_sha,
        implementation_sha=implementation_sha,
        push_intended_sha=implementation_sha,
        push_observed_sha=None,
    )
    workspace.push(path, branch, expected_sha=approval_sha)
    observed_sha = workspace.remote_sha(path, branch)
    if observed_sha != implementation_sha:
        raise ExecutePhaseError(
            "push returned without publishing the checkpointed implementation "
            f"({observed_sha or 'missing'} != {implementation_sha})"
        )
    _checkpoint(claim, push_observed_sha=implementation_sha)


def _assert_approved_head(
    workspace,
    path: Path,
    *,
    branch: str,
    approved_sha: str,
) -> None:
    actual_sha = workspace.head_sha(path)
    if actual_sha != approved_sha:
        raise ExecutePhaseError(
            "provisioned Workshop HEAD no longer matches the approved SHA "
            f"({actual_sha} != {approved_sha}); approve the current branch head again"
        )
    remote_sha = workspace.remote_sha(path, branch)
    if remote_sha != approved_sha:
        raise ExecutePhaseError(
            "remote Task branch no longer matches the approved SHA "
            f"({remote_sha or 'missing'} != {approved_sha}); approve the current head again"
        )


def _assert_git_custody(
    workspace,
    path: Path,
    *,
    branch: str,
    expected_sha: str,
    actor: str,
) -> None:
    actual_head = workspace.head_sha(path)
    if actual_head != expected_sha:
        raise ExecutePhaseError(
            f"{actor} created or changed a git commit; Git custody belongs to "
            "AgentMachinist"
        )
    actual_remote = workspace.remote_sha(path, branch)
    if actual_remote != expected_sha:
        raise ExecutePhaseError(
            f"{actor} harness/gate changed the remote branch; refusing to continue "
            "after an uncontrolled push"
        )


def _capture_harness_report(claim, report: Any) -> str:
    text = "" if report is None else str(report)
    excerpt = text[-_MAX_HARNESS_REPORT_CHARS:]
    if claim is None or not hasattr(claim, "log_path"):
        return excerpt
    path = claim.log_path("harness-report.txt")
    try:
        path.write_text(text)
    except OSError as exc:
        raise ExecutePhaseError(
            f"could not persist harness report at {path}: {exc}"
        ) from exc
    _checkpoint(
        claim,
        harness_report_path=str(path),
        harness_report_excerpt=excerpt,
    )
    return excerpt


def _enforce_change_limits(
    path: Path,
    *,
    workspace,
    config: MachinistConfig,
) -> _ChangeSummary:
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
    return _ChangeSummary(changed_files, changed_bytes, tuple(binary_files))


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
        report = {
            "success": True,
            "duration_seconds": 0.0,
            "required_failures": [],
            "advisory_failures": [],
            "gates": [],
        }
        _checkpoint(claim, verification_report=report)
        return report

    if claim is not None and hasattr(claim, "log_path"):
        log_dir = claim.log_path("verification-logs")
        return _invoke_verification_engine(
            path,
            gates,
            log_dir=log_dir,
            workspace=workspace,
            claim=claim,
            test_runner=test_runner,
            cancel_check=cancel_check,
        )

    with tempfile.TemporaryDirectory(prefix="machinist-verification-") as directory:
        return _invoke_verification_engine(
            path,
            gates,
            log_dir=Path(directory),
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
        from machinist.verification import (
            VerificationError,
            VerificationFailed,
            run_verification_gates,
        )
    except ModuleNotFoundError as exc:
        if exc.name != "machinist.verification":
            raise ExecutePhaseError(
                f"verification engine dependency is unavailable: {exc}"
            ) from exc
        return _fallback_verification(
            path,
            gates,
            log_dir=log_dir,
            workspace=workspace,
            claim=claim,
            test_runner=test_runner,
            cancel_check=cancel_check,
        )
    except ImportError as exc:
        raise ExecutePhaseError(
            f"verification engine API is unavailable or incompatible: {exc}"
        ) from exc

    _checkpoint(claim, verification_log_dir=str(log_dir))
    try:
        report = run_verification_gates(
            path,
            gates,
            log_dir=log_dir,
            snapshotter=workspace.change_snapshot,
            runner=test_runner,
            cancel_check=cancel_check,
        )
    except VerificationFailed as exc:
        evidence = exc.report.as_dict()
        _checkpoint(claim, verification_report=evidence)
        message = _verification_failure_message(evidence)
        if any(
            isinstance(gate, dict) and gate.get("status") == "cancelled"
            for gate in evidence.get("gates", [])
        ):
            raise ExecutePhaseCancelled(message) from exc
        raise ExecutePhaseError(message) from exc
    except VerificationError as exc:
        raise ExecutePhaseError(f"verification could not run safely: {exc}") from exc
    evidence = report.as_dict()
    _checkpoint(claim, verification_report=evidence)
    return evidence


def _fallback_verification(
    path: Path,
    gates,
    *,
    log_dir: Path,
    workspace,
    claim,
    test_runner,
    cancel_check,
) -> dict[str, Any]:
    """Compatibility fallback used only while the engine module is unavailable."""
    log_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, gate in enumerate(gates, start=1):
        before = (
            workspace.change_snapshot(path)
            if gate.mutation_policy is GateMutationPolicy.FORBID
            else None
        )
        if cancel_check is not None and cancel_check():
            completed = None
            status = "cancelled"
            stdout = ""
            stderr = "cancelled before verification gate dispatch"
        else:
            try:
                completed = test_runner(
                    gate.command,
                    shell=True,
                    cwd=path,
                    capture_output=True,
                    text=True,
                    timeout=gate.timeout_minutes * 60,
                )
                status = "passed" if completed.returncode == 0 else "failed"
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
            except subprocess.TimeoutExpired as exc:
                completed = None
                status = "timed_out"
                stdout = str(exc.output or "")
                stderr = str(exc.stderr or "")
        after = (
            workspace.change_snapshot(path)
            if gate.mutation_policy is GateMutationPolicy.FORBID
            else None
        )
        if before != after:
            status = "mutation_detected"
        stem = f"{index:02d}-{gate.name.replace(' ', '-')}"
        stdout_log = log_dir / f"{stem}.stdout.log"
        stderr_log = log_dir / f"{stem}.stderr.log"
        stdout_log.write_text(stdout)
        stderr_log.write_text(stderr)
        passed = status == "passed"
        results.append(
            {
                "name": gate.name,
                "command": gate.command,
                "required": gate.required,
                "mutation_policy": gate.mutation_policy.value,
                "status": status,
                "passed": passed,
                "blocking": status == "cancelled" or (gate.required and not passed),
                "returncode": None if completed is None else completed.returncode,
                "stdout_excerpt": stdout[-4000:],
                "stderr_excerpt": stderr[-4000:],
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
                "snapshot_before": before,
                "snapshot_after": after,
            }
        )
    failures = [result["name"] for result in results if result["blocking"]]
    evidence = {
        "success": not failures,
        "duration_seconds": 0.0,
        "required_failures": failures,
        "advisory_failures": [
            result["name"]
            for result in results
            if not result["required"] and not result["passed"]
        ],
        "gates": results,
    }
    _checkpoint(claim, verification_report=evidence)
    if failures:
        message = _verification_failure_message(evidence)
        if any(result["status"] == "cancelled" for result in results):
            raise ExecutePhaseCancelled(message)
        raise ExecutePhaseError(message)
    return evidence


def _verification_failure_message(report: dict[str, Any]) -> str:
    details: list[str] = []
    for gate in report.get("gates", []):
        if not isinstance(gate, dict) or not gate.get("blocking"):
            continue
        output = (
            gate.get("stderr_excerpt")
            or gate.get("stdout_excerpt")
            or gate.get("error")
        )
        suffix = f": {str(output).strip()[-2000:]}" if output else ""
        details.append(
            f"{gate.get('name', 'gate')} ({gate.get('status', 'failed')}){suffix}"
        )
    return "required verification gates failed: " + "; ".join(details)


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
) -> None:
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
    )
    _checkpoint(
        claim,
        completion_comment_intended_sha=implementation_sha,
        completion_duration_seconds=duration_seconds,
    )
    saved_comment_id = github.upsert_pr_comment(
        pr.number,
        body,
        comment_id=comment_id,
    )
    _checkpoint(
        claim,
        completion_comment_id=saved_comment_id,
        completion_comment_observed_sha=implementation_sha,
    )
    _checkpoint(claim, ready_intended_sha=implementation_sha)
    if pr.is_draft:
        github.mark_ready(pr.number)
    _checkpoint(claim, ready_observed_sha=implementation_sha)


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
    previous: dict[str, Any],
    approval_sha: str | None,
    pr: PullRequest,
    *,
    force: bool,
) -> str | None:
    if force or approval_sha is None or previous.get("approved_sha") != approval_sha:
        return None
    intended = previous.get("push_intended_sha") or previous.get("implementation_sha")
    if _is_full_sha(intended) and intended == pr.head_sha:
        return str(intended)
    observed = previous.get("push_observed_sha")
    if _is_full_sha(observed) and observed != pr.head_sha:
        raise ExecutePhaseError(
            "the previously observed implementation push no longer matches the PR head; "
            "inspect the remote branch before retrying"
        )
    return None


def _checkpoint(claim, **evidence: Any) -> None:
    if claim is not None:
        claim.checkpoint(**evidence)


def _mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _harness_details(harness) -> dict[str, Any]:
    harness_config = getattr(harness, "config", None)
    model = getattr(harness_config, "model", None)
    if hasattr(model, "value"):
        model = model.value
    return {
        "name": str(getattr(harness, "name", "unknown")),
        "model": str(model) if model is not None else None,
        "profile": "execute",
    }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _is_full_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _FULL_SHA_LENGTH
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _duration(started_at: float) -> float:
    return round(max(0.0, time.monotonic() - started_at), 3)
