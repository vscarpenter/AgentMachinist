"""Safe file operations for repository paths managed by AgentMachinist.

Managed setup files live in a repository that may already contain attacker-
controlled symlinks or special files.  These helpers traverse every parent
with ``O_NOFOLLOW`` and perform writes relative to an opened directory, so a
setup or workflow-sync operation cannot be redirected outside the repository.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Iterator


class ManagedPathError(Exception):
    """A managed repository path is unsafe to read or mutate."""


def _relative_parts(relative: str | Path) -> tuple[str, ...]:
    path = PurePath(relative)
    if path.is_absolute() or not path.parts:
        raise ManagedPathError(f"managed path must be repository-relative: {path}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ManagedPathError(f"managed path escapes the repository: {path}")
    if any("\x00" in part for part in path.parts):
        raise ManagedPathError(f"managed path contains a NUL byte: {path}")
    return tuple(path.parts)


def _open_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _path_error(relative: str | Path, detail: str) -> ManagedPathError:
    return ManagedPathError(f"unsafe managed path '{relative}': {detail}")


@contextmanager
def _parent_directory(
    repo_root: Path, relative: str | Path, *, create: bool
) -> Iterator[tuple[int | None, str, str]]:
    """Open a managed file's real parent directory without following links.

    A ``None`` descriptor means a parent is absent and ``create`` was false.
    Every descriptor is opened relative to the previously verified descriptor,
    which also pins traversal against parent-directory replacement races.
    """
    parts = _relative_parts(relative)
    display = str(PurePath(*parts))
    root = Path(os.path.abspath(repo_root))
    try:
        root_fd = os.open(root, _open_flags(directory=True))
    except OSError as exc:
        raise _path_error(
            display, f"repository root is not a real directory ({exc})"
        ) from exc

    current_fd = root_fd
    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(
                    part,
                    _open_flags(directory=True),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                if not create:
                    yield None, parts[-1], display
                    return
                try:
                    os.mkdir(part, mode=0o777, dir_fd=current_fd)
                except FileExistsError:
                    # Another process created it.  The no-follow open below
                    # determines whether the new entry is safe.
                    pass
                except OSError as exc:
                    raise _path_error(
                        display, f"cannot create parent '{part}' ({exc})"
                    ) from exc
                try:
                    child_fd = os.open(
                        part,
                        _open_flags(directory=True),
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    raise _path_error(
                        display, f"parent '{part}' is not a real directory"
                    ) from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise _path_error(
                        display, f"parent '{part}' is a symlink or not a directory"
                    ) from exc
                raise _path_error(
                    display, f"cannot inspect parent '{part}' ({exc})"
                ) from exc

            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = child_fd

        yield current_fd, parts[-1], display
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _regular_metadata(parent_fd: int, name: str, display: str) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _path_error(display, f"cannot inspect target ({exc})") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise _path_error(display, "target is a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise _path_error(display, "target is not a regular file")
    return metadata


def managed_file_exists(repo_root: Path, relative: str | Path) -> bool:
    """Return whether a managed regular file exists, rejecting unsafe nodes."""
    with _parent_directory(repo_root, relative, create=False) as (
        parent_fd,
        name,
        display,
    ):
        if parent_fd is None:
            return False
        return _regular_metadata(parent_fd, name, display) is not None


def read_managed_text(
    repo_root: Path,
    relative: str | Path,
    *,
    max_bytes: int | None = None,
) -> str | None:
    """Read bounded UTF-8 text without following the target or its parents."""
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    with _parent_directory(repo_root, relative, create=False) as (
        parent_fd,
        name,
        display,
    ):
        if parent_fd is None:
            return None
        before = _regular_metadata(parent_fd, name, display)
        if before is None:
            return None
        try:
            descriptor = os.open(name, _open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise _path_error(display, f"cannot safely open target ({exc})") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise _path_error(display, "opened target is not a regular file")
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise _path_error(display, "target changed while it was being opened")
            if max_bytes is not None and opened.st_size > max_bytes:
                raise _path_error(display, f"target exceeds {max_bytes}-byte limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk_size = 64 * 1024
                if max_bytes is not None:
                    chunk_size = min(chunk_size, max_bytes + 1 - total)
                    if chunk_size <= 0:
                        raise _path_error(
                            display, f"target exceeds {max_bytes}-byte limit"
                        )
                chunk = os.read(descriptor, chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise _path_error(display, f"target exceeds {max_bytes}-byte limit")
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _path_error(display, "target is not valid UTF-8 text") from exc
        except OSError as exc:
            raise _path_error(display, f"cannot safely read target ({exc})") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def write_managed_text(repo_root: Path, relative: str | Path, content: str) -> None:
    """Atomically replace a managed text file inside verified real parents."""
    encoded = content.encode("utf-8")
    with _parent_directory(repo_root, relative, create=True) as (
        parent_fd,
        name,
        display,
    ):
        assert parent_fd is not None
        existing = _regular_metadata(parent_fd, name, display)
        temporary = f".{name}.machinist-{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o666, dir_fd=parent_fd)
            if existing is not None:
                os.fchmod(descriptor, stat.S_IMODE(existing.st_mode))
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise _path_error(display, "managed file write made no progress")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1

            # Reject a target that became unsafe during content generation.
            # A subsequent rename race still cannot follow or clobber a link:
            # os.replace operates on the entry in this pinned directory.
            _regular_metadata(parent_fd, name, display)
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except ManagedPathError:
            raise
        except OSError as exc:
            raise _path_error(
                display, f"cannot atomically write target ({exc})"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def remove_managed_file(repo_root: Path, relative: str | Path) -> bool:
    """Remove a managed regular file without following any symlinks."""
    with _parent_directory(repo_root, relative, create=False) as (
        parent_fd,
        name,
        display,
    ):
        if parent_fd is None or _regular_metadata(parent_fd, name, display) is None:
            return False
        try:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as exc:
            raise _path_error(display, f"cannot safely remove target ({exc})") from exc
        return True
