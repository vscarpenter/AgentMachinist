"""Pipeline status: where every machinist-managed issue and PR stands."""

from __future__ import annotations

from dataclasses import dataclass

from machinist.config import MachinistConfig


@dataclass(frozen=True)
class StatusRow:
    kind: str    # "issue" | "pr"
    number: int
    title: str
    state: str   # "awaiting spec" | "awaiting approval" | "approved" | "in review"
    url: str
    issue_number: int | None = None


def pipeline_status(config: MachinistConfig, github) -> list[StatusRow]:
    prefix = config.workspace.branch_prefix
    issues = github.issues_with_label(config.github.labels.trigger)
    prs = github.open_machinist_prs(prefix)

    covered = {
        number for pr in prs
        if (number := _issue_number_from_branch(pr.branch, prefix)) is not None
    }

    rows = [
        StatusRow(kind="issue", number=i.number, title=i.title,
                  state="awaiting spec", url=i.url, issue_number=i.number)
        for i in issues
        if i.number not in covered
    ]
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
        rows.append(StatusRow(kind="pr", number=pr.number, title=pr.title,
                              state=state, url=pr.url,
                              issue_number=_issue_number_from_branch(pr.branch, prefix)))
    return rows


def _issue_number_from_branch(branch: str, prefix: str) -> int | None:
    tail = branch.removeprefix(prefix)
    if tail.startswith("issue-") and tail[6:].isdigit():
        return int(tail[6:])
    return None
