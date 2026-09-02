"""Plan and execute one isolated watch-daemon dispatch pass.

``pipeline_status`` is the source of truth: ``approved`` rows get Execute
before ``awaiting spec`` rows get Spec.  A pass returns structured attempts,
failures, and deferred tasks while remaining iterable over human-readable
events for compatibility with the original CLI.

Task lifecycle state, not this process-local object, owns retry eligibility.
Durable active and terminal records stay out of the dispatch plan, while an
explicit ``machinist retry`` becomes visible to an already-running watcher on
its next pass.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Callable

from machinist.config import MachinistConfig, SpecSource
from machinist.phases.status import StatusRow, pipeline_status
from machinist.transitions import transition_for


@dataclass
class WatchState:
    """Ephemeral presentation state; never a source of retry eligibility."""

    notified_failures: dict[tuple[str, int], str] = field(default_factory=dict)
    notified_stale_approvals: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchTask:
    phase: str
    issue_number: int
    row: StatusRow


@dataclass(frozen=True)
class WatchFailure:
    phase: str
    issue_number: int
    message: str
    exception_type: str


@dataclass(frozen=True, eq=False)
class WatchResult(Sequence[str]):
    """Structured pass result with list-like access to display events."""

    events: tuple[str, ...] = ()
    failures: tuple[WatchFailure, ...] = ()
    attempted: tuple[WatchTask, ...] = ()
    deferred: tuple[WatchTask, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    def __iter__(self) -> Iterator[str]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, index):
        return self.events[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WatchResult):
            return (
                self.events == other.events
                and self.failures == other.failures
                and self.attempted == other.attempted
                and self.deferred == other.deferred
            )
        if isinstance(other, (list, tuple)):
            return self.events == tuple(other)
        return NotImplemented


Admission = Callable[[WatchTask], bool]


def plan_watch_tasks(
    config: MachinistConfig,
    github,
    *,
    rows: Sequence[StatusRow] | None = None,
    lifecycle=None,
) -> tuple[WatchTask, ...]:
    """Return the ordered eligible work without dispatching a phase.

    ``lifecycle`` is optional for compatibility with callers that only need a
    GitHub projection.  Daemon callers should supply it so durable local
    outcomes participate in eligibility decisions.
    """

    tasks: list[WatchTask] = []
    projected_rows = (
        rows
        if rows is not None
        else pipeline_status(config, github, lifecycle=lifecycle)
    )
    for row in projected_rows:
        if row.issue_number is None:
            continue
        decision = transition_for(row.state, issue=row.issue_number)
        phase = decision.dispatch_phase
        if phase is None:
            continue
        if phase.value == "spec" and config.github.spec_source is not SpecSource.LOCAL:
            continue
        tasks.append(WatchTask(phase.value, row.issue_number, row))

    # Keep the ordering contract local even if the human-facing status order is
    # changed later. Stable sorting preserves GitHub order within each phase.
    return tuple(
        sorted(
            tasks,
            key=lambda task: (
                transition_for(task.row.state, issue=task.issue_number).priority
            ),
        )
    )


def watch_once(
    config: MachinistConfig,
    github,
    *,
    run_spec,
    run_execute,
    run_review=None,
    state: WatchState,
    notify: Callable[[str], None] | None = None,
    notify_stale: Callable[[int, str], None] | None = None,
    max_tasks: int | None = None,
    admit: Admission | None = None,
    lifecycle=None,
) -> WatchResult:
    """Dispatch eligible work, isolating failures and recording deferrals.

    ``max_tasks`` is a per-pass admission ceiling. ``admit`` can implement a
    future pause, defer, or policy decision without changing phase execution;
    rejected tasks are returned in ``deferred``. For a side-effect-free dry
    run, callers can use :func:`plan_watch_tasks` directly.
    """

    if max_tasks is not None and max_tasks < 0:
        raise ValueError("max_tasks must be zero or greater")

    rows = pipeline_status(config, github, lifecycle=lifecycle)
    stale_now: set[int] = set()
    for row in rows:
        if row.state != "approval stale" or row.issue_number is None:
            continue
        stale_now.add(row.issue_number)
        fingerprint = f"{row.number}:{row.state}:{row.title}"
        if (
            notify_stale is not None
            and state.notified_stale_approvals.get(row.issue_number) != fingerprint
        ):
            notify_stale(row.issue_number, f"PR #{row.number} approval is stale")
        state.notified_stale_approvals[row.issue_number] = fingerprint
    for issue_number in set(state.notified_stale_approvals) - stale_now:
        state.notified_stale_approvals.pop(issue_number, None)

    events: list[str] = []
    failures: list[WatchFailure] = []
    attempted: list[WatchTask] = []
    deferred: list[WatchTask] = []
    for task in plan_watch_tasks(config, github, rows=rows):
        if max_tasks is not None and len(attempted) >= max_tasks:
            deferred.append(task)
            continue
        # Admission is intentionally evaluated at the dispatch boundary.  A
        # prior task in this same pass may have consumed a durable daily task
        # or runtime budget, requested cancellation, or changed queue control.
        if admit is not None and not admit(task):
            deferred.append(task)
            continue
        attempted.append(task)
        if task.phase == "review":
            action = run_review
            if action is None:
                raise ValueError("run_review is required when Review is enabled")
        else:
            action = run_execute if task.phase == "execute" else run_spec
        event, failure = _dispatch(
            action,
            task,
            state,
            success=_success_message(task),
            notify=notify,
        )
        events.append(event)
        if failure is not None:
            failures.append(failure)

    return WatchResult(
        events=tuple(events),
        failures=tuple(failures),
        attempted=tuple(attempted),
        deferred=tuple(deferred),
    )


def _success_message(task: WatchTask):
    if task.phase == "review":
        return lambda pr, issue=task.issue_number: (
            f"review: issue #{issue} → PR #{pr.number} independent review complete "
            f"({pr.url})"
        )
    if task.phase == "execute":
        return lambda pr, issue=task.issue_number: (
            f"execute: issue #{issue} → PR #{pr.number} delivered ({pr.url})"
        )
    return lambda pr, issue=task.issue_number: (
        f"spec: issue #{issue} → draft PR #{pr.number} ({pr.url})"
    )


def _dispatch(action, task: WatchTask, state: WatchState, *, success, notify=None):
    key = (task.phase, task.issue_number)
    try:
        pr = action(task.issue_number)
    except Exception as exc:  # daemon must outlive any single task's failure
        message = f"{task.phase} for issue #{task.issue_number} failed: {exc}"
        failure = WatchFailure(
            phase=task.phase,
            issue_number=task.issue_number,
            message=message,
            exception_type=type(exc).__name__,
        )
        if notify is not None and state.notified_failures.get(key) != message:
            notify(message)
        state.notified_failures[key] = message
        return f"error: {message}", failure
    state.notified_failures.pop(key, None)
    return success(pr), None
