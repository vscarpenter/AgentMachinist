"""Aggregate local Task Run metrics and optional OTLP/HTTP JSON export."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from machinist.evidence import TaskEvidence
from machinist.lifecycle import Phase, RunRecord, RunStatus
from machinist.local_tasks import LocalTask

_DURATION = re.compile(r"^([1-9][0-9]*)([hdw])$")
_EXCEPTION_TYPE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):")
_TERMINAL = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.ABANDONED,
    }
)
_GATE_FAILURE_STATUSES = frozenset(
    {"failed", "timed_out", "cancelled", "mutation_detected"}
)
_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


class ReportingError(Exception):
    """A report window, record, or export request is invalid."""


@dataclass(frozen=True)
class MetricSeries:
    phase: str
    status: str
    harness: str | None
    model: str | None
    count: int

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "phase": self.phase,
            "status": self.status,
            "harness": self.harness,
            "model": self.model,
            "count": self.count,
        }


@dataclass(frozen=True)
class MetricsReport:
    since: str
    generated_at: str
    attempts: int
    outcomes: dict[str, int]
    by_phase: dict[str, dict[str, int]]
    success_rate: float | None
    retry_count: int
    cancellation_count: int
    duration_seconds: dict[str, float | None]
    failure_categories: dict[str, int]
    gate_failures: dict[str, int]
    harnesses: tuple[dict[str, str | int | None], ...]
    token_totals: dict[str, int]
    series: tuple[MetricSeries, ...]
    by_source: dict[str, int]
    task_counts: dict[str, int]
    first_pass_execute: dict[str, int | float | None]
    repairs: dict[str, Any]
    usage_coverage: dict[str, Any]
    local_delivery: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "since": self.since,
            "generated_at": self.generated_at,
            "attempts": self.attempts,
            "outcomes": self.outcomes,
            "by_phase": self.by_phase,
            "success_rate": self.success_rate,
            "retry_count": self.retry_count,
            "cancellation_count": self.cancellation_count,
            "duration_seconds": self.duration_seconds,
            "failure_categories": self.failure_categories,
            "gate_failures": self.gate_failures,
            "harnesses": list(self.harnesses),
            "token_totals": self.token_totals,
            "series": [item.to_dict() for item in self.series],
            "by_source": self.by_source,
            "task_counts": self.task_counts,
            "first_pass_execute": self.first_pass_execute,
            "repairs": self.repairs,
            "usage_coverage": self.usage_coverage,
            "local_delivery": self.local_delivery,
        }


def parse_since_duration(value: str) -> timedelta:
    """Parse a positive integer reporting window in hours, days, or weeks."""
    match = _DURATION.fullmatch(value)
    if match is None:
        raise ReportingError(
            "report window must be a positive integer followed by h, d, or w"
        )
    amount = int(match.group(1))
    unit = match.group(2)
    return {
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
    }[unit]


def build_metrics_report(
    records: Iterable[RunRecord],
    *,
    since: datetime,
    generated_at: datetime | None = None,
    local_records: Iterable[RunRecord] = (),
    local_tasks: Iterable[LocalTask] = (),
) -> MetricsReport:
    """Aggregate Phase attempts while preserving legacy/local Task namespaces.

    ``success_rate`` is successful terminal Phase attempts / terminal Phase
    attempts, not whole-Task acceptance. Local delivery counts are current
    stored snapshots of Tasks updated in the window, not delivery events.
    """
    generated = generated_at or datetime.now(UTC)
    if since.tzinfo is None or generated.tzinfo is None:
        raise ReportingError("report timestamps must include a timezone")
    local_history = _unique_attempts(local_records)
    sources = {
        "legacy": tuple(
            record
            for record in _unique_attempts(records)
            if _updated_at(record) >= since
        ),
        "local": tuple(
            record for record in local_history if _updated_at(record) >= since
        ),
    }
    selected = sources["legacy"] + sources["local"]
    outcome_counts = Counter(record.status.value for record in selected)
    by_phase = _phase_counts(selected)
    durations = sorted(
        record.duration_seconds
        for record in selected
        if record.duration_seconds is not None
    )
    terminal_count = sum(
        count
        for status, count in outcome_counts.items()
        if RunStatus(status) in _TERMINAL
    )
    series = _series(selected)
    return MetricsReport(
        since=since.astimezone(UTC).isoformat(),
        generated_at=generated.astimezone(UTC).isoformat(),
        attempts=len(selected),
        outcomes=dict(sorted(outcome_counts.items())),
        by_phase=by_phase,
        success_rate=(
            outcome_counts[RunStatus.SUCCEEDED.value] / terminal_count
            if terminal_count
            else None
        ),
        retry_count=sum(record.attempt > 1 for record in selected),
        cancellation_count=outcome_counts[RunStatus.CANCELLED.value],
        duration_seconds={
            "median": _median(durations),
            "p95": _percentile_95(durations),
        },
        failure_categories=_failure_categories(selected),
        gate_failures=_gate_failures(selected),
        harnesses=_harness_breakdown(series),
        token_totals=_token_totals(selected),
        series=series,
        by_source={source: len(attempts) for source, attempts in sources.items()},
        task_counts={
            source: len({record.issue for record in attempts})
            for source, attempts in sources.items()
        },
        first_pass_execute=_first_pass_execute(selected),
        repairs=_repair_metrics(sources),
        usage_coverage=_usage_coverage(selected),
        local_delivery=_local_delivery(local_tasks, local_history, since=since),
    )


def _unique_attempts(records: Iterable[RunRecord]) -> tuple[RunRecord, ...]:
    """Choose the latest snapshot of each attempt inside one identity namespace."""
    latest: dict[tuple[int, Phase, int], RunRecord] = {}
    for record in records:
        key = (record.issue, record.phase, record.attempt)
        previous = latest.get(key)
        if previous is None or _updated_at(record) >= _updated_at(previous):
            latest[key] = record
    return tuple(latest.values())


def _first_pass_execute(
    records: tuple[RunRecord, ...],
) -> dict[str, int | float | None]:
    first = tuple(
        record
        for record in records
        if record.phase is Phase.EXECUTE
        and record.attempt == 1
        and record.status in _TERMINAL
    )
    successes = sum(
        record.status is RunStatus.SUCCEEDED
        and TaskEvidence.load(record.evidence).repair is None
        for record in first
    )
    return {
        "terminal_attempts": len(first),
        "succeeded_without_repair": successes,
        "success_rate": successes / len(first) if first else None,
    }


def _repair_metrics(sources: dict[str, tuple[RunRecord, ...]]) -> dict[str, Any]:
    # Explicit resumed attempts can carry the same repair Evidence. Count each
    # consumed round once, using its latest durable snapshot within the window.
    rounds: dict[tuple[str, int, str, int], tuple[datetime, dict[str, Any]]] = {}
    for source, records in sources.items():
        for record in records:
            if record.phase is not Phase.EXECUTE:
                continue
            repair = TaskEvidence.load(record.evidence).repair
            attempts = repair.get("attempts") if repair is not None else None
            if not isinstance(attempts, list):
                continue
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                started = attempt.get("started_at")
                number = attempt.get("attempt")
                if not isinstance(started, str) or type(number) is not int:
                    continue
                key = (source, record.issue, started, number)
                updated = _updated_at(record)
                if key not in rounds or updated >= rounds[key][0]:
                    rounds[key] = (updated, attempt)
    statuses = Counter(
        status
        for _, attempt in rounds.values()
        if isinstance(status := attempt.get("status"), str)
    )
    durations = sorted(
        float(value)
        for _, attempt in rounds.values()
        if isinstance(value := attempt.get("duration_seconds"), (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )
    verified = statuses["succeeded"]
    unsuccessful = sum(
        statuses[status]
        for status in ("failed", "timed_out", "cancelled", "interrupted")
    )
    return {
        "attempted_rounds": len(rounds),
        "verified_rounds": verified,
        "unsuccessful_rounds": unsuccessful,
        "incomplete_rounds": len(rounds) - verified - unsuccessful,
        "success_rate": verified / len(rounds) if rounds else None,
        "duration_seconds": {
            "total": sum(durations) if durations else None,
            "median": _median(durations),
            "p95": _percentile_95(durations),
        },
    }


def _local_delivery(
    tasks: Iterable[LocalTask],
    records: tuple[RunRecord, ...],
    *,
    since: datetime,
) -> dict[str, int]:
    counts = {
        "tasks_updated": 0,
        "reviewed_candidates": 0,
        "integrated": 0,
        "published": 0,
    }
    latest: dict[tuple[int, Phase], RunRecord] = {}
    for record in records:
        key = (record.issue, record.phase)
        if key not in latest or record.attempt > latest[key].attempt:
            latest[key] = record
    for task in tasks:
        if _updated_at(task) < since:
            continue
        counts["tasks_updated"] += 1
        if not _has_reviewed_candidate(task, latest):
            continue
        counts["reviewed_candidates"] += 1
        if task.integration and all(
            task.integration.get(key) == task.candidate_sha
            for key in ("candidate_sha", "observed_sha")
        ):
            counts["integrated"] += 1
        if (
            task.publication
            and task.publication.get("stage") == "published"
            and task.publication.get("published_sha") == task.candidate_sha
        ):
            counts["published"] += 1
    return counts


def _has_reviewed_candidate(
    task: LocalTask, latest: dict[tuple[int, Phase], RunRecord]
) -> bool:
    if not task.candidate_sha or not task.spec_sha:
        return False
    if not task.approval or any(
        task.approval.get(key) != value
        for key, value in {
            "repository": task.repository,
            "task_id": task.id,
            "spec_sha": task.spec_sha,
        }.items()
    ):
        return False
    if (
        not task.review_report
        or task.review_report.get("completed") is not True
        or task.review_report.get("reviewed_sha") != task.candidate_sha
    ):
        return False
    execute = latest.get((task.number, Phase.EXECUTE))
    review = latest.get((task.number, Phase.REVIEW))
    if (
        execute is None
        or review is None
        or execute.status is not RunStatus.SUCCEEDED
        or review.status is not RunStatus.SUCCEEDED
    ):
        return False
    implementation = TaskEvidence.load(execute.evidence)
    inspection = TaskEvidence.load(review.evidence)
    return (
        implementation.implementation_sha == task.candidate_sha
        and implementation.approved_sha == task.spec_sha
        and inspection.reviewed_sha == task.candidate_sha
    )


def _updated_at(record: RunRecord | LocalTask) -> datetime:
    try:
        value = datetime.fromisoformat(record.updated_at)
    except ValueError as exc:
        raise ReportingError("Task Run has an invalid updated_at timestamp") from exc
    if value.tzinfo is None:
        raise ReportingError("Task Run updated_at timestamp has no timezone")
    return value


def _phase_counts(records: tuple[RunRecord, ...]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = {}
    for record in records:
        counts.setdefault(record.phase.value, Counter())[record.status.value] += 1
    return {
        phase: dict(sorted(statuses.items()))
        for phase, statuses in sorted(counts.items())
    }


def _series(records: tuple[RunRecord, ...]) -> tuple[MetricSeries, ...]:
    counts: Counter[tuple[str, str, str | None, str | None]] = Counter()
    for record in records:
        harness, model = _harness_identity(record)
        counts[(record.phase.value, record.status.value, harness, model)] += 1
    return tuple(
        MetricSeries(*key, count)
        for key, count in sorted(counts.items(), key=lambda item: str(item[0]))
    )


def _harness_identity(record: RunRecord) -> tuple[str | None, str | None]:
    harness = TaskEvidence.load(record.evidence).harness
    if harness is None:
        return None, None
    name = harness.get("name")
    model = harness.get("model")
    return (
        name if isinstance(name, str) and name else None,
        model if isinstance(model, str) and model else None,
    )


def _failure_categories(records: tuple[RunRecord, ...]) -> dict[str, int]:
    categories: Counter[str] = Counter()
    for record in records:
        if record.status is not RunStatus.FAILED:
            continue
        match = _EXCEPTION_TYPE.match(record.error or "")
        error_type = match.group(1) if match is not None else "controller"
        categories[f"{error_type}@{_checkpoint(record)}"] += 1
    return dict(sorted(categories.items()))


def _checkpoint(record: RunRecord) -> str:
    stage = TaskEvidence.load(record.evidence).current_stage
    if stage is None:
        return "controller"
    for prefix in (
        "verification",
        "independent review",
        "generate spec",
        "implement",
        "commit",
        "push",
    ):
        if stage.casefold().startswith(prefix):
            return prefix.replace(" ", "-")
    return "controller"


def _gate_failures(records: tuple[RunRecord, ...]) -> dict[str, int]:
    failures: Counter[str] = Counter()
    for record in records:
        report = TaskEvidence.load(record.evidence).verification_report
        gates = report.get("gates") if report is not None else None
        if not isinstance(gates, list):
            continue
        for gate in gates:
            status = gate.get("status") if isinstance(gate, dict) else None
            if status in _GATE_FAILURE_STATUSES:
                failures[status] += 1
    return dict(sorted(failures.items()))


def _harness_breakdown(
    series: tuple[MetricSeries, ...],
) -> tuple[dict[str, str | int | None], ...]:
    counts: Counter[tuple[str, str | None]] = Counter()
    for item in series:
        if item.harness is not None:
            counts[(item.harness, item.model)] += item.count
    return tuple(
        {"name": name, "model": model, "attempts": count}
        for (name, model), count in sorted(
            counts.items(), key=lambda item: str(item[0])
        )
    )


def _token_totals(records: tuple[RunRecord, ...]) -> dict[str, int]:
    totals: Counter[str] = Counter()
    for record in records:
        totals.update(_known_usage(record))
    return {key: totals[key] for key in _USAGE_KEYS if key in totals}


def _known_usage(record: RunRecord) -> dict[str, int]:
    evidence = TaskEvidence.load(record.evidence)
    harness, usage = evidence.harness, evidence.usage
    if harness is None or harness.get("structured_usage") is not True or usage is None:
        return {}
    return {
        key: value
        for key in _USAGE_KEYS
        if type(value := usage.get(key)) is int and value >= 0
    }


def _usage_coverage(records: tuple[RunRecord, ...]) -> dict[str, Any]:
    known = tuple(_known_usage(record) for record in records)
    count = sum(bool(usage) for usage in known)
    return {
        "attempts_with_usage": count,
        "attempts_without_usage": len(records) - count,
        "by_token": {key: sum(key in usage for usage in known) for key in _USAGE_KEYS},
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return float(values[middle])
    return float((values[middle - 1] + values[middle]) / 2)


def _percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    index = max(0, math.ceil(len(values) * 0.95) - 1)
    return float(values[index])
