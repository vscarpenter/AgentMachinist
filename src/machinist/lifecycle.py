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
import signal
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from types import FrameType
from typing import Any, TypeVar, cast

from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    append_text_file,
    atomic_write_text_file,
    list_directory_names,
    open_regular_file,
    read_text_file,
    regular_file_exists,
    reserve_regular_file,
)

_SCHEMA_VERSION = 1
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "abandoned"})
_MAX_PROJECTION_BYTES = 8 * 1024 * 1024
_MAX_JOURNAL_BYTES = 32 * 1024 * 1024

type EvidenceValue = (
    str | int | float | bool | None | list[EvidenceValue] | dict[str, EvidenceValue]
)
type Evidence = dict[str, EvidenceValue]


class LifecycleError(Exception):
    """A Task cannot enter the requested lifecycle transition."""


class LifecycleFinalizationError(LifecycleError):
    """A Phase action finished, but its terminal Evidence was not finalized."""

    action_succeeded = True


class LifecycleSignalInterruption(SystemExit):
    """A service termination signal received while a Task Claim was active."""

    cancelled = True

    def __init__(self, signal_number: int):
        self.signal_number = signal_number
        self.signal_name = signal.Signals(signal_number).name
        super().__init__(128 + signal_number)

    def __str__(self) -> str:
        return f"Task Run interrupted by {self.signal_name}"


class _ScopedLifecycleSignals:
    """Make TERM/HUP durable for a claimed Task without making them catchable."""

    def __init__(self) -> None:
        self._received: int | None = None
        self._active = False
        self._persisting_terminal = False
        self._prior_handlers: dict[signal.Signals, Any] = {}

    def __enter__(self) -> _ScopedLifecycleSignals:
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
        ):
            return self
        for current_signal in (signal.SIGTERM, signal.SIGHUP):
            self._prior_handlers[current_signal] = signal.getsignal(current_signal)
            signal.signal(current_signal, self._handle)
        return self

    def __exit__(self, *_exc: object) -> None:
        for current_signal, prior_handler in self._prior_handlers.items():
            signal.signal(current_signal, prior_handler)

    def activate(self) -> None:
        self._active = True
        self.raise_if_received()

    def begin_terminal_persistence(self) -> None:
        # Repeated service signals remain pending but cannot interrupt the
        # fsync/replace operations that make the terminal projection durable.
        self._persisting_terminal = True

    def pending(self) -> LifecycleSignalInterruption | None:
        if self._received is None:
            return None
        return LifecycleSignalInterruption(self._received)

    def resume_after_terminal_persistence(self) -> None:
        self._persisting_terminal = False
        self.raise_if_received()

    def raise_if_received(self) -> None:
        pending = self.pending()
        if pending is not None and not self._persisting_terminal:
            raise pending

    def _handle(self, signal_number: int, _frame: FrameType | None) -> None:
        if self._received is None:
            self._received = signal_number
        if self._active and not self._persisting_terminal:
            raise LifecycleSignalInterruption(self._received)


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

    def log_directory(self, name: str) -> Path:
        """Return a contained per-attempt directory for a related log set."""
        return self._lifecycle.log_directory(self.issue, self.phase, self.attempt, name)

    def checkpoint(self, **evidence: EvidenceValue) -> None:
        """Atomically project and append Evidence needed for reconciliation."""
        merged = _validate_evidence({**self._record.evidence, **evidence})
        updated = self._lifecycle._replace(self._record, evidence=merged)
        self._lifecycle._persist(updated, event="checkpointed")
        self._record = updated


