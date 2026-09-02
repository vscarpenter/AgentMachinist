"""Table-driven contracts for pipeline transition decisions."""

import pytest

from machinist.lifecycle import Phase, RunRecord, RunStatus
from machinist.transitions import (
    PipelineState,
    classify_issue,
    classify_pull_request,
    transition_for,
)


def record(phase: Phase, status: RunStatus, *, evidence=None) -> RunRecord:
    return RunRecord(
        issue=42,
        phase=phase,
        status=status,
        attempt=1,
        started_at="2026-09-02T00:00:00+00:00",
        updated_at="2026-09-02T00:00:00+00:00",
        evidence=evidence or {},
    )


@pytest.mark.parametrize(
    ("state", "priority", "phase", "action"),
    [
        (PipelineState.AWAITING_REVIEW, 0, Phase.REVIEW, "machinist review 42"),
        (PipelineState.APPROVED, 1, Phase.EXECUTE, "machinist watch --once -v"),
        (PipelineState.AWAITING_SPEC, 2, Phase.SPEC, "machinist watch --once -v"),
        (PipelineState.AWAITING_APPROVAL, 3, None, "machinist approve --issue 42"),
        (PipelineState.SPEC_FAILED, 3, None, "machinist retry 42 --phase spec"),
        (PipelineState.IN_REVIEW, 3, None, None),
    ],
)
def test_transition_table_owns_priority_dispatch_and_next_action(
    state, priority, phase, action
):
    decision = transition_for(state, issue=42)

    assert decision.priority == priority
    assert decision.dispatch_phase is phase
    assert decision.next_action == action


def test_issue_transition_projects_retryable_back_to_awaiting_spec():
    decision = classify_issue(record(Phase.SPEC, RunStatus.RETRYABLE))

    assert decision.state is PipelineState.AWAITING_SPEC
    assert decision.dispatch_phase is Phase.SPEC


def test_issue_transition_distinguishes_running_from_interrupted_claim():
    active = classify_issue(record(Phase.SPEC, RunStatus.RUNNING), claim_held=True)
    interrupted = classify_issue(
        record(Phase.SPEC, RunStatus.RUNNING), claim_held=False
    )

    assert active.state is PipelineState.SPEC_RUNNING
    assert interrupted.state is PipelineState.SPEC_INTERRUPTED


def test_pull_request_transition_prioritizes_review_delivery_evidence():
    decision = classify_pull_request(
        issue=42,
        is_draft=True,
        labels=("machinist:approved",),
        approved_label="machinist:approved",
        approval_sha="a" * 40,
        head_sha="b" * 40,
        review_enabled=True,
        execute_record=record(
            Phase.EXECUTE,
            RunStatus.SUCCEEDED,
            evidence={"push_observed_sha": "b" * 40},
        ),
    )

    assert decision.state is PipelineState.AWAITING_REVIEW
    assert decision.dispatch_phase is Phase.REVIEW


def test_unknown_pipeline_state_fails_closed():
    with pytest.raises(ValueError, match="unknown pipeline state"):
        transition_for("mystery", issue=42)
