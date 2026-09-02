"""Read-only queue admission policy for a solo-developer watcher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from machinist.config import MachinistConfig
from machinist.lifecycle import (
    LifecycleError,
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
        records = {
            (record.issue, record.phase.value, record.attempt): record
            for record in inventory.attempts
        }
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
    if inventory.corrupt:
        return _corrupt_history_decision()
    return AdmissionDecision(True)


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
