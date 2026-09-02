"""Pipeline status: where every machinist-managed issue and PR stands."""

from __future__ import annotations

from dataclasses import dataclass

from machinist.config import MachinistConfig
from machinist.github import normalize_repository_identity
from machinist.lifecycle import Phase
from machinist.transitions import (
    PipelineState,
    classify_issue,
    classify_pull_request,
    transition_for,
)

PIPELINE_STATES = tuple(state.value for state in PipelineState)


@dataclass(frozen=True)
class StatusRow:
    kind: str  # "issue" | "pr"
    number: int
    title: str
    state: PipelineState
    url: str
    issue_number: int | None = None


def pipeline_status(
    config: MachinistConfig, github, *, lifecycle=None
) -> list[StatusRow]:
    prefix = config.workspace.branch_prefix
    issues = github.issues_with_label(config.github.labels.trigger)
    expected_repository = normalize_repository_identity(
        getattr(github, "repo", None)
    ) or normalize_repository_identity(config.github.repo)
    prs = [
        pr
        for pr in github.open_machinist_prs(prefix)
        if not pr.is_cross_repository
        and (
            expected_repository is None
            or pr.head_repository is None
            or normalize_repository_identity(pr.head_repository) == expected_repository
        )
    ]

    covered = {
        number
        for pr in prs
        if (number := _issue_number_from_branch(pr.branch, prefix)) is not None
    }

    issue_rows = []
    for issue in issues:
        if issue.number in covered:
            continue
        spec_record = (
            None if lifecycle is None else lifecycle.record(issue.number, Phase.SPEC)
        )
        held = None if lifecycle is None else lifecycle.claim_held(issue.number)
        state = classify_issue(spec_record, claim_held=held).state
        issue_rows.append(
            StatusRow(
                kind="issue",
                number=issue.number,
                title=issue.title,
                state=state,
                url=issue.url,
                issue_number=issue.number,
            )
        )
    pr_rows: list[StatusRow] = []
    approved_label = config.github.labels.approved
    for pr in prs:
        issue_number = _issue_number_from_branch(pr.branch, prefix)
        approval_sha = (
            github.approval_sha(pr.number)
            if pr.is_draft and approved_label in pr.labels
            else None
        )
        execute = (
            lifecycle.record(issue_number, Phase.EXECUTE)
            if lifecycle is not None and issue_number is not None
            else None
        )
        latest = (
            lifecycle.latest(issue_number)
            if lifecycle is not None and issue_number is not None
            else None
        )
        held = (
            lifecycle.claim_held(issue_number)
            if lifecycle is not None and issue_number is not None
            else None
        )
        state = classify_pull_request(
            issue=issue_number,
            is_draft=pr.is_draft,
            labels=pr.labels,
            approved_label=approved_label,
            approval_sha=approval_sha,
            head_sha=pr.head_sha,
            review_enabled=config.review.enabled,
            execute_record=execute,
            latest_record=latest,
            claim_held=held,
        ).state
        pr_rows.append(
            StatusRow(
                kind="pr",
                number=pr.number,
                title=pr.title,
                state=state,
                url=pr.url,
                issue_number=issue_number,
            )
        )

    # Approved implementation work has already crossed the human gate.  Put it
    # ahead of new planning work so a backlog of labeled issues cannot delay an
    # approved Execute task for hours.  Python's stable sort preserves GitHub's
    # order within each priority class and leaves non-actionable rows last.
    rows = [*pr_rows, *issue_rows]
    return sorted(rows, key=_dispatch_priority)


def _issue_number_from_branch(branch: str, prefix: str) -> int | None:
    tail = branch.removeprefix(prefix)
    if tail.startswith("issue-") and tail[6:].isdigit():
        return int(tail[6:])
    return None


def _dispatch_priority(row: StatusRow) -> int:
    return transition_for(row.state, issue=row.issue_number).priority


def next_action_for_status(row: StatusRow) -> str | None:
    return transition_for(row.state, issue=row.issue_number).next_action
