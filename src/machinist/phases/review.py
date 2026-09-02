"""Independent read-only Review Phase for an implemented draft pull request."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from machinist.config import MachinistConfig
from machinist.evidence import EvidenceError, TaskEvidence
from machinist.github import PullRequest
from machinist.harness import harness_evidence
from machinist.managed_paths import ManagedPathError, read_managed_text
from machinist.phases.progress import bind_harness_progress, report_progress

_MAX_REPORT_CHARS = 100_000
_MAX_DIFF_BYTES = 2 * 1024 * 1024
_LEVELS = frozenset({"low", "medium", "high"})


class ReviewPhaseError(Exception):
    """Review refused to certify the exact implemented draft."""


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    confidence: str
    file: str
    line: int
    requirement: str
    message: str
    remediation: str


@dataclass(frozen=True)
class ReviewReport:
    version: int
    summary: str
    findings: tuple[ReviewFinding, ...]


def parse_review_report(payload: str) -> ReviewReport:
    """Parse bounded version-1 JSON and reject ambiguous output."""
    if len(payload) > _MAX_REPORT_CHARS:
        raise ReviewPhaseError("review report exceeds 100000 characters")
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReviewPhaseError("review report must be valid JSON") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ReviewPhaseError("review report version must be 1")
    summary = _text(raw.get("summary"), "summary")
    raw_findings = raw.get("findings")
    if not isinstance(raw_findings, list):
        raise ReviewPhaseError("review report findings must be a list")
    findings = tuple(_parse_finding(item) for item in raw_findings)
    return ReviewReport(version=1, summary=summary, findings=findings)


def run_review_phase(
    issue_number: int,
    config: MachinistConfig,
    *,
    github,
    harness,
    workspace,
    execute_evidence: dict[str, Any],
    claim=None,
    cancel_check=None,
) -> PullRequest:
    """Review the exact Execute head and transition its PR to ready."""
    if not config.review.enabled:
        raise ReviewPhaseError("independent Review is disabled in machinist.yaml")
    branch = f"{config.workspace.branch_prefix}issue-{issue_number}"
    pr = _find_pr(github, config, branch)
    evidence = TaskEvidence.load(execute_evidence)
    expected_sha, base = _review_identity(pr, evidence, github)
    report_progress(claim, "provision Review preview", branch)
    path = workspace.provision_preview(
        f"review-issue-{issue_number}", branch, f"origin/{base}"
    )
    try:
        return _run_in_preview(
            issue_number,
            config,
            github=github,
            harness=harness,
            workspace=workspace,
            path=path,
            pr=pr,
            branch=branch,
            base=base,
            expected_sha=expected_sha,
            execute_evidence=evidence,
            claim=claim,
            cancel_check=cancel_check,
        )
    finally:
        workspace.cleanup_preview(path)


def _run_in_preview(
    issue_number: int,
    config: MachinistConfig,
    *,
    github,
    harness,
    workspace,
    path: Path,
    pr: PullRequest,
    branch: str,
    base: str,
    expected_sha: str,
    execute_evidence: TaskEvidence,
    claim,
    cancel_check,
) -> PullRequest:
    workspace.assert_head(path, expected_sha)
    prompt = _review_prompt(
        issue_number,
        config,
        github,
        workspace,
        path,
        base=base,
        execute_evidence=execute_evidence,
    )
    _cancel(cancel_check, "before independent review")
    report_progress(claim, "independent review", harness.name)
    bind_harness_progress(harness, claim, stage="review")
    output = harness.review(prompt, path)
    if workspace.has_changes(path):
        raise ReviewPhaseError(
            f"{harness.name} modified the read-only Review workspace"
        )
    report = parse_review_report(output)
    _checkpoint_review(claim, expected_sha, harness, report)
    _deliver_review(
        issue_number,
        pr,
        report,
        github=github,
        branch=branch,
        expected_sha=expected_sha,
        claim=claim,
        cancel_check=cancel_check,
    )
    return pr


def _review_identity(
    pr: PullRequest, execute_evidence: TaskEvidence, github
) -> tuple[str, str]:
    expected_sha = execute_evidence.pushed_sha
    if expected_sha is None:
        raise ReviewPhaseError("successful Execute evidence has no delivered head SHA")
    if pr.head_sha != expected_sha:
        raise ReviewPhaseError(
            f"PR #{pr.number} changed after Execute; run and approve Execute again"
        )
    if not pr.is_draft:
        raise ReviewPhaseError(f"PR #{pr.number} is already ready for human review")
    try:
        base = execute_evidence.pr_base() or github.default_branch()
    except EvidenceError as exc:
        raise ReviewPhaseError("Execute evidence has an invalid PR base") from exc
    if not base.strip():
        raise ReviewPhaseError("Execute evidence has an invalid PR base")
    return expected_sha, base


def _review_prompt(
    issue_number: int,
    config: MachinistConfig,
    github,
    workspace,
    path: Path,
    *,
    base: str,
    execute_evidence: TaskEvidence,
) -> str:
    spec = _read_spec(path, issue_number, config)
    diff = workspace.diff_against(path, f"origin/{base}", max_bytes=_MAX_DIFF_BYTES)
    issue = github.get_issue(issue_number)
    instructions = config.resolve_instructions("review", path)
    evidence = json.dumps(
        {
            "change_summary": execute_evidence.change_summary,
            "verification_report": execute_evidence.verification_report,
        },
        sort_keys=True,
    )
    prompt = _prompt_sections(issue_number, issue, spec, evidence, diff)
    if instructions:
        prompt += f"\n\n## Repository review instructions\n\n{instructions}"
    return prompt


def _read_spec(path: Path, issue: int, config: MachinistConfig) -> str:
    relative = Path(".machinist/specs") / f"issue-{issue}-spec.md"
    try:
        spec = read_managed_text(
            path,
            relative,
            max_bytes=config.limits.max_spec_chars * 4,
        )
    except ManagedPathError as exc:
        raise ReviewPhaseError(f"cannot safely read approved Spec: {exc}") from exc
    if spec is None:
        raise ReviewPhaseError(f"approved Spec {relative} is missing")
    return spec


def _prompt_sections(issue: int, task, spec: str, evidence: str, diff: str) -> str:
    return (
        "Review this implementation independently and read-only. Return only "
        "version-1 JSON with summary and findings. Findings require severity, "
        "confidence, repository-relative file, positive line, requirement, "
        "message, and remediation. Do not edit files.\n\n"
        f"## Task #{issue}: {task.title}\n\n{task.body}\n\n"
        f"## Approved Spec\n\n{spec}\n\n"
        f"## Execute evidence\n\n```json\n{evidence}\n```\n\n"
        f"## Diff\n\n```diff\n{diff}\n```"
    )


def _deliver_review(
    issue_number: int,
    pr: PullRequest,
    report: ReviewReport,
    *,
    github,
    branch: str,
    expected_sha: str,
    claim,
    cancel_check,
) -> None:
    current = _exact_pr(github, pr, branch, expected_sha)
    _cancel(cancel_check, "before Review comment")
    previous = TaskEvidence.load(
        getattr(claim, "previous_evidence", {}) if claim is not None else {}
    )
    comment_id = github.upsert_pr_comment(
        current.number,
        _review_comment(issue_number, expected_sha, report),
        comment_id=previous.review_comment_id,
    )
    _checkpoint(claim, review_comment_id=comment_id)
    current = _exact_pr(github, pr, branch, expected_sha)
    _cancel(cancel_check, "before marking PR ready")
    github.mark_ready(current.number)
    observed = _exact_pr(github, pr, branch, expected_sha)
    if observed.is_draft:
        raise ReviewPhaseError(f"GitHub PR #{pr.number} remained draft after Review")
    _checkpoint(claim, ready_observed_sha=expected_sha)


def _find_pr(github, config: MachinistConfig, branch: str) -> PullRequest:
    pr = next(
        (
            item
            for item in github.open_machinist_prs(config.workspace.branch_prefix)
            if item.branch == branch
        ),
        None,
    )
    if pr is None:
        raise ReviewPhaseError(f"open draft PR for branch '{branch}' was not found")
    return pr


def _exact_pr(github, original: PullRequest, branch: str, sha: str) -> PullRequest:
    current = github.pr_for_branch(branch)
    if (
        current is None
        or current.number != original.number
        or current.branch != branch
        or current.state != "OPEN"
        or current.head_sha != sha
    ):
        raise ReviewPhaseError("PR identity or head changed during independent Review")
    return current


def _parse_finding(raw: object) -> ReviewFinding:
    if not isinstance(raw, dict):
        raise ReviewPhaseError("each review finding must be an object")
    line = raw.get("line")
    if type(line) is not int or line < 1:
        raise ReviewPhaseError("review finding line must be a positive integer")
    return ReviewFinding(
        severity=_level(raw.get("severity"), "severity"),
        confidence=_level(raw.get("confidence"), "confidence"),
        file=_repository_path(raw.get("file")),
        line=line,
        requirement=_text(raw.get("requirement"), "requirement"),
        message=_text(raw.get("message"), "message"),
        remediation=_text(raw.get("remediation"), "remediation"),
    )


def _review_comment(issue: int, sha: str, report: ReviewReport) -> str:
    lines = [
        f"<!-- agentmachinist:review issue={issue} sha={sha} -->",
        "## AgentMachinist Independent review",
        "",
        report.summary,
        "",
        f"Reviewed commit: `{sha}`",
    ]
    if not report.findings:
        lines.extend(["", "No findings."])
    for finding in report.findings:
        lines.extend(_finding_lines(finding))
    lines.extend(["", "Findings are advisory; human review and merge remain required."])
    return "\n".join(lines)


def _finding_lines(finding: ReviewFinding) -> list[str]:
    return [
        "",
        f"### {finding.severity.title()}: `{finding.file}:{finding.line}`",
        "",
        f"- Confidence: {finding.confidence}",
        f"- Requirement: {finding.requirement}",
        f"- Finding: {finding.message}",
        f"- Remediation: {finding.remediation}",
    ]


def _repository_path(value: object) -> str:
    text = _text(value, "file")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise ReviewPhaseError("review finding file must be repository-relative")
    return text


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ReviewPhaseError(f"review report {field} must be non-empty text")
    return value.strip()


def _level(value: object, field: str) -> str:
    text = _text(value, field).casefold()
    if text not in _LEVELS:
        raise ReviewPhaseError(f"review finding {field} must be low, medium, or high")
    return text


def _checkpoint_review(claim, expected_sha: str, harness, report: ReviewReport) -> None:
    counts = {
        level: sum(item.severity == level for item in report.findings)
        for level in sorted(_LEVELS)
    }
    _checkpoint(
        claim,
        reviewed_sha=expected_sha,
        harness=harness_evidence(harness, profile="review"),
        finding_counts=counts,
    )


def _checkpoint(claim, **evidence: Any) -> None:
    if claim is not None:
        claim.checkpoint(**evidence)


def _cancel(cancel_check, stage: str) -> None:
    if cancel_check is not None and cancel_check():
        error = ReviewPhaseError(f"Review cancelled {stage}")
        error.cancelled = True
        raise error
