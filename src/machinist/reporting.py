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
from machinist.lifecycle import RunRecord, RunStatus

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
) -> MetricsReport:
    """Build a content-free aggregate from immutable Task Run attempts."""
    generated = generated_at or datetime.now(UTC)
    if since.tzinfo is None or generated.tzinfo is None:
        raise ReportingError("report timestamps must include a timezone")
    selected = tuple(record for record in records if _updated_at(record) >= since)
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
    )


def _updated_at(record: RunRecord) -> datetime:
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
        evidence = TaskEvidence.load(record.evidence)
        harness = evidence.harness
        usage = evidence.usage
        if harness is None or harness.get("structured_usage") is not True:
            continue
        if usage is None:
            continue
        for key in _USAGE_KEYS:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                totals[key] += value
    return {key: totals[key] for key in _USAGE_KEYS if key in totals}


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
