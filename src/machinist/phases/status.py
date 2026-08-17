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
                  state="awaiting spec", url=i.url)
        for i in issues
        if i.number not in covered
    ]
    approved_label = config.github.labels.approved
    for pr in prs:
        if approved_label in pr.labels:
            state = "approved"
        elif pr.is_draft:
            state = "awaiting approval"
        else:
            state = "in review"
        rows.append(StatusRow(kind="pr", number=pr.number, title=pr.title,
                              state=state, url=pr.url))
    return rows


def _issue_number_from_branch(branch: str, prefix: str) -> int | None:
    tail = branch.removeprefix(prefix)
    if tail.startswith("issue-") and tail[6:].isdigit():
        return int(tail[6:])
    return None
