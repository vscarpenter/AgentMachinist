"""The watch daemon's single dispatch pass.

pipeline_status is the source of truth: 'awaiting spec' rows get Phase 1,
'approved' rows (label AND still draft) get Phase 3. A failed issue is
remembered for the daemon's lifetime and never re-dispatched — retrying a
deterministic failure every poll would burn harness time for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from machinist.config import MachinistConfig
from machinist.phases.status import pipeline_status


@dataclass
class WatchState:
    failed_issues: set[int] = field(default_factory=set)


def watch_once(
    config: MachinistConfig, github, *,
    run_spec, run_execute, state: WatchState,
    notify: Callable[[str], None] | None = None,
) -> list[str]:
    events: list[str] = []
    for row in pipeline_status(config, github):
        issue_number = row.issue_number
        if issue_number is None or issue_number in state.failed_issues:
            continue
        if row.state == "awaiting spec":
            _dispatch(
                run_spec, issue_number, state, events,
                phase="spec",
                success=lambda pr: f"spec: issue #{issue_number} → draft PR #{pr.number} ({pr.url})",
                notify=notify,
            )
        elif row.state == "approved":
            _dispatch(
                run_execute, issue_number, state, events,
                phase="execute",
                success=lambda pr: f"execute: issue #{issue_number} → PR #{pr.number} ready for review ({pr.url})",
                notify=notify,
            )
    return events


def _dispatch(action, issue_number: int, state: WatchState, events: list[str], *, phase: str, success, notify=None) -> None:
    try:
        pr = action(issue_number)
    except Exception as exc:  # daemon must outlive any single task's failure
        state.failed_issues.add(issue_number)
        message = f"{phase} for issue #{issue_number} failed: {exc}"
        events.append(f"error: {message}")
        if notify is not None:
            notify(message)
        return
    events.append(success(pr))
