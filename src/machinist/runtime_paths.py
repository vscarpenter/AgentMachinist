"""Fail-closed repository-local runtime directory handling.

Runtime state is intentionally kept below a repository boundary.  Pathlib's
``mkdir(parents=True)`` follows existing symlink components, so it is not a
sufficient boundary check for state that can later influence controller
decisions.  This module binds a directory to a repository root and walks every
managed component with ``lstat`` before optionally creating it.
"""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class RuntimePathError(Exception):
    """A runtime state path is outside its boundary or unsafe to use."""


@dataclass(frozen=True)
class RuntimeDirectory:
    """A runtime directory bound to one canonical repository root."""

    repository_root: Path
    path: Path
    _anchor: Path

    @classmethod
    def bind(
        cls,
        runs_dir: str | Path,
        *,
        repo_root: str | Path | None = None,
    ) -> RuntimeDirectory:
        """Bind ``runs_dir`` without creating it and validate existing parts.

        ``repo_root`` is explicit in controller-facing code.  For the public
        state-store APIs it remains optional for compatibility: a conventional
        ``<repo>/.machinist/runs`` path infers ``<repo>``; other paths infer
        their direct parent as the boundary.
        """
        target = _absolute(runs_dir, description="runtime directory")
        if repo_root is None:
            raw_root = (
                target.parent.parent
                if target.name == "runs" and target.parent.name == ".machinist"
                else target.parent
            )
            canonical_root, anchor = _canonical_root(raw_root, allow_missing=True)
        else:
            raw_root = _absolute(repo_root, description="repository root")
            canonical_root, anchor = _canonical_root(raw_root, allow_missing=False)

        try:
            relative = target.relative_to(raw_root)
        except ValueError as exc:
            raise RuntimePathError(
                f"runtime directory must be contained by repository {canonical_root}"
            ) from exc
        if not relative.parts:
            raise RuntimePathError(
                f"runtime directory must be below repository {canonical_root}"
            )

        binding = cls(
            repository_root=canonical_root,
            path=canonical_root / relative,
            _anchor=anchor,
        )
        binding.ensure(create=False)
        return binding

    def ensure(self, *, create: bool) -> Path:
        """Validate the runtime path and optionally create missing components."""
        return self.ensure_directory(self.path, create=create)

    def subdirectory(self, *parts: str, create: bool) -> Path:
        """Validate and optionally create a safe directory below this runtime."""
        if not parts or any(not _safe_component(part) for part in parts):
            raise RuntimePathError("runtime subdirectory names must be safe components")
        return self.ensure_directory(self.path.joinpath(*parts), create=create)

    def ensure_directory(self, path: str | Path, *, create: bool) -> Path:
        """Validate a descendant directory against the bound repository."""
        target = _absolute(path, description="runtime subdirectory")
        try:
            target.relative_to(self.repository_root)
            target.relative_to(self.path)
        except ValueError as exc:
            raise RuntimePathError(
                f"runtime path must remain below {self.path}"
            ) from exc
        _walk_directories(self._anchor, target, create=create)
        return target


def resolve_runtime_dir(
    runs_dir: str | Path,
    *,
    repo_root: str | Path | None = None,
    create: bool = False,
) -> Path:
    """Return a validated canonical runtime directory."""
    return RuntimeDirectory.bind(runs_dir, repo_root=repo_root).ensure(create=create)


def reserve_regular_file(path: str | Path, *, mode: int = 0o600) -> Path:
    """Create or validate a private regular file without following a symlink."""
    target = _absolute(path, description="runtime file")
    descriptor = open_regular_file(target, truncate=False, mode=mode)
    os.close(descriptor)
    return target


def validate_regular_file(path: str | Path, *, missing_ok: bool = True) -> Path:
    """Validate an existing regular leaf without opening or creating it."""
    target = _absolute(path, description="runtime file")
    try:
        with _open_parent_directory(target, create=False) as (parent_fd, name):
            metadata = _metadata_at(parent_fd, name)
            if metadata is None:
                if missing_ok:
                    return target
                raise RuntimePathError(f"runtime file does not exist: {target}")
            _validate_regular_metadata(target, metadata, max_bytes=None)
    except RuntimePathError as exc:
        if missing_ok and isinstance(exc.__cause__, FileNotFoundError):
            return target
        raise
    return target


