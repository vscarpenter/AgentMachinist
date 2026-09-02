"""Canonical Phase and Task transition decisions.

Remote GitHub facts and durable local Task Run facts enter here as primitives.
Callers receive one state, ordering priority, optional dispatch Phase, and exact
operator action without interpreting status strings themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from machinist.evidence import TaskEvidence
from machinist.lifecycle import Phase, RunRecord, RunStatus


class PipelineState(StrEnum):
    AWAITING_SPEC = "awaiting spec"
    AWAITING_APPROVAL = "awaiting approval"
    APPROVAL_PENDING = "approval pending"
    APPROVAL_STALE = "approval stale"
    APPROVED = "approved"
    AWAITING_REVIEW = "awaiting review"
    IN_REVIEW = "in review"
    SPEC_RUNNING = "spec running"
    SPEC_INTERRUPTED = "spec interrupted"
    SPEC_FAILED = "spec failed"
    SPEC_CANCELLED = "spec cancelled"
    SPEC_ABANDONED = "spec abandoned"
    SPEC_CLOSED = "spec closed"
    EXECUTE_RUNNING = "execute running"
    EXECUTE_INTERRUPTED = "execute interrupted"
    EXECUTE_FAILED = "execute failed"
    EXECUTE_CANCELLED = "execute cancelled"
    EXECUTE_ABANDONED = "execute abandoned"
    REVIEW_RUNNING = "review running"
    REVIEW_INTERRUPTED = "review interrupted"
    REVIEW_FAILED = "review failed"
    REVIEW_CANCELLED = "review cancelled"
    REVIEW_ABANDONED = "review abandoned"


@dataclass(frozen=True)
class TransitionDecision:
    state: PipelineState
    priority: int
    dispatch_phase: Phase | None = None
    next_action: str | None = None


@dataclass(frozen=True)
class RunDisposition:
    """Canonical operator-facing meaning of a Task Run projection."""

    display: str
    owner: str
    severity: str
    dispatchable: bool
    active: bool | None
    next_action: str | None


_DISPATCH_PHASE = {
    PipelineState.AWAITING_REVIEW: Phase.REVIEW,
    PipelineState.APPROVED: Phase.EXECUTE,
    PipelineState.AWAITING_SPEC: Phase.SPEC,
}
_RUN_STATES = {
    (Phase.SPEC, RunStatus.RUNNING): PipelineState.SPEC_RUNNING,
    (Phase.SPEC, RunStatus.FAILED): PipelineState.SPEC_FAILED,
    (Phase.SPEC, RunStatus.CANCELLED): PipelineState.SPEC_CANCELLED,
    (Phase.SPEC, RunStatus.ABANDONED): PipelineState.SPEC_ABANDONED,
    (Phase.EXECUTE, RunStatus.RUNNING): PipelineState.EXECUTE_RUNNING,
    (Phase.EXECUTE, RunStatus.FAILED): PipelineState.EXECUTE_FAILED,
    (Phase.EXECUTE, RunStatus.CANCELLED): PipelineState.EXECUTE_CANCELLED,
    (Phase.EXECUTE, RunStatus.ABANDONED): PipelineState.EXECUTE_ABANDONED,
    (Phase.REVIEW, RunStatus.RUNNING): PipelineState.REVIEW_RUNNING,
    (Phase.REVIEW, RunStatus.FAILED): PipelineState.REVIEW_FAILED,
    (Phase.REVIEW, RunStatus.CANCELLED): PipelineState.REVIEW_CANCELLED,
    (Phase.REVIEW, RunStatus.ABANDONED): PipelineState.REVIEW_ABANDONED,
}
_INTERRUPTED_STATES = {
    Phase.SPEC: PipelineState.SPEC_INTERRUPTED,
    Phase.EXECUTE: PipelineState.EXECUTE_INTERRUPTED,
    Phase.REVIEW: PipelineState.REVIEW_INTERRUPTED,
}
_RETRY_PHASE = {
    PipelineState.SPEC_INTERRUPTED: Phase.SPEC,
    PipelineState.SPEC_FAILED: Phase.SPEC,
    PipelineState.SPEC_CANCELLED: Phase.SPEC,
    PipelineState.SPEC_ABANDONED: Phase.SPEC,
    PipelineState.EXECUTE_INTERRUPTED: Phase.EXECUTE,
    PipelineState.EXECUTE_FAILED: Phase.EXECUTE,
    PipelineState.EXECUTE_CANCELLED: Phase.EXECUTE,
    PipelineState.EXECUTE_ABANDONED: Phase.EXECUTE,
    PipelineState.REVIEW_INTERRUPTED: Phase.REVIEW,
    PipelineState.REVIEW_FAILED: Phase.REVIEW,
    PipelineState.REVIEW_CANCELLED: Phase.REVIEW,
    PipelineState.REVIEW_ABANDONED: Phase.REVIEW,
}


def transition_for(
    state: PipelineState | str,
    *,
    issue: int | None = None,
) -> TransitionDecision:
    """Return canonical behavior for one pipeline state."""
    try:
        resolved = PipelineState(state)
    except ValueError as exc:
        raise ValueError(f"unknown pipeline state {state!r}") from exc
    dispatch_phase = _DISPATCH_PHASE.get(resolved)
    priority = _priority(resolved)
    action = _next_action(resolved, issue)
    return TransitionDecision(resolved, priority, dispatch_phase, action)


def classify_issue(
    record: RunRecord | None,
    *,
    claim_held: bool | None = None,
) -> TransitionDecision:
    """Classify one trigger-labeled Task with no open Task PR."""
    if record is None or record.status is RunStatus.RETRYABLE:
        return transition_for(PipelineState.AWAITING_SPEC, issue=_issue(record))
    if record.status is RunStatus.SUCCEEDED:
        return transition_for(PipelineState.SPEC_CLOSED, issue=record.issue)
    return classify_run(record, claim_held=claim_held)


def classify_pull_request(
    *,
    issue: int | None,
    is_draft: bool,
    labels: tuple[str, ...] | list[str],
    approved_label: str,
    approval_sha: str | None,
    head_sha: str,
    review_enabled: bool,
    execute_record: RunRecord | None = None,
    latest_record: RunRecord | None = None,
    claim_held: bool | None = None,
) -> TransitionDecision:
    """Classify one exact same-repository Task PR."""
    state = _remote_pr_state(
        is_draft=is_draft,
        labels=labels,
        approved_label=approved_label,
        approval_sha=approval_sha,
        head_sha=head_sha,
    )
    if _review_is_ready(
        review_enabled=review_enabled,
        is_draft=is_draft,
        head_sha=head_sha,
        execute_record=execute_record,
    ):
        state = PipelineState.AWAITING_REVIEW
    if latest_record is not None and latest_record.status in {
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.ABANDONED,
    }:
        return classify_run(latest_record, claim_held=claim_held)
    return transition_for(state, issue=issue)


def classify_run(
    record: RunRecord,
    *,
    claim_held: bool | None = None,
) -> TransitionDecision:
    """Project an active or terminal Task Run into a pipeline decision."""
    state: PipelineState | None
    if record.status is RunStatus.RUNNING and claim_held is False:
        state = _INTERRUPTED_STATES[record.phase]
    else:
        state = _RUN_STATES.get((record.phase, record.status))
    if state is None:
        raise ValueError(
            f"Task Run {record.phase.value}/{record.status.value} has no pipeline state"
        )
    return transition_for(state, issue=record.issue)


def describe_run(
    record: RunRecord,
    *,
    claim_held: bool | None = None,
) -> RunDisposition:
    """Map persistence vocabulary to one stable operator-facing state."""
    phase = record.phase.value
    if record.status is RunStatus.RETRYABLE:
        return RunDisposition(
            f"{phase} retryable",
            "watcher",
            "info",
            True,
            False,
            "machinist watch --once -v",
        )
    if record.status is RunStatus.SUCCEEDED:
        action = (
            f"machinist approve --issue {record.issue}"
            if record.phase is Phase.SPEC
            else None
        )
        return RunDisposition(
            f"{phase} succeeded", "operator", "success", False, False, action
        )

    decision = classify_run(record, claim_held=claim_held)
    if record.status is RunStatus.RUNNING and claim_held is not False:
        return RunDisposition(
            decision.state.value,
            "machinist",
            "info",
            False,
            claim_held,
            f"machinist cancel {record.issue}",
        )
    severity = (
        "error"
        if record.status is RunStatus.FAILED or record.status is RunStatus.RUNNING
        else "warning"
    )
    return RunDisposition(
        decision.state.value,
        "operator",
        severity,
        False,
        False,
        decision.next_action,
    )


def _remote_pr_state(
    *,
    is_draft: bool,
    labels: tuple[str, ...] | list[str],
    approved_label: str,
    approval_sha: str | None,
    head_sha: str,
) -> PipelineState:
    if not is_draft:
        return PipelineState.IN_REVIEW
    if approved_label not in labels:
        return PipelineState.AWAITING_APPROVAL
    if approval_sha is None:
        return PipelineState.APPROVAL_PENDING
    if approval_sha != head_sha:
        return PipelineState.APPROVAL_STALE
    return PipelineState.APPROVED


def _review_is_ready(
    *,
    review_enabled: bool,
    is_draft: bool,
    head_sha: str,
    execute_record: RunRecord | None,
) -> bool:
    return bool(
        review_enabled
        and is_draft
        and execute_record is not None
        and execute_record.status is RunStatus.SUCCEEDED
        and TaskEvidence.load(execute_record.evidence).pushed_sha == head_sha
    )


def _priority(state: PipelineState) -> int:
    if state is PipelineState.AWAITING_REVIEW:
        return 0
    if state is PipelineState.APPROVED:
        return 1
    if state is PipelineState.AWAITING_SPEC:
        return 2
    return 3


def _next_action(state: PipelineState, issue: int | None) -> str | None:
    if state in {PipelineState.AWAITING_SPEC, PipelineState.APPROVED}:
        return "machinist watch --once -v"
    if state in {
        PipelineState.AWAITING_APPROVAL,
        PipelineState.APPROVAL_PENDING,
        PipelineState.APPROVAL_STALE,
    }:
        return None if issue is None else f"machinist approve --issue {issue}"
    if state is PipelineState.AWAITING_REVIEW:
        return None if issue is None else f"machinist review {issue}"
    retry_phase = _RETRY_PHASE.get(state)
    if retry_phase is not None and issue is not None:
        return f"machinist retry {issue} --phase {retry_phase.value}"
    return None


def _issue(record: RunRecord | None) -> int | None:
    return None if record is None else record.issue
