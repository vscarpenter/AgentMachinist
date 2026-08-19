"""Read-only queue admission policy for a solo-developer watcher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from machinist.config import MachinistConfig
from machinist.lifecycle import (
    LifecycleError,
    Phase,
    RunInventory,
    RunRecord,
    RunStatus,
    TaskLifecycle,
)


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str | None = None


def queue_admission(
    config: MachinistConfig,
    lifecycle: TaskLifecycle,
    *,
    now: datetime | None = None,
    additionally_admitted: int = 0,
) -> AdmissionDecision:
    """Evaluate allowed hours and durable daily task/runtime ceilings."""
    moment = now or datetime.now(UTC)
    allowed_hours = config.queue.allowed_hours
    if allowed_hours is not None and not allowed_hours.contains(moment):
        return AdmissionDecision(False, "outside configured queue.allowed_hours")

    budget = config.queue.task_budget
    if budget is None:
        return AdmissionDecision(True)

    try:
        inventory = lifecycle.inventory()
        records, unrecognized_history = _budget_records(lifecycle, inventory)
    except (LifecycleError, OSError):
        return _corrupt_history_decision()

    timezone = (
        moment.astimezone().tzinfo
        if budget.timezone == "local"
        else ZoneInfo(budget.timezone)
    )
    local_now = moment.astimezone(timezone)
    today = local_now.date()
    try:
        todays = [
            record
            for record in records.values()
            if _started_at(record).astimezone(timezone).date() == today
            and record.status is not RunStatus.RETRYABLE
        ]
    except (OverflowError, ValueError):
        return _corrupt_history_decision()

    if (
        budget.max_tasks_per_day is not None
        and len(todays) + additionally_admitted >= budget.max_tasks_per_day
    ):
        return AdmissionDecision(
            False,
            f"daily Task budget reached ({budget.max_tasks_per_day})",
        )

    elapsed_minutes = sum(_duration_seconds(record, moment) for record in todays) / 60
    if (
        budget.max_runtime_minutes_per_day is not None
        and elapsed_minutes >= budget.max_runtime_minutes_per_day
    ):
        return AdmissionDecision(
            False,
            "daily runtime budget reached "
            f"({budget.max_runtime_minutes_per_day} minutes)",
        )
    if inventory.corrupt or unrecognized_history:
        return _corrupt_history_decision()
    return AdmissionDecision(True)


def _budget_records(
    lifecycle: TaskLifecycle,
    inventory: RunInventory,
) -> tuple[dict[tuple[int, str, int], RunRecord], bool]:
    """Load one durable record per attempt, including orphaned journals.

    Current projection files are compatibility snapshots, not the source of
    truth for attempt accounting. Discover journal identities from their
    controller-owned paths, then let ``TaskLifecycle.history`` decode and
    collapse their append-only events. Projections fill only legacy gaps so a
    projected attempt is never counted a second time.
    """
    identities = {(record.issue, record.phase) for record in inventory.records}
    journal_identities, unrecognized_history = _journal_identities(
        lifecycle.history_root()
    )
    identities.update(journal_identities)

    records: dict[tuple[int, str, int], RunRecord] = {}
    for issue, phase in sorted(identities, key=lambda item: (item[0], item[1].value)):
        for record in lifecycle.history(issue, phase):
            records[(record.issue, record.phase.value, record.attempt)] = record

    for projection in inventory.records:
        records.setdefault(
            (projection.issue, projection.phase.value, projection.attempt),
            projection,
        )
    return records, unrecognized_history


def _journal_identities(history_root: Path) -> tuple[set[tuple[int, Phase]], bool]:
    """Return valid issue/phase identities represented by journal artifacts."""
    if not history_root.exists():
        return set(), False

    identities: set[tuple[int, Phase]] = set()
    unrecognized = False
    for journal in history_root.glob("*/attempt-*.jsonl"):
        identity = _parse_journal_directory(journal.parent.name)
        if identity is None:
            unrecognized = True
        else:
            identities.add(identity)
    return identities, unrecognized


def _parse_journal_directory(name: str) -> tuple[int, Phase] | None:
    prefix = "issue-"
    if not name.startswith(prefix):
        return None
    for phase in Phase:
        suffix = f"-{phase.value}"
        if not name.endswith(suffix):
            continue
        issue_text = name[len(prefix) : -len(suffix)]
        if issue_text.isdigit() and int(issue_text) > 0:
            return int(issue_text), phase
    return None


def _corrupt_history_decision() -> AdmissionDecision:
    return AdmissionDecision(
        False,
        "runtime history is corrupt; refusing budgeted dispatch until inspected",
    )


def _started_at(record: RunRecord) -> datetime:
    value = datetime.fromisoformat(record.started_at)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _duration_seconds(record: RunRecord, now: datetime) -> float:
    if record.duration_seconds is not None:
        return max(0.0, record.duration_seconds)
    if record.status is RunStatus.RUNNING:
        return max(
            0.0, (now - _started_at(record).astimezone(now.tzinfo)).total_seconds()
        )
    return 0.0
