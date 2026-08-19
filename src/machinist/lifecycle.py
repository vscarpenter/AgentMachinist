"""Durable local Task Run records, attempt history, and local Claims.

The projection files preserve the original ``issue-<n>-<phase>.json`` interface
for existing callers. A versioned JSONL journal additionally preserves every
attempt and checkpoint without rewriting earlier attempts.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, TypeVar, cast

_SCHEMA_VERSION = 1
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "abandoned"})

type EvidenceValue = (
    str | int | float | bool | None | list[EvidenceValue] | dict[str, EvidenceValue]
)
type Evidence = dict[str, EvidenceValue]


class LifecycleError(Exception):
    """A Task cannot enter the requested lifecycle transition."""


class Phase(str, Enum):
    SPEC = "spec"
    EXECUTE = "execute"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE = "retryable"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class RunRecord:
    issue: int
    phase: Phase
    status: RunStatus
    attempt: int
    started_at: str
    updated_at: str
    error: str | None = None
    evidence: Evidence = field(default_factory=dict)
    ended_at: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class RunInventory:
    """Valid current projections and artifacts that could not be decoded."""

    records: tuple[RunRecord, ...] = ()
    corrupt: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _JournalEvent:
    event: str
    record: RunRecord


_T = TypeVar("_T")
_HELD_ISSUES: set[int] = set()
_HELD_LOCK = Lock()


class TaskClaim:
    """The narrow capability given to a running Phase."""

    def __init__(self, lifecycle: TaskLifecycle, record: RunRecord, previous: Evidence):
        self._lifecycle = lifecycle
        self._record = record
        self.previous_evidence = dict(previous)

    @property
    def issue(self) -> int:
        return self._record.issue

    @property
    def phase(self) -> Phase:
        return self._record.phase

    @property
    def attempt(self) -> int:
        return self._record.attempt

    def log_path(self, name: str) -> Path:
        """Return a contained per-attempt path for controller-owned logs."""
        return self._lifecycle.log_path(self.issue, self.phase, self.attempt, name)

    def checkpoint(self, **evidence: EvidenceValue) -> None:
        """Atomically project and append Evidence needed for reconciliation."""
        merged = _validate_evidence({**self._record.evidence, **evidence})
        updated = self._lifecycle._replace(self._record, evidence=merged)
        self._lifecycle._persist(updated, event="checkpointed")
        self._record = updated


class TaskLifecycle:
    """Own Task Run persistence, attempt history, transitions, and local Claims."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)

    def run(
        self,
        issue: int,
        phase: Phase,
        action: Callable[[TaskClaim], _T],
        *,
        repeat_succeeded: bool = False,
    ) -> _T:
        with self._hold_claim(issue):
            prior = self.record(issue, phase)
            if prior is None:
                prior_history = self.history(issue, phase)
                prior = prior_history[-1] if prior_history else None

            repeat = (
                prior is not None
                and prior.status in {RunStatus.SUCCEEDED, RunStatus.ABANDONED}
                and repeat_succeeded
            )
            if (
                prior is not None
                and prior.status is not RunStatus.RETRYABLE
                and not repeat
            ):
                if prior.status in {
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                    RunStatus.ABANDONED,
                }:
                    raise LifecycleError(
                        f"issue #{issue} {phase.value} previously {prior.status.value}; "
                        f"run 'machinist retry {issue}'"
                    )
                raise LifecycleError(
                    f"issue #{issue} {phase.value} is {prior.status.value}; refusing a duplicate run"
                )

            now = _now()
            previous = {} if prior is None else dict(prior.evidence)
            attempt = (
                max(
                    0 if prior is None else prior.attempt,
                    self._max_journal_attempt(issue, phase),
                )
                + 1
            )
            running = RunRecord(
                issue=issue,
                phase=phase,
                status=RunStatus.RUNNING,
                attempt=attempt,
                started_at=now,
                updated_at=now,
                evidence=previous,
            )
            self._persist(running, event="started")
            claim = TaskClaim(self, running, previous)

            try:
                result = action(claim)
                evidence = dict(claim._record.evidence)
                for attr, key in (("number", "pr_number"), ("url", "pr_url")):
                    value = getattr(result, attr, None)
                    if value is not None:
                        evidence[key] = value
                evidence = _validate_evidence(evidence)
            except Exception as exc:
                status = (
                    RunStatus.CANCELLED
                    if getattr(exc, "cancelled", False)
                    else RunStatus.FAILED
                )
                failed = self._finish(
                    claim._record,
                    status=status,
                    error=_exception_text(exc),
                )
                self._persist_without_masking(
                    failed,
                    event=status.value,
                    original=exc,
                )
                raise
            except BaseException as exc:
                status = (
                    RunStatus.CANCELLED
                    if isinstance(exc, KeyboardInterrupt)
                    else RunStatus.ABANDONED
                )
                interrupted = self._finish(
                    claim._record,
                    status=status,
                    error=_exception_text(exc),
                )
                self._persist_without_masking(
                    interrupted,
                    event=status.value,
                    original=exc,
                )
                raise

            succeeded = self._finish(
                claim._record,
                status=RunStatus.SUCCEEDED,
                error=None,
                evidence=evidence,
            )
            self._persist(succeeded, event="succeeded")
            return result

    def retry(self, issue: int, phase: Phase | None = None) -> RunRecord:
        with self._hold_claim(issue, retrying=True):
            record = (
                self.record(issue, phase) if phase is not None else self.latest(issue)
            )
            if record is None:
                historical = self.history(issue, phase)
                record = max(historical, key=lambda item: item.updated_at, default=None)
            if record is None:
                raise LifecycleError(f"no Task Run exists for issue #{issue}")
            if record.status not in {
                RunStatus.FAILED,
                RunStatus.RUNNING,
                RunStatus.CANCELLED,
                RunStatus.ABANDONED,
            }:
                raise LifecycleError(
                    f"issue #{issue} {record.phase.value} is {record.status.value}, "
                    "not failed or abandoned"
                )

            self._ensure_journal(record)
            if record.status is RunStatus.RUNNING:
                record = self._finish(
                    record,
                    status=RunStatus.ABANDONED,
                    error="previous process ended without completing the Task Run",
                )
                self._persist(record, event="abandoned")

            retryable = self._replace(record, status=RunStatus.RETRYABLE, error=None)
            self._persist(retryable, event="retry_requested")
            return retryable

    def abandon(self, issue: int, phase: Phase, reason: str) -> RunRecord:
        """Explicitly abandon a current or completed Phase attempt."""
        reason = reason.strip()
        if not reason:
            raise LifecycleError("an abandonment reason is required")

        with self._hold_claim(issue):
            record = self.record(issue, phase)
            if record is None:
                historical = self.history(issue, phase)
                record = historical[-1] if historical else None
            if record is None:
                raise LifecycleError(
                    f"no Task Run exists for issue #{issue} {phase.value}"
                )
            if record.status is RunStatus.ABANDONED and record.error == reason:
                return record

            self._ensure_journal(record)
            if record.status is RunStatus.RUNNING:
                abandoned = self._finish(
                    record,
                    status=RunStatus.ABANDONED,
                    error=reason,
                )
            else:
                abandoned = self._replace(
                    record,
                    status=RunStatus.ABANDONED,
                    error=reason,
                )
            self._persist(abandoned, event="abandoned")
            return abandoned

    def record(self, issue: int, phase: Phase | None = None) -> RunRecord | None:
        """Return the compatible current projection for a Task Phase."""
        if phase is None:
            return self.latest(issue)
        path = self._path(issue, phase)
        if not path.exists():
            return None
        return self._read_projection(path)

    def latest(self, issue: int) -> RunRecord | None:
        records = [
            record
            for phase in Phase
            if (record := self.record(issue, phase)) is not None
        ]
        return max(records, key=lambda item: item.updated_at, default=None)

    def history(self, issue: int, phase: Phase | None = None) -> list[RunRecord]:
        """Return one immutable outcome per attempt, oldest first.

        Retry requests intentionally do not replace the failed, cancelled, or
        abandoned outcome of the attempt they make eligible for another run.
        Malformed journal lines are ignored here so earlier valid append-only
        events remain useful; :meth:`inventory` reports the artifact as corrupt.
        """
        attempts: dict[tuple[Phase, int], RunRecord] = {}
        phases = (phase,) if phase is not None else tuple(Phase)

        for current_phase in phases:
            for path in self._journal_paths(issue, current_phase):
                events, _ = self._read_journal(path)
                if not events:
                    continue
                terminal = [
                    event
                    for event in events
                    if event.record.status.value in _TERMINAL_STATUSES
                ]
                chosen = terminal[-1] if terminal else events[-1]
                attempts[(chosen.record.phase, chosen.record.attempt)] = chosen.record

            try:
                projection = self.record(issue, current_phase)
            except LifecycleError:
                projection = None
            if projection is not None:
                attempts.setdefault((projection.phase, projection.attempt), projection)

        return sorted(
            attempts.values(),
            key=lambda item: (item.started_at, item.phase.value, item.attempt),
        )

    def inventory(self) -> RunInventory:
        """Enumerate valid current projections and report corrupt artifacts."""
        records: list[RunRecord] = []
        corrupt: list[Path] = []
        if not self.runs_dir.exists():
            return RunInventory()

        for path in sorted(self.runs_dir.glob("issue-*-*.json")):
            if not path.is_file():
                continue
            try:
                records.append(self._read_projection(path))
            except LifecycleError:
                corrupt.append(path)

        history_root = self.runs_dir / "history"
        if history_root.exists():
            for path in sorted(history_root.glob("issue-*-*/attempt-*.jsonl")):
                _, malformed = self._read_journal(path)
                if malformed:
                    corrupt.append(path)

        records.sort(key=lambda item: (item.issue, item.phase.value, item.attempt))
        return RunInventory(tuple(records), tuple(sorted(set(corrupt))))

    def claim_held(self, issue: int) -> bool:
        """Check a local Claim without waiting or creating a lock artifact."""
        with _HELD_LOCK:
            if issue in _HELD_ISSUES:
                return True

        lock_path = self.runs_dir / f"issue-{issue}.lock"
        if not lock_path.exists():
            return False
        try:
            lock_file = lock_path.open("a+")
        except FileNotFoundError:
            return False
        except OSError:
            return True

        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                return True
        return False

    def log_path(self, issue: int, phase: Phase, attempt: int, name: str) -> Path:
        """Create and return a contained per-attempt log path."""
        if issue < 1 or attempt < 1:
            raise LifecycleError("log paths require positive issue and attempt numbers")
        if (
            not name
            or len(name) > 128
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or "\x00" in name
            or any(
                not (character.isalnum() or character in "._-") for character in name
            )
        ):
            raise LifecycleError(
                "log name must be a safe filename using letters, digits, '.', '_', or '-'"
            )

        logs_root = (self.runs_dir / "logs").resolve()
        parent = (
            logs_root / f"issue-{issue}" / phase.value / f"attempt-{attempt}"
        ).resolve()
        if logs_root != parent and logs_root not in parent.parents:
            raise LifecycleError("resolved log path escapes the Task Run log root")
        parent.mkdir(parents=True, exist_ok=True)
        path = (parent / name).resolve()
        if path.parent != parent:
            raise LifecycleError("resolved log path escapes its Task Run attempt")
        return path

    @contextmanager
    def _hold_claim(self, issue: int, *, retrying: bool = False) -> Iterator[None]:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        with _HELD_LOCK:
            if issue in _HELD_ISSUES:
                state = "still claimed" if retrying else "already claimed"
                raise LifecycleError(f"issue #{issue} is {state} by this process")
            _HELD_ISSUES.add(issue)

        lock_file = None
        locked = False
        try:
            lock_file = (self.runs_dir / f"issue-{issue}.lock").open("a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError as exc:
                if retrying:
                    message = (
                        f"issue #{issue} is still claimed by another worker; "
                        "stop it before retrying"
                    )
                else:
                    message = f"issue #{issue} is already claimed by another worker"
                raise LifecycleError(message) from exc
            yield
        finally:
            if lock_file is not None:
                try:
                    if locked:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()
            with _HELD_LOCK:
                _HELD_ISSUES.discard(issue)

    def _finish(
        self,
        record: RunRecord,
        *,
        status: RunStatus,
        error: str | None,
        evidence: Evidence | None = None,
    ) -> RunRecord:
        ended_at = _now()
        return self._replace(
            record,
            status=status,
            error=error,
            evidence=record.evidence if evidence is None else evidence,
            ended_at=ended_at,
            duration_seconds=_duration(record.started_at, ended_at),
            updated_at=ended_at,
        )

    def _replace(self, record: RunRecord, **changes: Any) -> RunRecord:
        data = asdict(record)
        data.update(changes)
        if "updated_at" not in changes:
            data["updated_at"] = _now()
        data["phase"] = Phase(data["phase"])
        data["status"] = RunStatus(data["status"])
        data["evidence"] = _validate_evidence(data.get("evidence", {}))
        return RunRecord(**data)

    def _path(self, issue: int, phase: Phase) -> Path:
        return self.runs_dir / f"issue-{issue}-{phase.value}.json"

    def _journal_directory(self, issue: int, phase: Phase) -> Path:
        return self.runs_dir / "history" / f"issue-{issue}-{phase.value}"

    def _journal_path(self, record: RunRecord) -> Path:
        return self._journal_directory(record.issue, record.phase) / (
            f"attempt-{record.attempt:06d}.jsonl"
        )

    def _journal_paths(self, issue: int, phase: Phase) -> list[Path]:
        directory = self._journal_directory(issue, phase)
        if not directory.exists():
            return []
        return sorted(directory.glob("attempt-*.jsonl"))

    def _max_journal_attempt(self, issue: int, phase: Phase) -> int:
        attempts: list[int] = []
        for path in self._journal_paths(issue, phase):
            stem = path.stem
            if stem.startswith("attempt-") and stem[8:].isdigit():
                attempts.append(int(stem[8:]))
        return max(attempts, default=0)

    def _ensure_journal(self, record: RunRecord) -> None:
        if not self._journal_path(record).exists():
            self._append_event(record, event="legacy_snapshot")

    def _persist(self, record: RunRecord, *, event: str) -> None:
        _validate_evidence(record.evidence)
        if event != "started":
            self._ensure_journal(record)
        self._append_event(record, event=event)
        self._write_projection(record)

    def _persist_without_masking(
        self,
        record: RunRecord,
        *,
        event: str,
        original: BaseException,
    ) -> None:
        try:
            self._persist(record, event=event)
        except (LifecycleError, OSError, TypeError, ValueError) as exc:
            original.add_note(f"could not persist terminal Task Run state: {exc}")

    def _append_event(self, record: RunRecord, *, event: str) -> None:
        path = self._journal_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "event": event,
            "timestamp": record.updated_at,
            "record": _record_payload(record),
        }
        encoded = (
            json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode()
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise LifecycleError(
                    f"could not append complete Task Run event to {path}"
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_projection(self, record: RunRecord) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        payload = _record_payload(record)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".run-", dir=self.runs_dir, text=True
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(payload, stream, allow_nan=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(record.issue, record.phase))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read_projection(self, path: Path) -> RunRecord:
        try:
            payload = json.loads(path.read_text())
            return _record_from_payload(payload, source=path)
        except LifecycleError:
            raise
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise LifecycleError(f"Task Run record {path} is corrupt: {exc}") from exc

    def _read_journal(self, path: Path) -> tuple[list[_JournalEvent], bool]:
        events: list[_JournalEvent] = []
        malformed = False
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return events, True

        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if payload.get("schema_version") != _SCHEMA_VERSION:
                    raise LifecycleError("unsupported journal schema version")
                event = payload["event"]
                if not isinstance(event, str) or not event:
                    raise LifecycleError("event must be a non-empty string")
                record = _record_from_payload(
                    payload["record"], source=f"{path}:{line_number}"
                )
                events.append(_JournalEvent(event=event, record=record))
            except (LifecycleError, json.JSONDecodeError, TypeError, KeyError):
                malformed = True
        return events, malformed


def _record_payload(record: RunRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["schema_version"] = _SCHEMA_VERSION
    payload["phase"] = record.phase.value
    payload["status"] = record.status.value
    return payload


def _record_from_payload(payload: Any, *, source: object) -> RunRecord:
    if not isinstance(payload, dict):
        raise LifecycleError(f"Task Run record {source} is corrupt: expected an object")
    version = payload.get("schema_version", 0)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 0 <= version <= _SCHEMA_VERSION
    ):
        raise LifecycleError(
            f"Task Run record {source} is corrupt: unsupported schema version {version!r}"
        )

    try:
        issue = payload["issue"]
        attempt = payload["attempt"]
        phase = Phase(payload["phase"])
        status = RunStatus(payload["status"])
        started_at = payload["started_at"]
        updated_at = payload["updated_at"]
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleError(f"Task Run record {source} is corrupt: {exc}") from exc

    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise LifecycleError(f"Task Run record {source} is corrupt: invalid issue")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise LifecycleError(f"Task Run record {source} is corrupt: invalid attempt")
    if not isinstance(started_at, str) or not isinstance(updated_at, str):
        raise LifecycleError(f"Task Run record {source} is corrupt: invalid timestamps")

    error = payload.get("error")
    if error is not None and not isinstance(error, str):
        raise LifecycleError(f"Task Run record {source} is corrupt: invalid error")
    ended_at = payload.get("ended_at")
    if ended_at is not None and not isinstance(ended_at, str):
        raise LifecycleError(
            f"Task Run record {source} is corrupt: invalid end timestamp"
        )
    duration = payload.get("duration_seconds")
    if duration is not None:
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise LifecycleError(
                f"Task Run record {source} is corrupt: invalid duration"
            )
        duration = float(duration)

    return RunRecord(
        issue=issue,
        phase=phase,
        status=status,
        attempt=attempt,
        started_at=started_at,
        updated_at=updated_at,
        error=error,
        evidence=_validate_evidence(payload.get("evidence", {})),
        ended_at=ended_at,
        duration_seconds=duration,
    )


def _validate_evidence(value: object) -> Evidence:
    if not isinstance(value, dict):
        raise LifecycleError("Task Run evidence must be an object")
    for key, item in value.items():
        if not isinstance(key, str):
            raise LifecycleError("Task Run evidence keys must be strings")
        _validate_evidence_value(item, path=key)
    return cast(Evidence, dict(value))


def _validate_evidence_value(value: object, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise LifecycleError(f"Task Run evidence '{path}' must be finite")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_evidence_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LifecycleError(f"Task Run evidence '{path}' keys must be strings")
            _validate_evidence_value(item, path=f"{path}.{key}")
        return
    raise LifecycleError(
        f"Task Run evidence '{path}' has unsupported type {type(value).__name__}"
    )


def _duration(started_at: str, ended_at: str) -> float | None:
    try:
        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=UTC)
        return round(max(0.0, (ended - started).total_seconds()), 6)
    except ValueError:
        return None


def _exception_text(exc: BaseException) -> str:
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted by user"
    return str(exc).strip() or type(exc).__name__


def _now() -> str:
    return datetime.now(UTC).isoformat()
