"""Atomic user registry and read-only status collection for multiple repositories."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from machinist.lifecycle import TaskLifecycle
from machinist.observability import RunReport, build_run_report

_SCHEMA_VERSION = 1
_GIT_TIMEOUT_SECONDS = 10
DEFAULT_REGISTRY_PATH = Path("~/.machinist/portfolio.json")

type GitRootLoader = Callable[[Path], Path]
type StatusLoader = Callable[[Path], object]


class PortfolioError(Exception):
    """A registry or local repository status operation could not be completed."""


class CorruptPortfolioError(PortfolioError):
    """The registry cannot be trusted and must not be partially consumed."""


@dataclass(frozen=True)
class RepositoryStatus:
    """One isolated, read-only repository status result."""

    path: Path
    report: object | None = None
    error_type: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        report = self.report
        to_dict = None if report is None else getattr(report, "to_dict", None)
        if callable(to_dict):
            report = to_dict()
        return {
            "path": str(self.path),
            "ok": self.ok,
            "report": report,
            "error": (
                None if self.ok else {"type": self.error_type, "message": self.error}
            ),
        }


class PortfolioRegistry:
    """Own the canonical set of repositories supervised by one user."""

    def __init__(
        self,
        path: str | Path = DEFAULT_REGISTRY_PATH,
        *,
        git_root_loader: GitRootLoader | None = None,
        runner=subprocess.run,
    ):
        self.path = Path(path).expanduser().resolve()
        self._runner = runner
        self._git_root_loader = git_root_loader or self._git_root

    def list(self) -> tuple[Path, ...]:
        """Return the complete registry without creating any filesystem state."""
        return self._read()

    def add(self, path: str | Path) -> Path:
        """Add a repository by canonical Git root; duplicate additions are no-ops."""
        root = self._canonical_git_root(Path(path))
        with self._mutation_lock():
            repositories = self._read()
            if root not in repositories:
                self._write(tuple(sorted((*repositories, root), key=str)))
        return root

    def remove(self, path: str | Path) -> bool:
        """Remove an existing repository, including a now-missing exact root."""
        candidate = Path(path).expanduser().resolve()
        with self._mutation_lock():
            repositories = self._read()
            try:
                root = self._canonical_git_root(candidate)
            except PortfolioError:
                root = candidate
            if root not in repositories:
                return False
            self._write(tuple(item for item in repositories if item != root))
            return True

    def _canonical_git_root(self, path: Path) -> Path:
        try:
            root = Path(self._git_root_loader(path)).expanduser().resolve(strict=True)
        except PortfolioError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise PortfolioError(
                f"cannot resolve Git repository {path}: {exc}"
            ) from exc
        if not root.is_dir():
            raise PortfolioError(f"Git root is not a directory: {root}")
        return root

    def _git_root(self, path: Path) -> Path:
        try:
            candidate = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise PortfolioError(f"repository path does not exist: {path}") from exc
        if not candidate.is_dir():
            raise PortfolioError(f"repository path is not a directory: {candidate}")
        try:
            result = self._runner(
                ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise PortfolioError(
                f"Git root lookup timed out after {_GIT_TIMEOUT_SECONDS} seconds"
            ) from exc
        except OSError as exc:
            raise PortfolioError(
                f"cannot inspect Git repository {candidate}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or "not a Git repository"
            raise PortfolioError(f"cannot add {candidate}: {detail.splitlines()[0]}")
        output = (result.stdout or "").strip()
        if not output:
            raise PortfolioError(f"Git returned no repository root for {candidate}")
        return Path(output)

    def _read(self) -> tuple[Path, ...]:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise PortfolioError(
                f"cannot read portfolio registry {self.path}: {exc}"
            ) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CorruptPortfolioError(
                f"portfolio registry {self.path} is invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "repositories",
        }:
            raise CorruptPortfolioError(
                f"portfolio registry {self.path} has an unsupported shape"
            )
        if payload["schema_version"] != _SCHEMA_VERSION:
            raise CorruptPortfolioError(
                f"portfolio registry {self.path} uses unsupported schema version "
                f"{payload['schema_version']!r}"
            )
        raw = payload["repositories"]
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise CorruptPortfolioError(
                "portfolio repositories must be a list of paths"
            )

        repositories: list[Path] = []
        for item in raw:
            stored = Path(item)
            canonical = stored.expanduser().resolve()
            if not stored.is_absolute() or stored != canonical:
                raise CorruptPortfolioError(
                    f"portfolio repository path is not canonical: {item!r}"
                )
            repositories.append(stored)
        if len(repositories) != len(set(repositories)):
            raise CorruptPortfolioError(
                "portfolio registry contains duplicate repositories"
            )
        if repositories != sorted(repositories, key=str):
            raise CorruptPortfolioError(
                "portfolio repositories are not canonically ordered"
            )
        return tuple(repositories)

    @contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                self.path.with_suffix(self.path.suffix + ".lock"),
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
        except OSError as exc:
            raise PortfolioError(
                f"cannot lock portfolio registry {self.path}: {exc}"
            ) from exc
        with os.fdopen(descriptor, "a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, repositories: tuple[Path, ...]) -> None:
        payload = (
            json.dumps(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "repositories": [str(path) for path in repositories],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        mode = 0o600
        try:
            if self.path.exists():
                mode = stat.S_IMODE(self.path.stat().st_mode)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            try:
                os.fchmod(descriptor, mode)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError as exc:
            raise PortfolioError(
                f"cannot write portfolio registry {self.path}: {exc}"
            ) from exc


def collect_local_status(
    registry: PortfolioRegistry,
    *,
    loader: StatusLoader | None = None,
) -> tuple[RepositoryStatus, ...]:
    """Read each registered repository independently without remote operations."""
    status_loader = loader or _load_local_status
    results: list[RepositoryStatus] = []
    for path in registry.list():
        try:
            report = status_loader(path)
        except Exception as exc:  # noqa: BLE001 - one repo must not hide the others
            results.append(
                RepositoryStatus(
                    path=path,
                    error_type=type(exc).__name__,
                    error=str(exc).strip() or type(exc).__name__,
                )
            )
        else:
            results.append(RepositoryStatus(path=path, report=report))
    return tuple(results)


def _load_local_status(path: Path) -> RunReport:
    if not path.is_dir():
        raise PortfolioError("registered repository no longer exists")
    return build_run_report(TaskLifecycle(path / ".machinist" / "runs"))