class TaskLifecycle:
    """Own Task Run persistence, attempt history, transitions, and local Claims."""

    def __init__(
        self,
        runs_dir: Path,
        *,
        repo_root: str | Path | None = None,
    ):
        try:
            self._runtime = RuntimeDirectory.bind(runs_dir, repo_root=repo_root)
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run path: {exc}") from exc
        self.runs_dir = self._runtime.path

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
            with _ScopedLifecycleSignals() as signal_scope:
                # Before activation, TERM/HUP is recorded but not raised.  This
                # lets the initial RUNNING event become durable before the same
                # signal is converted into a terminal cancellation.
                self._persist(running, event="started")
                claim = TaskClaim(self, running, previous)

                try:
                    signal_scope.activate()
                    result = action(claim)
                except BaseException as exc:
                    signal_scope.begin_terminal_persistence()
                    status = _interrupted_status(exc)
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

                    # If TERM/HUP raced persistence of an unrelated failure,
                    # make cancellation the final projection and preserve the
                    # service-level exit rather than letting a watcher continue.
                    pending = signal_scope.pending()
                    if pending is not None:
                        if not getattr(exc, "signal_number", None):
                            cancelled = self._finish(
                                claim._record,
                                status=RunStatus.CANCELLED,
                                error=_exception_text(pending),
                            )
                            self._persist_without_masking(
                                cancelled,
                                event=RunStatus.CANCELLED.value,
                                original=pending,
                            )
                        raise pending from exc
                    signal_scope.resume_after_terminal_persistence()
                    raise

                # Once the Phase action returns, delivery may already be
                # externally visible. Keep terminal projection failures out of
                # the action-failure path so history can never claim that a
                # successfully delivered PR subsequently failed.
                signal_scope.begin_terminal_persistence()
                try:
                    evidence = dict(claim._record.evidence)
                    for attr, key in (("number", "pr_number"), ("url", "pr_url")):
                        value = getattr(result, attr, None)
                        if value is not None:
                            evidence[key] = value
                    evidence = _validate_evidence(evidence)
                    succeeded = self._finish(
                        claim._record,
                        status=RunStatus.SUCCEEDED,
                        error=None,
                        evidence=evidence,
                    )
                    self._persist(succeeded, event="succeeded")
                except (LifecycleError, OSError, TypeError, ValueError) as exc:
                    raise LifecycleFinalizationError(
                        f"issue #{issue} {phase.value} action succeeded, but terminal "
                        "Task Run Evidence could not be finalized; inspect the PR and "
                        f"run 'machinist inspect {issue}' before reconciling: {exc}"
                    ) from exc

                # A service signal received during durable success persistence
                # still exits the worker, but must not rewrite delivered work
                # as cancelled or failed.
                signal_scope.resume_after_terminal_persistence()
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
        self._ensure_runs(create=False)
        if phase is None:
            return self.latest(issue)
        path = self._path(issue, phase)
        try:
            if not regular_file_exists(path):
                return None
        except (OSError, RuntimePathError) as exc:
            raise LifecycleError(
                f"cannot inspect Task Run record {path}: {exc}"
            ) from exc
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
        self._ensure_runs(create=False)
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
        self._ensure_runs(create=False)
        records: list[RunRecord] = []
        corrupt: list[Path] = []
        try:
            run_names = list_directory_names(self.runs_dir)
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run inventory path: {exc}") from exc
        for name in run_names:
            if not _projection_filename(name):
                continue
            path = self.runs_dir / name
            try:
                records.append(self._read_projection(path))
            except LifecycleError:
                corrupt.append(path)

        history_root = self.history_root()
        try:
            history_names = list_directory_names(history_root)
            for directory_name in history_names:
                directory = history_root / directory_name
                if not _history_directory_name(directory_name):
                    corrupt.append(directory)
                    continue
                for filename in list_directory_names(directory):
                    path = directory / filename
                    if not _journal_filename(filename):
                        corrupt.append(path)
                        continue
                    _, malformed = self._read_journal(path)
                    if malformed:
                        corrupt.append(path)
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run history inventory: {exc}") from exc

        records.sort(key=lambda item: (item.issue, item.phase.value, item.attempt))
        return RunInventory(tuple(records), tuple(sorted(set(corrupt))))

    def history_root(self) -> Path:
        """Return the validated, non-creating append-only history directory."""
        try:
            return self._runtime.subdirectory("history", create=False)
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run history path: {exc}") from exc

    def claim_held(self, issue: int) -> bool:
        """Check a local Claim without waiting or creating a lock artifact."""
        self._ensure_runs(create=False)
        with _HELD_LOCK:
            if issue in _HELD_ISSUES:
                return True

        lock_path = self.runs_dir / f"issue-{issue}.lock"
        try:
            if not regular_file_exists(lock_path):
                return False
        except (OSError, RuntimePathError):
            return True
        try:
            descriptor = open_regular_file(lock_path, truncate=False)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                return True
            lock_file = os.fdopen(descriptor, "a+")
        except (OSError, RuntimePathError):
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
        _validate_log_name(name)

        try:
            parent = self._runtime.subdirectory(
                "logs",
                f"issue-{issue}",
                phase.value,
                f"attempt-{attempt}",
                create=True,
            )
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run log path: {exc}") from exc
        path = parent / name
        try:
            return reserve_regular_file(path)
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run log file: {exc}") from exc

    def log_directory(self, issue: int, phase: Phase, attempt: int, name: str) -> Path:
        """Create and return a contained per-attempt log directory."""
        if issue < 1 or attempt < 1:
            raise LifecycleError("log paths require positive issue and attempt numbers")
        _validate_log_name(name)
        try:
            return self._runtime.subdirectory(
                "logs",
                f"issue-{issue}",
                phase.value,
                f"attempt-{attempt}",
                name,
                create=True,
            )
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run log directory: {exc}") from exc

    @contextmanager
    def _hold_claim(self, issue: int, *, retrying: bool = False) -> Iterator[None]:
        self._ensure_runs(create=True)
        with _HELD_LOCK:
            if issue in _HELD_ISSUES:
                state = "still claimed" if retrying else "already claimed"
                raise LifecycleError(f"issue #{issue} is {state} by this process")
            _HELD_ISSUES.add(issue)

        lock_file = None
        locked = False
        try:
            lock_path = self.runs_dir / f"issue-{issue}.lock"
            try:
                descriptor = open_regular_file(
                    lock_path,
                    truncate=False,
                    mode=0o600,
                )
            except (OSError, RuntimePathError) as exc:
                raise LifecycleError(f"could not open Task Run claim: {exc}") from exc
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise LifecycleError("Task Run claim lock is not a regular file")
            lock_file = os.fdopen(descriptor, "a+")
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
        try:
            self.history_root()
            self._runtime.subdirectory(
                "history", f"issue-{issue}-{phase.value}", create=False
            )
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run history path: {exc}") from exc
        try:
            return [
                directory / name
                for name in list_directory_names(directory)
                if _journal_filename(name)
            ]
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run history path: {exc}") from exc

    def _max_journal_attempt(self, issue: int, phase: Phase) -> int:
        attempts: list[int] = []
        for path in self._journal_paths(issue, phase):
            stem = path.stem
            if stem.startswith("attempt-") and stem[8:].isdigit():
                attempts.append(int(stem[8:]))
        return max(attempts, default=0)

    def _ensure_journal(self, record: RunRecord) -> None:
        try:
            exists = regular_file_exists(self._journal_path(record))
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run journal path: {exc}") from exc
        if not exists:
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
        try:
            self._runtime.subdirectory(
                "history",
                f"issue-{record.issue}-{record.phase.value}",
                create=True,
            )
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run history path: {exc}") from exc
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "event": event,
            "timestamp": record.updated_at,
            "record": _record_payload(record),
        }
        serialized = (
            json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        )
        try:
            append_text_file(path, serialized)
        except (OSError, RuntimePathError) as exc:
            raise LifecycleError(
                f"could not append Task Run journal {path}: {exc}"
            ) from exc

    def _write_projection(self, record: RunRecord) -> None:
        self._ensure_runs(create=True)
        payload = _record_payload(record)
        try:
            serialized = json.dumps(
                payload,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            atomic_write_text_file(
                self._path(record.issue, record.phase),
                serialized + "\n",
            )
        except (OSError, RuntimePathError, TypeError, ValueError) as exc:
            raise LifecycleError(f"could not write Task Run projection: {exc}") from exc

    def _read_projection(self, path: Path) -> RunRecord:
        try:
            expected = _projection_identity(path.name)
            if expected is None:
                raise LifecycleError(
                    f"Task Run record {path} is corrupt: noncanonical filename"
                )
            payload = json.loads(read_text_file(path, max_bytes=_MAX_PROJECTION_BYTES))
            record = _record_from_payload(payload, source=path)
            if (record.issue, record.phase) != expected:
                raise LifecycleError(
                    f"Task Run record {path} is corrupt: payload identity "
                    f"#{record.issue} {record.phase.value} does not match filename"
                )
            return record
        except LifecycleError:
            raise
        except (
            OSError,
            RuntimePathError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            KeyError,
        ) as exc:
            raise LifecycleError(f"Task Run record {path} is corrupt: {exc}") from exc

    def _read_journal(self, path: Path) -> tuple[list[_JournalEvent], bool]:
        events: list[_JournalEvent] = []
        malformed = False
        expected_identity = _history_identity(path.parent.name)
        expected_attempt = _journal_attempt(path.name)
        if expected_identity is None or expected_attempt is None:
            return events, True
        try:
            lines = read_text_file(path, max_bytes=_MAX_JOURNAL_BYTES).splitlines()
        except (OSError, RuntimePathError, UnicodeError):
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
                if (record.issue, record.phase) != expected_identity:
                    raise LifecycleError(
                        "journal payload identity does not match its directory"
                    )
                if record.attempt != expected_attempt:
                    raise LifecycleError(
                        "journal payload attempt does not match its filename"
                    )
                events.append(_JournalEvent(event=event, record=record))
            except (LifecycleError, json.JSONDecodeError, TypeError, KeyError):
                malformed = True
        return events, malformed

    def _ensure_runs(self, *, create: bool) -> Path:
        try:
            return self._runtime.ensure(create=create)
        except RuntimePathError as exc:
            raise LifecycleError(f"unsafe Task Run path: {exc}") from exc


def _validate_log_name(name: str) -> None:
    if (
        not name
        or len(name) > 128
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or any(not (character.isalnum() or character in "._-") for character in name)
    ):
        raise LifecycleError(
            "log name must be a safe filename using letters, digits, '.', '_', or '-'"
        )


def _projection_filename(name: str) -> bool:
    return _projection_identity(name) is not None


def _projection_identity(name: str) -> tuple[int, Phase] | None:
    if not name.startswith("issue-"):
        return None
    for phase in Phase:
        suffix = f"-{phase.value}.json"
        if not name.endswith(suffix):
            continue
        issue = name[len("issue-") : -len(suffix)]
        if issue.isdigit() and int(issue) > 0:
            return int(issue), phase
    return None


def _history_directory_name(name: str) -> bool:
    return _history_identity(name) is not None


def _history_identity(name: str) -> tuple[int, Phase] | None:
    if not name.startswith("issue-"):
        return None
    for phase in Phase:
        suffix = f"-{phase.value}"
        if not name.endswith(suffix):
            continue
        issue = name[len("issue-") : -len(suffix)]
        if issue.isdigit() and int(issue) > 0:
            return int(issue), phase
    return None


def _journal_filename(name: str) -> bool:
    return _journal_attempt(name) is not None


def _journal_attempt(name: str) -> int | None:
    if not name.startswith("attempt-") or not name.endswith(".jsonl"):
        return None
    attempt = name[8:-6]
    return int(attempt) if attempt.isdigit() and int(attempt) > 0 else None


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


def _interrupted_status(exc: BaseException) -> RunStatus:
    if isinstance(exc, KeyboardInterrupt) or getattr(exc, "cancelled", False):
        return RunStatus.CANCELLED
    if isinstance(exc, Exception):
        return RunStatus.FAILED
    return RunStatus.ABANDONED


def _now() -> str:
    return datetime.now(UTC).isoformat()