def regular_file_exists(path: str | Path) -> bool:
    """Return whether a safe regular leaf exists through pinned traversal."""
    target = _absolute(path, description="runtime file")
    try:
        with _open_parent_directory(target, create=False) as (parent_fd, name):
            metadata = _metadata_at(parent_fd, name)
            if metadata is None:
                return False
            _validate_regular_metadata(target, metadata, max_bytes=None)
            return True
    except RuntimePathError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return False
        raise


def list_directory_names(
    path: str | Path,
    *,
    missing_ok: bool = True,
    max_entries: int = 100_000,
) -> tuple[str, ...]:
    """List a real directory through a pinned descriptor with an entry cap."""
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries < 1
    ):
        raise ValueError("runtime directory entry limit must be a positive integer")
    target = _absolute(path, description="runtime directory")
    try:
        with _open_directory(target, create=False) as descriptor:
            names = os.listdir(descriptor)
    except RuntimePathError as exc:
        if missing_ok and isinstance(exc.__cause__, FileNotFoundError):
            return ()
        raise
    if len(names) > max_entries:
        raise RuntimePathError(
            f"runtime directory has too many entries ({len(names)}; maximum {max_entries}): {target}"
        )
    return tuple(sorted(names))


def open_regular_file(
    path: str | Path,
    *,
    truncate: bool,
    mode: int = 0o600,
) -> int:
    """Open a regular file by descriptor without following its leaf symlink."""
    target = _absolute(path, description="runtime file")
    with _open_parent_directory(target, create=False) as (parent_fd, name):
        before = _metadata_at(parent_fd, name)
        if before is not None:
            _validate_regular_metadata(target, before, max_bytes=None)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if before is None:
            flags |= os.O_EXCL
        try:
            descriptor = os.open(name, flags, mode, dir_fd=parent_fd)
        except FileExistsError as exc:
            if before is not None:
                raise RuntimePathError(
                    f"cannot open runtime file {target}: {exc}"
                ) from exc
            retry_flags = (flags & ~os.O_EXCL) & ~os.O_CREAT
            try:
                descriptor = os.open(name, retry_flags, dir_fd=parent_fd)
            except (OSError, ValueError) as retry_exc:
                raise RuntimePathError(
                    f"cannot open runtime file {target}: {retry_exc}"
                ) from retry_exc
        except (OSError, ValueError) as exc:
            raise RuntimePathError(f"cannot open runtime file {target}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            current = _metadata_at(parent_fd, name)
            _validate_regular_metadata(target, opened, max_bytes=None)
            if current is None or _identity(opened) != _identity(current):
                raise RuntimePathError(f"runtime file changed while opening: {target}")
            if before is not None and _identity(before) != _identity(opened):
                raise RuntimePathError(f"runtime file changed while opening: {target}")
            os.fchmod(descriptor, mode)
            if truncate:
                os.ftruncate(descriptor, 0)
            if before is None:
                os.fsync(descriptor)
                os.fsync(parent_fd)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise


def write_text_file(path: str | Path, value: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace one runtime text file through a pinned parent."""
    atomic_write_text_file(path, value, encoding=encoding)


def atomic_write_text_file(
    path: str | Path,
    value: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o600,
) -> None:
    """Durably replace a regular file without following any path component."""
    target = _absolute(path, description="runtime file")
    encoded = value.encode(encoding)
    with _open_parent_directory(target, create=False) as (parent_fd, name):
        before = _metadata_at(parent_fd, name)
        if before is not None:
            _validate_regular_metadata(target, before, max_bytes=None)
        temporary = f".{name}.machinist-{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, mode, dir_fd=parent_fd)
            os.fchmod(descriptor, mode)
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1

            current = _metadata_at(parent_fd, name)
            if before is None:
                if current is not None:
                    raise RuntimePathError(
                        f"runtime file appeared while replacing: {target}"
                    )
            elif current is None or _identity(before) != _identity(current):
                raise RuntimePathError(
                    f"runtime file changed while replacing: {target}"
                )
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except RuntimePathError:
            raise
        except (OSError, ValueError) as exc:
            raise RuntimePathError(
                f"cannot replace runtime file {target}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                # The state update result above remains authoritative. A stale
                # private temporary is inert and can be removed by the operator.
                pass


def append_text_file(
    path: str | Path,
    value: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o600,
) -> None:
    """Durably append to a regular file through a pinned parent descriptor."""
    target = _absolute(path, description="runtime file")
    encoded = value.encode(encoding)
    with _open_parent_directory(target, create=False) as (parent_fd, name):
        before = _metadata_at(parent_fd, name)
        if before is not None:
            _validate_regular_metadata(target, before, max_bytes=None)
        flags = os.O_APPEND | os.O_WRONLY | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if before is None:
            flags |= os.O_EXCL
        descriptor = -1
        try:
            descriptor = os.open(name, flags, mode, dir_fd=parent_fd)
            opened = os.fstat(descriptor)
            _validate_regular_metadata(target, opened, max_bytes=None)
            if before is not None and _identity(before) != _identity(opened):
                raise RuntimePathError(f"runtime file changed while opening: {target}")
            os.fchmod(descriptor, mode)
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            current = _metadata_at(parent_fd, name)
            if current is None or _identity(opened) != _identity(current):
                raise RuntimePathError(
                    f"runtime file changed while appending: {target}"
                )
            os.fsync(parent_fd)
        except RuntimePathError:
            raise
        except (OSError, ValueError) as exc:
            raise RuntimePathError(
                f"cannot append runtime file {target}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def unlink_regular_file(path: str | Path, *, missing_ok: bool = False) -> bool:
    """Durably unlink a regular file through a pinned parent descriptor."""
    target = _absolute(path, description="runtime file")
    try:
        with _open_parent_directory(target, create=False) as (parent_fd, name):
            metadata = _metadata_at(parent_fd, name)
            if metadata is None:
                if missing_ok:
                    return False
                raise RuntimePathError(f"runtime file does not exist: {target}")
            _validate_regular_metadata(target, metadata, max_bytes=None)
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return True
    except RuntimePathError as exc:
        if missing_ok and isinstance(exc.__cause__, FileNotFoundError):
            return False
        raise
    except (OSError, ValueError) as exc:
        raise RuntimePathError(f"cannot unlink runtime file {target}: {exc}") from exc


def read_text_file(
    path: str | Path,
    *,
    max_bytes: int,
    encoding: str = "utf-8",
) -> str:
    """Read a bounded regular text file through a no-follow descriptor."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("runtime file read limit must be a positive integer")
    target = _absolute(path, description="runtime file")
    with _open_parent_directory(target, create=False) as (parent_fd, name):
        before = _metadata_at(parent_fd, name)
        if before is None:
            raise RuntimePathError(f"runtime file does not exist: {target}")
        _validate_regular_metadata(target, before, max_bytes=max_bytes)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except (OSError, ValueError) as exc:
            raise RuntimePathError(f"cannot open runtime file {target}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            _validate_regular_metadata(target, opened, max_bytes=max_bytes)
            current = _metadata_at(parent_fd, name)
            if (
                current is None
                or _identity(before) != _identity(opened)
                or _identity(opened) != _identity(current)
            ):
                raise RuntimePathError(f"runtime file changed while opening: {target}")
            payload = _read_bounded(descriptor, max_bytes=max_bytes)
            if len(payload) > max_bytes:
                raise RuntimePathError(
                    f"runtime file is too large ({len(payload)}+ bytes; maximum {max_bytes}): {target}"
                )
            return payload.decode(encoding)
        finally:
            os.close(descriptor)


def _absolute(value: str | Path, *, description: str) -> Path:
    try:
        raw = Path(value).expanduser()
        # ``abspath`` normalizes ``..`` without resolving symlinks.  Symlink
        # resolution is deliberately reserved for the trusted boundary.
        return Path(os.path.abspath(os.fspath(raw)))
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimePathError(f"invalid {description}: {exc}") from exc


def _canonical_root(raw_root: Path, *, allow_missing: bool) -> tuple[Path, Path]:
    probe = raw_root
    missing: list[str] = []
    while True:
        try:
            metadata = probe.lstat()
        except FileNotFoundError:
            if probe == probe.parent:
                raise RuntimePathError(
                    f"repository root does not exist: {raw_root}"
                ) from None
            missing.append(probe.name)
            probe = probe.parent
            continue
        except (OSError, ValueError) as exc:
            raise RuntimePathError(f"cannot inspect repository root: {exc}") from exc
        break

    if missing and not allow_missing:
        raise RuntimePathError(f"repository root does not exist: {raw_root}")
    if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        raise RuntimePathError(f"repository root is not a directory: {probe}")
    try:
        anchor = probe.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimePathError(f"cannot resolve repository root: {exc}") from exc
    if not anchor.is_dir():
        raise RuntimePathError(f"repository root is not a directory: {anchor}")
    canonical_root = anchor.joinpath(*reversed(missing))
    return canonical_root, anchor


def _walk_directories(anchor: Path, target: Path, *, create: bool) -> None:
    try:
        target.relative_to(anchor)
    except ValueError as exc:
        raise RuntimePathError(f"runtime path escapes trusted anchor {anchor}") from exc

    # Opening each component relative to the prior descriptor pins traversal
    # against rename-and-symlink swaps. Newly created directory entries are
    # synced through their parent before the descriptor advances.
    try:
        with _open_directory(target, create=create):
            pass
    except RuntimePathError as exc:
        if not create and isinstance(exc.__cause__, FileNotFoundError):
            return
        raise


@contextmanager
def _open_directory(path: Path, *, create: bool) -> Iterator[int]:
    target = _absolute(path, description="runtime directory")
    anchor = Path(target.anchor)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(anchor, flags)
    except (OSError, ValueError) as exc:
        raise RuntimePathError(
            f"cannot open runtime path anchor {anchor}: {exc}"
        ) from exc
    try:
        current_path = anchor
        for component in target.parts[1:]:
            current_path /= component
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError as exc:
                if not create:
                    raise RuntimePathError(
                        f"runtime directory does not exist: {current_path}"
                    ) from exc
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileExistsError:
                    try:
                        child = os.open(component, flags, dir_fd=descriptor)
                    except (OSError, ValueError) as open_exc:
                        raise RuntimePathError(
                            f"runtime directory contains a symlink component or non-directory: {current_path}"
                        ) from open_exc
                except (OSError, ValueError) as create_exc:
                    raise RuntimePathError(
                        f"cannot create runtime directory {current_path}: {create_exc}"
                    ) from create_exc
            except (OSError, ValueError) as exc:
                raise RuntimePathError(
                    f"runtime directory contains a symlink component or non-directory: {current_path}"
                ) from exc
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise RuntimePathError(
                    f"runtime path component is not a directory: {current_path}"
                )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_parent_directory(target: Path, *, create: bool) -> Iterator[tuple[int, str]]:
    if not target.name:
        raise RuntimePathError(f"runtime file has no filename: {target}")
    with _open_directory(target.parent, create=create) as parent_fd:
        yield parent_fd, target.name


def _metadata_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimePathError(f"cannot inspect runtime file {name}: {exc}") from exc


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise RuntimePathError("runtime file write made no progress")
        offset += written


def _read_bounded(descriptor: int, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _validate_regular_metadata(
    path: Path, metadata: os.stat_result, *, max_bytes: int | None
) -> None:
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise RuntimePathError(
            f"runtime file is a symlink or non-regular file (including hard links): {path}"
        )
    if max_bytes is not None and metadata.st_size > max_bytes:
        raise RuntimePathError(
            f"runtime file is too large ({metadata.st_size} bytes; maximum {max_bytes}): {path}"
        )


def _safe_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {"", ".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )
