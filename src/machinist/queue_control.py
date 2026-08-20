"""Durable operator controls for repository-local queue admission.

State is stored atomically under the caller's ``.machinist/runs`` directory.
Healthy missing state means the queue is open; malformed or unreadable state
fails closed so a watcher cannot silently ignore an operator control.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    atomic_write_text_file,
    open_regular_file,
    read_text_file,
    regular_file_exists,
)

_SCHEMA_VERSION = 1
_STATE_FILENAME = "queue-control.json"
_LOCK_FILENAME = "queue-control.lock"
_MAX_REASON_CHARS = 1_000
_MAX_STATE_BYTES = 1024 * 1024

_LOCK_REGISTRY_GUARD = Lock()
_THREAD_LOCKS: dict[Path, Lock] = {}


class QueueControlError(Exception):
    """Queue control state cannot be safely read or changed."""


@dataclass(frozen=True)
class QueueAdmission:
    """A watch-compatible boolean decision with a human-readable reason."""

    allowed: bool
    issue_number: int
    reason: str | None = None
    kind: str | None = None

    def __bool__(self) -> bool:
        return self.allowed

    def as_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "allowed": self.allowed,
            "issue_number": self.issue_number,
            "reason": self.reason,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class _ControlEntry:
    reason: str
    timestamp: str


@dataclass(frozen=True)
class _QueueState:
    pause: _ControlEntry | None = None
    deferred: tuple[tuple[int, _ControlEntry], ...] = ()
    updated_at: str | None = None

    def deferred_dict(self) -> dict[int, _ControlEntry]:
        return dict(self.deferred)


class QueueControl:
    """Persist pause and per-issue deferral controls for one repository."""

    def __init__(
        self,
        runs_dir: str | Path,
        *,
        repo_root: str | Path | None = None,
    ):
        try:
            self._runtime = RuntimeDirectory.bind(runs_dir, repo_root=repo_root)
        except RuntimePathError as exc:
            raise QueueControlError(f"unsafe queue state path: {exc}") from exc
        self.runs_dir = self._runtime.path
        self.state_path = self.runs_dir / _STATE_FILENAME
        self.lock_path = self.runs_dir / _LOCK_FILENAME

    def admission(self, task_or_issue: object) -> QueueAdmission:
        """Return a decision for an issue number or issue-shaped task object.

        The result implements ``__bool__``, so this bound method can be passed
        directly as ``watch_once(..., admit=control.admission)``.
        """
        issue_number = _issue_number(task_or_issue)
        try:
            with self._locked(exclusive=False):
                state, _ = self._read_state()
        except QueueControlError:
            return QueueAdmission(
                False,
                issue_number,
                "queue control state is corrupt; dispatch denied",
                "corrupt",
            )
        except OSError:
            return QueueAdmission(
                False,
                issue_number,
                "queue control state is unavailable; dispatch denied",
                "unavailable",
            )

        if state.pause is not None:
            return QueueAdmission(
                False,
                issue_number,
                f"queue paused at {state.pause.timestamp}: {state.pause.reason}",
                "paused",
            )
        entry = state.deferred_dict().get(issue_number)
        if entry is not None:
            return QueueAdmission(
                False,
                issue_number,
                f"issue #{issue_number} deferred at {entry.timestamp}: {entry.reason}",
                "deferred",
            )
        return QueueAdmission(True, issue_number)

    def pause(
        self,
        reason: str | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Pause all new dispatches and return the resulting inspection view."""
        clean_reason = _reason("paused by operator" if reason is None else reason)
        timestamp = _timestamp(now)

        def update(state: _QueueState) -> _QueueState:
            return _QueueState(
                pause=_ControlEntry(clean_reason, timestamp),
                deferred=state.deferred,
                updated_at=timestamp,
            )

        return self._change(update)

    def resume(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Remove the global pause without changing issue deferrals."""
        timestamp = _timestamp(now)

        def update(state: _QueueState) -> _QueueState:
            if state.pause is None:
                return state
            return _QueueState(
                pause=None,
                deferred=state.deferred,
                updated_at=timestamp,
            )

        return self._change(update)

    def defer(
        self,
        issue_number: int,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Deny one issue until explicitly allowed again."""
        issue = _issue_number(issue_number)
        clean_reason = _reason(reason)
        timestamp = _timestamp(now)

        def update(state: _QueueState) -> _QueueState:
            deferred = state.deferred_dict()
            deferred[issue] = _ControlEntry(clean_reason, timestamp)
            return _QueueState(
                pause=state.pause,
                deferred=tuple(sorted(deferred.items())),
                updated_at=timestamp,
            )

        return self._change(update)

    def allow(
        self,
        issue_number: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Remove one issue deferral without changing the global pause."""
        issue = _issue_number(issue_number)
        timestamp = _timestamp(now)

        def update(state: _QueueState) -> _QueueState:
            deferred = state.deferred_dict()
            if issue not in deferred:
                return state
            deferred.pop(issue)
            return _QueueState(
                pause=state.pause,
                deferred=tuple(sorted(deferred.items())),
                updated_at=timestamp,
            )

        return self._change(update)

    def inspect(self) -> dict[str, Any]:
        """Return a JSON-safe view; corrupt state is represented, not raised."""
        try:
            with self._locked(exclusive=False):
                state, exists = self._read_state()
            return self._inspection(state, exists=exists)
        except (QueueControlError, OSError) as exc:
            return self._corrupt_inspection(exc)

    def _change(
        self,
        update: Callable[[_QueueState], _QueueState],
    ) -> dict[str, Any]:
        with self._locked(exclusive=True):
            state, exists = self._read_state()
            changed = update(state)
            if changed != state:
                self._write_state(changed)
                exists = True
            return self._inspection(changed, exists=exists)

    def _read_state(self) -> tuple[_QueueState, bool]:
        try:
            if not regular_file_exists(self.state_path):
                return _QueueState(), False
        except (OSError, RuntimePathError) as exc:
            raise QueueControlError(
                f"queue control state is unreadable: {type(exc).__name__}"
            ) from exc

        try:
            payload = json.loads(
                read_text_file(self.state_path, max_bytes=_MAX_STATE_BYTES),
                object_pairs_hook=_object_without_duplicates,
            )
            state = _state_from_payload(payload)
        except QueueControlError:
            raise
        except (
            OSError,
            RuntimePathError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise QueueControlError(
                f"queue control state is corrupt: {type(exc).__name__}"
            ) from exc
        return state, True

    def _write_state(self, state: _QueueState) -> None:
        self._ensure_runs(create=True)
        payload = _state_payload(state)
        try:
            serialized = json.dumps(
                payload,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            atomic_write_text_file(self.state_path, serialized + "\n")
        except (OSError, RuntimePathError, TypeError, ValueError) as exc:
            raise QueueControlError(
                f"could not write queue control state: {exc}"
            ) from exc

    def _inspection(self, state: _QueueState, *, exists: bool) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "state_path": str(self.state_path),
            "exists": exists,
            "corrupt": False,
            "error": None,
            "paused": state.pause is not None,
            "pause": _entry_payload(state.pause),
            "deferred": {
                str(issue): _entry_payload(entry) for issue, entry in state.deferred
            },
            "updated_at": state.updated_at,
        }

    def _corrupt_inspection(self, exc: BaseException) -> dict[str, Any]:
        try:
            self._ensure_runs(create=False)
            self.state_path.lstat()
            exists = True
        except (FileNotFoundError, QueueControlError):
            exists = False
        except OSError:
            exists = True
        return {
            "schema_version": _SCHEMA_VERSION,
            "state_path": str(self.state_path),
            "exists": exists,
            "corrupt": True,
            "error": str(exc)[:1000] or type(exc).__name__,
            # Effective pause communicates the fail-closed behavior while the
            # absent pause record avoids inventing operator metadata.
            "paused": True,
            "pause": None,
            "deferred": {},
            "updated_at": None,
        }

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        # Reads consume an atomically replaced snapshot and must not create a
        # directory or lock artifact.  Writers still serialize across threads
        # and processes before their read/change/write transaction.
        if not exclusive:
            self._ensure_runs(create=False)
            yield
            return

        self._ensure_runs(create=True)
        thread_lock = _thread_lock(self.lock_path)
        with thread_lock:
            try:
                descriptor = open_regular_file(
                    self.lock_path,
                    truncate=False,
                    mode=0o600,
                )
            except RuntimePathError as exc:
                raise QueueControlError(
                    f"could not open queue control lock: {exc}"
                ) from exc
            lock_file = os.fdopen(descriptor, "a+")
            try:
                if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                    raise QueueControlError("queue control lock is not a regular file")
                os.fchmod(lock_file.fileno(), 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()

    def _ensure_runs(self, *, create: bool) -> Path:
        try:
            return self._runtime.ensure(create=create)
        except RuntimePathError as exc:
            raise QueueControlError(f"unsafe queue state path: {exc}") from exc


def _state_from_payload(payload: object) -> _QueueState:
    if not isinstance(payload, dict):
        raise QueueControlError(
            "queue control state is corrupt: root must be an object"
        )
    expected = {"schema_version", "pause", "deferred", "updated_at"}
    if set(payload) != expected:
        raise QueueControlError("queue control state is corrupt: unexpected fields")
    if (
        not isinstance(payload["schema_version"], int)
        or isinstance(payload["schema_version"], bool)
        or payload["schema_version"] != _SCHEMA_VERSION
    ):
        raise QueueControlError(
            "queue control state is corrupt: unsupported schema version"
        )

    pause = _entry_from_payload(payload["pause"], name="pause", nullable=True)
    deferred_payload = payload["deferred"]
    if not isinstance(deferred_payload, dict):
        raise QueueControlError(
            "queue control state is corrupt: deferred must be an object"
        )
    deferred: list[tuple[int, _ControlEntry]] = []
    for raw_issue, raw_entry in deferred_payload.items():
        if not isinstance(raw_issue, str) or not raw_issue.isdigit():
            raise QueueControlError(
                "queue control state is corrupt: invalid deferred issue"
            )
        issue = int(raw_issue)
        if issue < 1 or str(issue) != raw_issue:
            raise QueueControlError(
                "queue control state is corrupt: invalid deferred issue"
            )
        entry = _entry_from_payload(raw_entry, name="deferral", nullable=False)
        assert entry is not None
        deferred.append((issue, entry))

    updated_at = payload["updated_at"]
    if not isinstance(updated_at, str):
        raise QueueControlError(
            "queue control state is corrupt: updated_at is required"
        )
    _validate_timestamp(updated_at)
    return _QueueState(pause, tuple(sorted(deferred)), updated_at)


def _entry_from_payload(
    payload: object,
    *,
    name: str,
    nullable: bool,
) -> _ControlEntry | None:
    if payload is None and nullable:
        return None
    if not isinstance(payload, dict) or set(payload) != {"reason", "timestamp"}:
        raise QueueControlError(f"queue control state is corrupt: invalid {name}")
    reason = payload["reason"]
    timestamp = payload["timestamp"]
    if not isinstance(reason, str) or _reason(reason) != reason:
        raise QueueControlError(
            f"queue control state is corrupt: invalid {name} reason"
        )
    if not isinstance(timestamp, str):
        raise QueueControlError(
            f"queue control state is corrupt: invalid {name} timestamp"
        )
    _validate_timestamp(timestamp)
    return _ControlEntry(reason, timestamp)


def _state_payload(state: _QueueState) -> dict[str, Any]:
    if state.updated_at is None:
        raise QueueControlError("cannot persist queue control state without updated_at")
    return {
        "schema_version": _SCHEMA_VERSION,
        "pause": _entry_payload(state.pause),
        "deferred": {
            str(issue): _entry_payload(entry) for issue, entry in state.deferred
        },
        "updated_at": state.updated_at,
    }


def _entry_payload(entry: _ControlEntry | None) -> dict[str, str] | None:
    if entry is None:
        return None
    return {"reason": entry.reason, "timestamp": entry.timestamp}


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reason(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("queue control reason must be text")
    clean = value.strip()
    if not clean:
        raise ValueError("queue control reason must not be empty")
    if len(clean) > _MAX_REASON_CHARS:
        raise ValueError(
            f"queue control reason must be {_MAX_REASON_CHARS} characters or fewer"
        )
    return clean


def _timestamp(now: datetime | None) -> str:
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise QueueControlError(
            "queue control state is corrupt: invalid timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise QueueControlError(
            "queue control state is corrupt: timestamp lacks timezone"
        )


def _issue_number(task_or_issue: object) -> int:
    value: object
    if isinstance(task_or_issue, bool):
        value = task_or_issue
    elif isinstance(task_or_issue, int):
        value = task_or_issue
    elif isinstance(task_or_issue, Mapping) and "issue_number" in task_or_issue:
        value = task_or_issue["issue_number"]
    elif hasattr(task_or_issue, "issue_number"):
        value = task_or_issue.issue_number
    elif hasattr(task_or_issue, "issue"):
        value = task_or_issue.issue
    else:
        raise ValueError("admission requires an issue_number or issue-shaped task")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("admission requires a positive issue number")
    return value


def _thread_lock(path: Path) -> Lock:
    key = Path(os.path.abspath(path))
    with _LOCK_REGISTRY_GUARD:
        return _THREAD_LOCKS.setdefault(key, Lock())
