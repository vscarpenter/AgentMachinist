"""Pipeline status: where every machinist-managed issue and PR stands."""

from __future__ import annotations

from dataclasses import dataclass

from machinist.config import MachinistConfig
from machinist.github import normalize_repository_identity
from machinist.lifecycle import Phase, RunStatus

PIPELINE_STATES = (
    "awaiting spec",
    "awaiting approval",
    "approval pending",
    "approval stale",
    "approved",
    "in review",
)


@dataclass(frozen=True)
class StatusRow:
    kind: str  # "issue" | "pr"
    number: int
    title: str
    state: str  # "awaiting spec" | "awaiting approval" | "approved" | "in review"
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
        state = "awaiting spec"
        if lifecycle is not None:
            spec_record = lifecycle.record(issue.number, Phase.SPEC)
            if spec_record is not None and spec_record.status is RunStatus.SUCCEEDED:
                # A manually closed Spec PR must not become an apparently new
                # Task that watch will repeatedly try (and fail) to recreate.
                state = "spec closed"
            elif (
                spec_record is not None
                and spec_record.status is not RunStatus.RETRYABLE
            ):
                # Durable in-flight and terminal outcomes require an explicit
                # operator transition. RETRYABLE deliberately projects back
                # to the remote eligible state so a live watcher can resume it.
                state = f"spec {spec_record.status.value}"
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
        # Draft-ness outranks the label: once the agent marks a PR ready,
        # a leftover approval label must not make it look runnable again.
        if not pr.is_draft:
            state = "in review"
        elif approved_label in pr.labels:
            approval_sha = github.approval_sha(pr.number)
            if approval_sha is None:
                state = "approval pending"
            elif approval_sha != pr.head_sha:
                state = "approval stale"
            else:
                state = "approved"
        else:
            state = "awaiting approval"
        issue_number = _issue_number_from_branch(pr.branch, prefix)
        if lifecycle is not None and issue_number is not None:
            record = lifecycle.latest(issue_number)
            if record is not None and record.status in {
                RunStatus.RUNNING,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.ABANDONED,
            }:
                state = f"{record.phase.value} {record.status.value}"
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
    if row.state == "approved":
        return 0
    if row.state == "awaiting spec":
        return 1
    return 2
