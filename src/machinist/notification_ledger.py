"""Durable, bounded deduplication for best-effort notifications.

The ledger is repository-local and intentionally advisory. A missing,
unreadable, or corrupt ledger must never prevent an important notification:
delivery proceeds without deduplication and the caller receives a warning.
Only a confirmed delivery is recorded.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from machinist.notify import NotificationResult
from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    atomic_write_text_file,
    open_regular_file,
    read_text_file,
    regular_file_exists,
)

_SCHEMA_VERSION = 1
_STATE_FILENAME = "notification-ledger.json"
_LOCK_FILENAME = "notification-ledger.lock"
_MAX_STATE_BYTES = 1024 * 1024
_MAX_KEY_CHARS = 512
_DEFAULT_MAX_ENTRIES = 2_048

DEFAULT_REMINDER_TTL = timedelta(hours=24)

_LOCK_REGISTRY_GUARD = Lock()
_THREAD_LOCKS: dict[Path, Lock] = {}


class NotificationLedgerError(Exception):
    """The notification ledger could not be used safely."""


@dataclass(frozen=True)
class LedgerDelivery:
    """Outcome of a durable delivery attempt."""

    notification: NotificationResult | None
    suppressed: bool = False
    warning: str | None = None


class NotificationLedger:
    """Serialize notification delivery and remember recent successes."""

    def __init__(
        self,
        runs_dir: str | Path,
        *,
        reminder_ttl: timedelta = DEFAULT_REMINDER_TTL,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        clock: Callable[[], datetime] | None = None,
        repo_root: str | Path | None = None,
    ) -> None:
        if reminder_ttl <= timedelta(0):
            raise ValueError("notification reminder TTL must be positive")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise ValueError("notification ledger max_entries must be an integer")
        if max_entries < 1 or max_entries > 100_000:
            raise ValueError("notification ledger max_entries must be 1-100,000")
        self._runtime_error: RuntimePathError | None = None
        try:
            self._runtime: RuntimeDirectory | None = RuntimeDirectory.bind(
                runs_dir, repo_root=repo_root
            )
            self.runs_dir = self._runtime.path
        except RuntimePathError as exc:
            # Notification delivery is advisory and intentionally fail-open.
            # Keep a display-only lexical path, but never touch it after a
            # failed binding.
            self._runtime = None
            self._runtime_error = exc
            self.runs_dir = Path(os.path.abspath(os.fspath(runs_dir)))
        self.state_path = self.runs_dir / _STATE_FILENAME
        self.lock_path = self.runs_dir / _LOCK_FILENAME
        self.reminder_ttl = reminder_ttl
        self.max_entries = max_entries
        self._clock = clock or (lambda: datetime.now(UTC))

    def deliver_once(
        self,
        dedupe_key: str,
        deliver: Callable[[], NotificationResult],
    ) -> LedgerDelivery:
        """Deliver unless the same key succeeded within the reminder window.

        The cross-process lock remains held through delivery. That makes the
        read/deliver/write decision indivisible for concurrent launchd jobs.
        Ledger failures are fail-open: delivery still runs, but is not assumed
        to have been durably recorded.
        """
        try:
            key = _validated_key(dedupe_key)
        except NotificationLedgerError as exc:
            return LedgerDelivery(
                notification=deliver(),
                warning=_warning("notification ledger input is invalid", exc),
            )

        delivery_started = False
        notification: NotificationResult | None = None
        try:
            with self._locked():
                try:
                    # Read the clock only after acquiring the lock. A waiter may
                    # otherwise compare an older instant with a just-written
                    # timestamp and mistake the successful delivery for skew.
                    now = _normalized_moment(self._clock())
                except (TypeError, ValueError) as exc:
                    return LedgerDelivery(
                        notification=deliver(),
                        warning=_warning("notification ledger clock is invalid", exc),
                    )
                warning = None
                try:
                    entries = self._read_entries()
                except (NotificationLedgerError, OSError) as exc:
                    # A corrupt file is recoverable after a successful delivery.
                    entries = {}
                    warning = _warning(
                        "notification ledger state is unavailable; dedupe bypassed",
                        exc,
                    )

                delivered_at = entries.get(key)
                if delivered_at is not None and _is_recent(
                    delivered_at, now, self.reminder_ttl
                ):
                    return LedgerDelivery(
                        notification=None,
                        suppressed=True,
                        warning=warning,
                    )

                delivery_started = True
                notification = deliver()
                if notification.delivered:
                    entries = self._bounded_entries(entries, key, now)
                    try:
                        self._write_entries(entries)
                    except (NotificationLedgerError, OSError) as exc:
                        warning = _merge_warning(
                            warning,
                            _warning(
                                "notification was delivered but its ledger update failed",
                                exc,
                            ),
                        )
                return LedgerDelivery(notification=notification, warning=warning)
        except (NotificationLedgerError, OSError) as exc:
            # If writing/unlocking failed after delivery, do not send twice.
            if delivery_started:
                if notification is None:
                    raise
                return LedgerDelivery(
                    notification=notification,
                    warning=_warning(
                        "notification was delivered but its ledger update failed", exc
                    ),
                )
            return LedgerDelivery(
                notification=deliver(),
                warning=_warning(
                    "notification ledger lock is unavailable; dedupe bypassed", exc
                ),
            )

    def _bounded_entries(
        self,
        entries: dict[str, datetime],
        key: str,
        now: datetime,
    ) -> dict[str, datetime]:
        active = {
            candidate: delivered_at
            for candidate, delivered_at in entries.items()
            if candidate != key and _is_recent(delivered_at, now, self.reminder_ttl)
        }
        newest_others = sorted(
            active.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )[: self.max_entries - 1]
        # Keep the just-delivered key first so equal test clocks cannot evict it.
        return dict([(key, now), *newest_others])

    def _read_entries(self) -> dict[str, datetime]:
        try:
            if not regular_file_exists(self.state_path):
                return {}
        except RuntimePathError as exc:
            raise NotificationLedgerError("state is not safely readable") from exc

        try:
            payload = json.loads(
                read_text_file(self.state_path, max_bytes=_MAX_STATE_BYTES),
                object_pairs_hook=_object_without_duplicates,
            )
        except (
            json.JSONDecodeError,
            OSError,
            RuntimePathError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise NotificationLedgerError("state is not valid JSON") from exc

        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "entries",
        }:
            raise NotificationLedgerError("state has unexpected fields")
        version = payload["schema_version"]
        if type(version) is not int or version != _SCHEMA_VERSION:
            raise NotificationLedgerError("state has an unsupported schema version")
        raw_entries = payload["entries"]
        if not isinstance(raw_entries, dict):
            raise NotificationLedgerError("state entries must be an object")
        if len(raw_entries) > self.max_entries:
            raise NotificationLedgerError("state contains too many entries")

        entries: dict[str, datetime] = {}
        for raw_key, raw_timestamp in raw_entries.items():
            key = _validated_key(raw_key)
            if not isinstance(raw_timestamp, str):
                raise NotificationLedgerError("state contains an invalid timestamp")
            try:
                delivered_at = datetime.fromisoformat(raw_timestamp)
            except ValueError as exc:
                raise NotificationLedgerError(
                    "state contains an invalid timestamp"
                ) from exc
            if delivered_at.tzinfo is None:
                raise NotificationLedgerError("state timestamp lacks a timezone")
            entries[key] = delivered_at.astimezone(UTC)
        return entries

    def _write_entries(self, entries: dict[str, datetime]) -> None:
        self._ensure_runs(create=True)
        ordered = list(entries.items())
        low, high = 1, len(ordered)
        serialized = _serialized_state(ordered[:1])
        while low <= high:
            middle = (low + high) // 2
            candidate = _serialized_state(ordered[:middle])
            if len(candidate.encode("utf-8")) <= _MAX_STATE_BYTES:
                serialized = candidate
                low = middle + 1
            else:
                high = middle - 1
        try:
            atomic_write_text_file(self.state_path, serialized)
        except RuntimePathError as exc:
            raise NotificationLedgerError("could not write ledger state") from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
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
                raise NotificationLedgerError("could not open ledger lock") from exc
            lock_file = os.fdopen(descriptor, "a+")
            try:
                if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                    raise NotificationLedgerError("lock is not a regular file")
                os.fchmod(lock_file.fileno(), 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()

    def _ensure_runs(self, *, create: bool) -> Path:
        if self._runtime_error is not None or self._runtime is None:
            detail = self._runtime_error or "runtime path binding failed"
            raise NotificationLedgerError(f"unsafe notification state path: {detail}")
        try:
            return self._runtime.ensure(create=create)
        except RuntimePathError as exc:
            raise NotificationLedgerError(
                f"unsafe notification state path: {exc}"
            ) from exc


def _validated_key(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_KEY_CHARS:
        raise NotificationLedgerError("dedupe key must be 1-512 characters")
    if any(ord(character) < 32 for character in value):
        raise NotificationLedgerError("dedupe key contains control characters")
    return value


def _normalized_moment(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("notification ledger clock must return a datetime")
    if value.tzinfo is None:
        raise ValueError("notification ledger clock must return an aware datetime")
    return value.astimezone(UTC)


def _is_recent(delivered_at: datetime, now: datetime, ttl: timedelta) -> bool:
    age = now - delivered_at
    # Future timestamps cannot suppress forever after clock skew or corruption.
    return timedelta(0) <= age < ttl


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _serialized_state(entries: list[tuple[str, datetime]]) -> str:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "entries": {
            key: delivered_at.astimezone(UTC).isoformat()
            for key, delivered_at in entries
        },
    }
    return (
        json.dumps(
            payload, allow_nan=False, indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n"
    )


def _thread_lock(path: Path) -> Lock:
    key = Path(os.path.abspath(path))
    with _LOCK_REGISTRY_GUARD:
        return _THREAD_LOCKS.setdefault(key, Lock())


def _warning(prefix: str, exc: BaseException) -> str:
    return f"{prefix}: {type(exc).__name__}: {str(exc)[:500]}"


def _merge_warning(first: str | None, second: str) -> str:
    return second if first is None else f"{first}; {second}"
