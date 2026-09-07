"""Repository-bound local Tasks, atomic projections and operation Claims.

The index allocates durable local IDs independently of forge issue numbers.
Operation Claims protect a whole foreground action; they deliberately do not
reuse the lifecycle Claim that protects a single Phase attempt.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    atomic_write_text_file,
    list_directory_names,
    open_regular_file,
    read_text_file,
    regular_file_exists,
)

_VERSION = 1
_MAX_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_TASK_ID = re.compile(r"T([1-9][0-9]*)")
_THREAD_LOCKS: dict[Path, Lock] = {}
_THREAD_LOCKS_GUARD = Lock()


class LocalTaskError(Exception):
    """Local Task state is invalid, unsafe or unavailable."""


class LocalTaskConflict(LocalTaskError):
    """A concurrent operation or newer revision prevents this mutation."""


class LocalTaskNotFound(LocalTaskError):
    """The selected local Task does not exist."""


@dataclass(frozen=True)
class LocalTask:
    number: int
    title: str
    body: str
    repository: str
    base_branch: str
    base_sha: str
    branch: str
    created_at: str
    updated_at: str
    revision: int
    spec_sha: str | None = None
    candidate_sha: str | None = None
    approval: dict[str, Any] | None = None
    review_report: dict[str, Any] | None = None
    integration: dict[str, Any] | None = None
    publication: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    feedback: str | None = None
    spec_base_sha: str | None = None

    @property
    def id(self) -> str:
        return f"T{self.number}"


_TASK_FIELDS = frozenset(field.name for field in fields(LocalTask))
_IMMUTABLE_FIELDS = frozenset(
    {
        "number",
        "repository",
        "base_branch",
        "base_sha",
        "branch",
        "created_at",
        "updated_at",
        "revision",
    }
)


class LocalTaskStore:
    """Own local Task identity, revisions and safe runtime persistence."""

    def __init__(self, repo_root: str | Path):
        try:
            raw_root = Path(repo_root).expanduser().absolute()
            self._runtime = RuntimeDirectory.bind(
                raw_root / ".machinist/runs/local/tasks", repo_root=raw_root
            )
        except (RuntimePathError, OSError, ValueError) as exc:
            raise LocalTaskError(f"unsafe local Task directory: {exc}") from exc
        self.repo_root = self._runtime.repository_root
        self.tasks_dir = self._runtime.path

    def create(
        self,
        title: str,
        body: str,
        base_branch: str,
        base_sha: str,
        branch_prefix: str,
        source: dict[str, Any] | None = None,
    ) -> LocalTask:
        """Allocate an ID before recording its Task; interrupted creates leave gaps."""
        if not isinstance(branch_prefix, str) or not branch_prefix.endswith("/"):
            raise LocalTaskError(
                "branch_prefix must be a safe Git prefix ending in '/'"
            )
        now = _now()
        proposal = LocalTask(
            number=1,
            title=title,
            body=body,
            repository=str(self.repo_root),
            base_branch=base_branch,
            base_sha=base_sha,
            branch=f"{branch_prefix}task-1",
            created_at=now,
            updated_at=now,
            revision=1,
            source=_copy_object(source, "source"),
        )
        _validate_task(proposal)
        with self._locked("index.lock"):
            index = self._index(create=True)
            assert index is not None
            number = index["next_number"]
            task = replace(
                proposal, number=number, branch=f"{branch_prefix}task-{number}"
            )
            path = self._task_path(number)
            if self._exists(path):
                raise LocalTaskError("local Task index would reuse an existing ID")
            # Persist the counter first so deleting a Task cannot reuse its ID,
            # and a crash between these atomic writes never double-allocates.
            self._write_json(
                self.tasks_dir / "index.json", {**index, "next_number": number + 1}
            )
            self._write_task(task, index)
            return task

    def get(self, task: str | int) -> LocalTask:
        number = _number(task)
        self._ensure(create=False)
        index = self._index(create=False)
        if index is None:
            raise LocalTaskNotFound(f"local Task T{number} does not exist")
        return self._read_task(number, index)

    def list(self) -> tuple[LocalTask, ...]:
        self._ensure(create=False)
        # Inspect names before the index: allocation advances the index before
        # creating a record, so concurrent creates cannot look unallocated.
        names = self._names()
        index = self._index(create=False)
        if index is None:
            return ()
        numbers = []
        for name in names:
            if name.startswith("T") and name.endswith(".json"):
                numbers.append(_number(name[:-5]))
        return tuple(self._read_task(number, index) for number in sorted(numbers))

    def update(self, task: LocalTask, **changes: Any) -> LocalTask:
        """Atomically change explicit fields only if the supplied revision is current."""
        if not isinstance(task, LocalTask):
            raise LocalTaskError("update requires a LocalTask")
        unknown = set(changes) - _TASK_FIELDS
        if unknown:
            raise LocalTaskError(
                "unknown local Task fields: " + ", ".join(sorted(unknown))
            )
        immutable = set(changes) & _IMMUTABLE_FIELDS
        if immutable:
            raise LocalTaskError(
                "immutable local Task fields: " + ", ".join(sorted(immutable))
            )
        with self._locked("index.lock"):
            current, index = self._current(task)
            # Never persist incidental mutations of a caller's nested dicts.
            # Current disk state plus explicit changes is the only write input.
            changed = replace(
                current,
                **changes,
                revision=current.revision + 1,
                updated_at=max(_now(), current.updated_at),
            )
            _validate_task(changed)
            self._write_task(changed, index)
            return self._read_task(changed.number, index)

    @contextmanager
    def claim(self, task: str | int) -> Iterator[LocalTask]:
        """Yield fresh Task state under a nonblocking whole-operation Claim."""
        number = _number(task)
        self.get(number)
        with self._locked(f"T{number}-operation.lock", blocking=False):
            yield self.get(number)

    def report_path(self, task: LocalTask | str | int) -> Path:
        number = _number(task.number if isinstance(task, LocalTask) else task)
        return self.tasks_dir / f"T{number}-report.md"

    def save_report(self, task: LocalTask, markdown: str) -> Path:
        """Atomically save derived Review text only for the current Task revision."""
        if not isinstance(markdown, str) or len(markdown.encode("utf-8")) > _MAX_BYTES:
            raise LocalTaskError("local Task report must be bounded Markdown text")
        with self._locked("index.lock"):
            current, _index = self._current(task)
            path = self.report_path(current)
            self._write_text(path, markdown)
            return path

    def read_report(self, task: str | int) -> str | None:
        current = self.get(task)
        path = self.report_path(current)
        return self._read_text(path) if self._exists(path) else None

    def _current(self, task: LocalTask) -> tuple[LocalTask, dict[str, Any]]:
        if not isinstance(task, LocalTask):
            raise LocalTaskError("mutation requires a LocalTask")
        if task.repository != str(self.repo_root):
            raise LocalTaskError("local Task belongs to a different repository")
        index = self._index(create=False)
        if index is None:
            raise LocalTaskNotFound(f"local Task {task.id} does not exist")
        current = self._read_task(_number(task.number), index)
        if type(task.revision) is not int or current.revision != task.revision:
            raise LocalTaskConflict(
                f"local Task {task.id} changed; reload before retrying"
            )
        return current, index

    def _index(self, *, create: bool) -> dict[str, Any] | None:
        identity_path = self.tasks_dir / "identity.json"
        index_path = self.tasks_dir / "index.json"
        has_identity, has_index = self._exists(identity_path), self._exists(index_path)
        if has_identity != has_index:
            raise LocalTaskError(
                "local Task identity/index is incomplete; refusing ID reuse"
            )
        if not has_identity:
            if any(name != "index.lock" for name in self._names()):
                raise LocalTaskError(
                    "local Task identity/index is incomplete; records remain"
                )
            if not create:
                return None
            identity = {
                "version": _VERSION,
                "repository": str(self.repo_root),
                "repository_id": str(uuid4()),
            }
            index = {**identity, "next_number": 1}
            self._write_json(identity_path, identity)
            self._write_json(index_path, index)
            return index
        identity = self._read_json(identity_path)
        index = self._read_json(index_path)
        self._validate_metadata(identity, indexed=False)
        self._validate_metadata(index, indexed=True)
        if index["repository_id"] != identity["repository_id"]:
            raise LocalTaskError("local Task index has a different repository identity")
        return index

    def _validate_metadata(self, payload: dict[str, Any], *, indexed: bool) -> None:
        expected = {"version", "repository", "repository_id"}
        if indexed:
            expected.add("next_number")
        if set(payload) != expected:
            raise LocalTaskError("invalid local Task metadata fields")
        _version(payload)
        if payload["repository"] != str(self.repo_root):
            raise LocalTaskError(
                "local Task metadata belongs to a different repository"
            )
        identifier = payload["repository_id"]
        try:
            if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
                raise ValueError("not a canonical UUID")
        except ValueError as exc:
            raise LocalTaskError("invalid local Task repository identity") from exc
        if indexed:
            _positive_int(payload["next_number"], "next_number")

    def _read_task(self, number: int, index: dict[str, Any]) -> LocalTask:
        path = self._task_path(number)
        if not self._exists(path):
            raise LocalTaskNotFound(f"local Task T{number} does not exist")
        payload = self._read_json(path)
        _version(payload)
        if set(payload) != _TASK_FIELDS | {"version", "repository_id"}:
            raise LocalTaskError(f"invalid local Task T{number} record fields")
        if payload["repository_id"] != index["repository_id"]:
            raise LocalTaskError(
                f"local Task T{number} has a different repository identity"
            )
        task = LocalTask(**{name: payload[name] for name in _TASK_FIELDS})
        _validate_task(task)
        if task.number != number or number >= index["next_number"]:
            raise LocalTaskError(
                f"local Task T{number} has an invalid allocated identity"
            )
        if task.repository != str(self.repo_root):
            raise LocalTaskError(
                f"local Task T{number} belongs to a different repository"
            )
        return task

    def _write_task(self, task: LocalTask, index: dict[str, Any]) -> None:
        self._write_json(
            self._task_path(task.number),
            {
                **asdict(task),
                "version": _VERSION,
                "repository_id": index["repository_id"],
            },
        )

    def _task_path(self, number: int) -> Path:
        return self.tasks_dir / f"T{_number(number)}.json"

    def _ensure(self, *, create: bool) -> None:
        try:
            self._runtime.ensure(create=create)
        except RuntimePathError as exc:
            raise LocalTaskError(f"unsafe local Task directory: {exc}") from exc

    def _names(self) -> tuple[str, ...]:
        try:
            return list_directory_names(self.tasks_dir)
        except RuntimePathError as exc:
            raise LocalTaskError(f"cannot list local Tasks: {exc}") from exc

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            return regular_file_exists(path)
        except RuntimePathError as exc:
            raise LocalTaskError(f"unsafe local Task file: {exc}") from exc

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return read_text_file(path, max_bytes=_MAX_BYTES)
        except (RuntimePathError, UnicodeError) as exc:
            raise LocalTaskError(f"cannot read local Task file: {exc}") from exc

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(self._read_text(path), object_pairs_hook=_unique_pairs)
        except (ValueError, RecursionError) as exc:
            raise LocalTaskError(f"invalid local Task JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise LocalTaskError("local Task JSON must be an object")
        return value

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        try:
            atomic_write_text_file(path, value)
        except RuntimePathError as exc:
            raise LocalTaskError(f"cannot persist local Task file: {exc}") from exc

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._write_text(path, _encode(value) + "\n")

    @contextmanager
    def _locked(self, filename: str, *, blocking: bool = True) -> Iterator[None]:
        self._ensure(create=True)
        path = self.tasks_dir / filename
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(path, Lock())
        if not thread_lock.acquire(blocking=blocking):
            raise LocalTaskConflict("local Task has an active operation")
        try:
            try:
                descriptor = open_regular_file(path, truncate=False, mode=0o600)
            except RuntimePathError as exc:
                raise LocalTaskError(f"unsafe local Task lock: {exc}") from exc
            with os.fdopen(descriptor, "a+") as lock_file:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                try:
                    fcntl.flock(lock_file.fileno(), flags)
                except BlockingIOError as exc:
                    raise LocalTaskConflict(
                        "local Task has an active operation"
                    ) from exc
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise LocalTaskError(f"local Task lock failed: {exc}") from exc
        finally:
            thread_lock.release()


def _number(value: object) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and (match := _TASK_ID.fullmatch(value)):
        try:
            return int(match.group(1))
        except ValueError:
            pass
    raise LocalTaskError("Task identifier must be a positive integer or T<number>")


def _positive_int(value: object, name: str) -> None:
    if type(value) is not int or value < 1:
        raise LocalTaskError(f"local Task {name} must be a positive integer")


def _version(payload: dict[str, Any]) -> None:
    if type(payload.get("version")) is not int or payload["version"] != _VERSION:
        raise LocalTaskError("unsupported local Task record version")


def _branch(value: object) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) is None
        or ".." in value
        or "//" in value
        or value.endswith(("/", "."))
        or any(
            part.startswith(".") or part.endswith(".lock") for part in value.split("/")
        )
    ):
        raise LocalTaskError("local Task branch must be a safe Git branch name")


def _validate_task(task: LocalTask) -> None:
    _positive_int(task.number, "number")
    _positive_int(task.revision, "revision")
    for name in ("title", "body", "repository"):
        value = getattr(task, name)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise LocalTaskError(f"local Task {name} must be nonempty text")
    _branch(task.branch)
    _branch(task.base_branch)
    for name in ("base_sha", "spec_sha", "candidate_sha", "spec_base_sha"):
        value = getattr(task, name)
        if value is None and name != "base_sha":
            continue
        if not isinstance(value, str) or _SHA.fullmatch(value) is None:
            raise LocalTaskError(f"local Task {name} must be a full 40-character SHA")
    for name in ("created_at", "updated_at"):
        value = getattr(task, name)
        try:
            if (
                not isinstance(value, str)
                or datetime.fromisoformat(value).utcoffset() is None
            ):
                raise ValueError("timezone missing")
        except ValueError as exc:
            raise LocalTaskError(f"invalid local Task {name} timestamp") from exc
    if datetime.fromisoformat(task.updated_at) < datetime.fromisoformat(
        task.created_at
    ):
        raise LocalTaskError("local Task updated_at precedes created_at")
    if task.feedback is not None and not isinstance(task.feedback, str):
        raise LocalTaskError("local Task feedback must be text")
    for name in ("approval", "review_report", "integration", "publication", "source"):
        value = getattr(task, name)
        if value is not None and not isinstance(value, dict):
            raise LocalTaskError(f"local Task {name} must be a JSON object")
    _encode({name: getattr(task, name) for name in _TASK_FIELDS})


def _copy_object(value: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LocalTaskError(f"local Task {name} must be a JSON object")
    return json.loads(_encode(value))


def _encode(value: dict[str, Any]) -> str:
    try:
        _validate_json(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode("utf-8")) > _MAX_BYTES:
            raise LocalTaskError("local Task record is too large")
        return encoded
    except (TypeError, ValueError, RecursionError) as exc:
        raise LocalTaskError(f"invalid local Task JSON value: {exc}") from exc


def _validate_json(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LocalTaskError("local Task JSON object keys must be text")
            if (key == "sha" or key.endswith("_sha")) and item is not None:
                if not isinstance(item, str) or _SHA.fullmatch(item) is None:
                    raise LocalTaskError(
                        f"local Task {key} must be a full 40-character SHA"
                    )
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise LocalTaskError("local Task Evidence must contain JSON values")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _now() -> str:
    return datetime.now(UTC).isoformat()
