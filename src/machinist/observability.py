"""Read-only, JSON-safe observability for local Task Runs.

The lifecycle remains the source of truth.  This module turns its projections
and append-only attempt history into a stable read model that command surfaces
can render as JSON or concise human text.  Optional remote loaders are isolated
from one another so an unavailable source never hides usable local state.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from machinist.evidence import TaskEvidence
from machinist.lifecycle import Phase, RunRecord, RunStatus, TaskLifecycle

_READ_MODEL_SCHEMA_VERSION = 1

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)
type RemoteLoader = Callable[[], JsonValue]


@dataclass(frozen=True)
class SourceError:
    """A structured failure from one optional read source."""

    source: str
    error_type: str
    message: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source": self.source,
            "type": self.error_type,
            "message": self.message,
        }


@dataclass(frozen=True)
class SourceEnvelope:
    """One independently collected remote source."""

    source: str
    data: JsonValue = None
    error: SourceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "ok": self.ok,
            "data": _json_value(self.data, path=f"source '{self.source}'"),
            "error": None if self.error is None else self.error.to_dict(),
        }


@dataclass(frozen=True)
class CorruptArtifact:
    """A projection or journal that the lifecycle could not fully decode."""

    path: str
    kind: str
    issue: int | None = None
    phase: str | None = None
    attempt: int | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "kind": self.kind,
            "issue": self.issue,
            "phase": self.phase,
            "attempt": self.attempt,
        }


@dataclass(frozen=True)
class RunReport:
    """A deterministic local read model with optional remote source results."""

    issue: int | None
    current: tuple[RunRecord, ...] = ()
    history: tuple[RunRecord, ...] = ()
    orphans: tuple[RunRecord, ...] = ()
    corrupt: tuple[CorruptArtifact, ...] = ()
    sources: tuple[SourceEnvelope, ...] = ()

    @property
    def source_errors(self) -> tuple[SourceError, ...]:
        return tuple(
            source.error for source in self.sources if source.error is not None
        )

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a value accepted by strict ``json.dumps``."""
        source_payload: dict[str, JsonValue] = {
            source.source: source.to_dict() for source in self.sources
        }
        return {
            "schema_version": _READ_MODEL_SCHEMA_VERSION,
            "issue": self.issue,
            "current": [_run_record_dict(record) for record in self.current],
            "history": [_run_record_dict(record) for record in self.history],
            "orphans": [_run_record_dict(record) for record in self.orphans],
            "corrupt": [artifact.to_dict() for artifact in self.corrupt],
            "sources": source_payload,
            "source_errors": [error.to_dict() for error in self.source_errors],
        }


@dataclass(frozen=True)
class RunDisposition:
    """Canonical operator-facing meaning of a Task Run projection."""

    display: str
    owner: str
    severity: str
    dispatchable: bool
    active: bool | None
    next_action: str | None


def describe_run(
    record: RunRecord,
    *,
    claim_held: bool | None = None,
) -> RunDisposition:
    """Map persistence vocabulary to one stable operator-facing state."""
    phase = record.phase.value
    issue = record.issue
    if record.status is RunStatus.RUNNING:
        if claim_held is False:
            return RunDisposition(
                f"{phase} interrupted",
                "operator",
                "error",
                False,
                False,
                f"machinist retry {issue} --phase {phase}",
            )
        return RunDisposition(
            f"{phase} running",
            "machinist",
            "info",
            False,
            claim_held,
            f"machinist cancel {issue}",
        )
    if record.status is RunStatus.RETRYABLE:
        return RunDisposition(
            f"{phase} retryable",
            "watcher",
            "info",
            True,
            False,
            "machinist watch --once -v",
        )
    if record.status in {
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.ABANDONED,
    }:
        return RunDisposition(
            f"{phase} {record.status.value}",
            "operator",
            "error" if record.status is RunStatus.FAILED else "warning",
            False,
            False,
            f"machinist retry {issue} --phase {phase}",
        )
    next_action = (
        f"machinist approve --issue {issue}" if record.phase is Phase.SPEC else None
    )
    return RunDisposition(
        f"{phase} succeeded",
        "operator",
        "success",
        False,
        False,
        next_action,
    )


def capture_source(source: str, loader: RemoteLoader) -> SourceEnvelope:
    """Collect one JSON source without leaking its failure into other reads."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source name must be a non-empty string")

    try:
        data = _json_value(loader(), path=f"source '{source}'")
    except Exception as exc:  # noqa: BLE001 - isolation is this helper's contract
        message = str(exc).strip() or type(exc).__name__
        return SourceEnvelope(
            source=source,
            error=SourceError(
                source=source,
                error_type=type(exc).__name__,
                message=message,
            ),
        )
    return SourceEnvelope(source=source, data=data)


def build_run_report(
    lifecycle: TaskLifecycle,
    issue: int | None = None,
    *,
    remote_sources: Mapping[str, RemoteLoader] | None = None,
) -> RunReport:
    """Build the Task Run read model without mutating lifecycle state.

    An orphan is a valid journal-backed attempt without an exact current
    projection.  That includes expected superseded attempts as well as an
    attempt whose projection was removed or could not be decoded; orphan does
    not itself mean the journal is corrupt.
    """
    if issue is not None and (
        isinstance(issue, bool) or not isinstance(issue, int) or issue < 1
    ):
        raise ValueError("issue scope must be a positive integer")

    inventory = lifecycle.inventory()
    current = tuple(
        sorted(
            (
                record
                for record in inventory.records
                if issue is None or record.issue == issue
            ),
            key=_run_sort_key,
        )
    )
    history = tuple(
        record
        for record in inventory.attempts
        if issue is None or record.issue == issue
    )
    orphans = tuple(
        record for record in inventory.orphans if issue is None or record.issue == issue
    )

    corrupt = tuple(
        sorted(
            (
                CorruptArtifact(
                    path=str(artifact.path),
                    kind=artifact.kind,
                    issue=artifact.issue,
                    phase=None if artifact.phase is None else artifact.phase.value,
                    attempt=artifact.attempt,
                )
                for artifact in inventory.artifacts
                if issue is None or artifact.issue == issue
            ),
            key=lambda artifact: artifact.path,
        )
    )

    loaders = remote_sources or {}
    if any(not isinstance(name, str) for name in loaders):
        raise ValueError("remote source names must be strings")
    sources = tuple(capture_source(name, loaders[name]) for name in sorted(loaders))

    return RunReport(
        issue=issue,
        current=current,
        history=history,
        orphans=orphans,
        corrupt=corrupt,
        sources=sources,
    )


def summarize_run_report(
    report: RunReport,
    *,
    lifecycle: TaskLifecycle | None = None,
) -> tuple[str, ...]:
    """Render a compact text summary suitable for ``inspect`` or ``status``."""
    scope = f"Issue #{report.issue}" if report.issue is not None else "All issues"
    lines = [
        (
            f"{scope}: {_count(len(report.current), 'current projection')}, "
            f"{_count(len(report.history), 'recorded attempt')}."
        )
    ]

    for record in report.current:
        held = lifecycle.claim_held(record.issue) if lifecycle is not None else None
        disposition = describe_run(record, claim_held=held)
        duration_seconds = _display_duration(record)
        duration = (
            ""
            if duration_seconds is None
            else f", {_format_duration(duration_seconds)}"
        )
        detail = (
            f"  #{record.issue} {disposition.display} "
            f"(attempt {record.attempt}{duration})"
        )
        stage = TaskEvidence.load(record.evidence).current_stage
        if stage:
            detail += f" — stage: {stage}"
        if record.error:
            detail += f": {record.error}"
        lines.append(detail)
        if disposition.next_action is not None:
            lines.append(f"    Next: {disposition.next_action}")

    if report.history:
        lines.append("Attempts:")
        current_keys = {
            (record.issue, record.phase, record.attempt) for record in report.current
        }
        for record in report.history:
            marker = (
                "current"
                if (record.issue, record.phase, record.attempt) in current_keys
                else "history"
            )
            lines.append(
                f"  #{record.issue} {record.phase.value} attempt {record.attempt}: "
                f"{record.status.value} ({marker}, updated {record.updated_at})"
            )

    if report.orphans:
        lines.append(_count(len(report.orphans), "orphaned history attempt"))
    if report.corrupt:
        lines.append(_count(len(report.corrupt), "corrupt Task Run artifact"))
    for error in report.source_errors:
        lines.append(f"{error.source} unavailable: {error.error_type}: {error.message}")
    return tuple(lines)


def _display_duration(record: RunRecord) -> float | None:
    if record.duration_seconds is not None:
        return record.duration_seconds
    if record.status is not RunStatus.RUNNING:
        return None
    try:
        started = datetime.fromisoformat(record.started_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - started).total_seconds())


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remainder = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {remainder:02d}s"
    return f"{remainder}s"


def _run_record_dict(record: RunRecord) -> dict[str, JsonValue]:
    return {
        "issue": record.issue,
        "phase": record.phase.value,
        "status": record.status.value,
        "attempt": record.attempt,
        "started_at": record.started_at,
        "updated_at": record.updated_at,
        "ended_at": record.ended_at,
        "duration_seconds": record.duration_seconds,
        "error": record.error,
        "evidence": _json_value(record.evidence, path="Task Run evidence"),
    }


def _run_sort_key(record: RunRecord) -> tuple[int, str, int, str]:
    return record.issue, record.phase.value, record.attempt, record.started_at


def _count(value: int, noun: str) -> str:
    suffix = "" if value == 1 else "s"
    return f"{value} {noun}{suffix}"


def _json_value(value: object, *, path: str) -> JsonValue:
    """Validate and clone a value into the strict JSON type union."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise TypeError(f"{path} contains a non-finite number")
    if isinstance(value, list):
        return [
            _json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            result[key] = _json_value(item, path=f"{path}.{key}")
        return result
    raise TypeError(f"{path} contains unsupported type {type(value).__name__}")
