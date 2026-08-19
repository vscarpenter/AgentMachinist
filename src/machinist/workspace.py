"""Isolated per-task workspaces.

Each agent task gets its own checkout under workspace.root so the user's
working tree is never touched. The default strategy is a git worktree
(shares the object store); 'clone' makes a fully independent copy whose
origin points at the same remote.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

from machinist.config import CleanupPolicy, WorkspaceConfig, WorkspaceStrategy

Runner = Callable[..., subprocess.CompletedProcess]

_BOT_NAME = "AgentMachinist"
_BOT_EMAIL = "machinist@users.noreply.github.com"
_GIT_TIMEOUT_SECONDS = 300
_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40,64}")
_TARGET_BRANCH_MARKER = "agentmachinist-target-branch"
_START_SHA_MARKER = "agentmachinist-start-sha"


class WorkspaceError(Exception):
    """A git workspace operation failed."""


class Workspace:
    def __init__(
        self, repo_root: Path, config: WorkspaceConfig, runner: Runner = subprocess.run
    ):
        self.repo_root = repo_root
        self.config = config
        self._runner = runner

    def provision(
        self,
        task: str,
        branch: str,
        base_ref: str,
        *,
        attempt: int | None = None,
    ) -> Path:
        """Create a fresh Workshop at the exact remote Task head.

        The legacy path remains the default. Supplying ``attempt`` creates a
        distinct, detached Workshop so failed diagnostic state can be retained
        without reusing its local branch or working tree.
        """
        self._validate_task(task)
        self._validate_branch(branch)
        self._validate_attempt(attempt)
        root = self.config.resolved_root()
        root.mkdir(parents=True, exist_ok=True)
        path = self.workspace_for_task(task, attempt=attempt)
        if path.exists() or path.is_symlink():
            raise WorkspaceError(
                f"workspace {path} already exists; remove it "
                "(or 'git worktree remove' it) and retry"
            )
        self._git(self.repo_root, "fetch", "--no-tags", "origin")
        remote_head = self._fetch_remote_branch(branch)
        start_sha = remote_head or self._resolve_commit(self.repo_root, base_ref)

        if self.config.strategy is WorkspaceStrategy.WORKTREE:
            if attempt is not None:
                self._git(
                    self.repo_root, "worktree", "add", "--detach", str(path), start_sha
                )
            else:
                local_branch = f"refs/heads/{branch}"
                if self._branch_exists(self.repo_root, local_branch):
                    try:
                        # The remote (or base when no remote exists) is the
                        # authority. Never preserve a local-ahead/diverged Task
                        # branch as the starting point for another attempt.
                        self._git(self.repo_root, "branch", "-f", branch, start_sha)
                    except WorkspaceError as exc:
                        raise WorkspaceError(
                            f"local branch '{branch}' cannot be reset to {start_sha[:12]}; "
                            "it may be checked out in a retained workspace. "
                            "Provision an explicit attempt path instead."
                        ) from exc
                    self._git(self.repo_root, "worktree", "add", str(path), branch)
                else:
                    self._git(
                        self.repo_root,
                        "worktree",
                        "add",
                        str(path),
                        "-b",
                        branch,
                        start_sha,
                    )
        else:
            origin_url = self._git(
                self.repo_root, "remote", "get-url", "origin"
            ).strip()
            self._git(root, "clone", origin_url, str(path))
            if remote_head is not None:
                self._git(
                    path,
                    "fetch",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                )
            if attempt is not None:
                self._git(path, "checkout", "--detach", start_sha)
            else:
                self._git(path, "checkout", "-B", branch, start_sha)

        self._write_workspace_metadata(path, branch=branch, start_sha=start_sha)
        self.assert_head(path, start_sha)
        return path

    def commit_all(self, path: Path, message: str) -> None:
        self._git(path, "add", "-A")
        args = ["commit", "-m", message]
        if not self._has_identity(path):
            # Bare CI runners have no git identity configured.
            args = [
                "-c",
                f"user.name={_BOT_NAME}",
                "-c",
                f"user.email={_BOT_EMAIL}",
                *args,
            ]
        self._git(path, *args)

    def has_changes(self, path: Path) -> bool:
        return bool(self._git(path, "status", "--porcelain").strip())

    def push(self, path: Path, branch: str, *, expected_sha: str | None = None) -> None:
        self._validate_branch(branch)
        args = ["push", "-u"]
        if expected_sha is not None:
            self._validate_sha(expected_sha, label="expected push SHA")
            args.append(f"--force-with-lease=refs/heads/{branch}:{expected_sha}")
        # Publish the Workshop's actual HEAD. Explicit fresh attempts are
        # detached by design, and a stale local Task branch must never be used
        # as an implicit push source.
        push_env = self._ephemeral_push_environment()
        self._git(
            path,
            *args,
            "origin",
            f"HEAD:refs/heads/{branch}",
            env=push_env,
        )

    def head_sha(self, path: Path) -> str:
        return self._git(path, "rev-parse", "HEAD").strip()

    def remote_sha(self, path: Path, branch: str) -> str | None:
        self._validate_branch(branch)
        output = self._git(
            path, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
        )
        return output.split()[0] if output.strip() else None

    def current_branch(self, path: Path) -> str | None:
        branch = self._git(path, "branch", "--show-current").strip()
        return branch or None

    def assert_head(self, path: Path, expected_sha: str) -> None:
        self._validate_sha(expected_sha, label="expected HEAD")
        actual = self.head_sha(path)
        if actual.lower() != expected_sha.lower():
            raise WorkspaceError(
                f"workspace {path} expected HEAD {expected_sha}, found {actual}"
            )

    def assert_branch(self, path: Path, expected_branch: str) -> None:
        self._validate_branch(expected_branch)
        actual = self.current_branch(path) or self._read_target_branch(path)
        if actual != expected_branch:
            raise WorkspaceError(
                f"workspace {path} expected branch '{expected_branch}', "
                f"found '{actual or 'detached HEAD with no target'}'"
            )

    def changed_files(self, path: Path) -> list[str]:
        """Return all tracked and untracked paths changed from HEAD."""
        tracked = self._git(path, "diff", "--name-only", "-z", "HEAD").split("\0")
        untracked = self._git(
            path, "ls-files", "--others", "--exclude-standard", "-z"
        ).split("\0")
        return sorted({name for name in [*tracked, *untracked] if name})

    def change_snapshot(self, path: Path) -> str:
        """Return a deterministic fingerprint of all Git-visible changes.

        The digest includes staged/unstaged status, file modes and names, and
        the content of changed and untracked files. Ignored files are omitted,
        matching Git's normal change boundary.
        """
        status_output = self._git(
            path,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
        )
        digest = hashlib.sha256()

        def add_record(label: str, value: bytes) -> None:
            label_bytes = label.encode()
            digest.update(len(label_bytes).to_bytes(4, "big"))
            digest.update(label_bytes)
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)

        add_record("format", b"agentmachinist-change-snapshot-v1")
        add_record("status", status_output.encode(errors="surrogateescape"))
        for relative in self.changed_files(path):
            root_node = path / relative
            for node in self._snapshot_nodes(root_node):
                node_relative = node.relative_to(path).as_posix()
                add_record("path", node_relative.encode(errors="surrogateescape"))
                try:
                    mode = node.lstat().st_mode
                except FileNotFoundError:
                    add_record("kind", b"missing")
                    continue
                except OSError as exc:
                    raise WorkspaceError(
                        f"could not inspect changed path {node}: {exc}"
                    ) from exc
                add_record("mode", f"{mode:o}".encode())
                if stat.S_ISLNK(mode):
                    try:
                        target = os.readlink(node)
                    except OSError as exc:
                        raise WorkspaceError(
                            f"could not read changed symlink {node}: {exc}"
                        ) from exc
                    add_record("symlink", os.fsencode(target))
                elif stat.S_ISREG(mode):
                    try:
                        with node.open("rb") as stream:
                            while chunk := stream.read(1024 * 1024):
                                add_record("content", chunk)
                    except OSError as exc:
                        raise WorkspaceError(
                            f"could not read changed file {node}: {exc}"
                        ) from exc
        return digest.hexdigest()

    def path_changed(self, path: Path, relative: str) -> bool:
        return bool(self._git(path, "status", "--porcelain", "--", relative).strip())

    def workspace_for_task(self, task: str, *, attempt: int | None = None) -> Path:
        self._validate_task(task)
        self._validate_attempt(attempt)
        suffix = f"-{task}" if attempt is None else f"-{task}-attempt-{attempt}"
        return self.managed_path(
            self.config.resolved_root() / f"{self.repo_root.name}{suffix}"
        )

    def managed_path(self, path: Path) -> Path:
        """Resolve and validate a direct child managed by this repository."""
        root = self.config.resolved_root()
        raw_path = Path(path).expanduser()
        if raw_path.is_symlink():
            raise WorkspaceError(
                f"workspace path {path} is a symbolic link; refusing managed operation"
            )
        try:
            candidate = raw_path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(
                f"could not resolve workspace path {path}: {exc}"
            ) from exc
        prefix = f"{self.repo_root.name}-"
        if candidate.parent != root or not candidate.name.startswith(prefix):
            raise WorkspaceError(
                f"workspace path {path} is outside managed workspace root {root}"
            )
        return candidate

    def resume(self, path: Path, *, branch: str, expected_sha: str) -> Path:
        """Validate and return an existing Workshop without mutating it."""
        target = self.managed_path(path)
        if not target.exists() or not target.is_dir():
            raise WorkspaceError(f"workspace {target} does not exist")
        self._assert_owned_checkout(target)
        self.assert_branch(target, branch)
        self.assert_head(target, expected_sha)
        return target

    def list_workspaces(self) -> list[Path]:
        root = self.config.resolved_root()
        if not root.exists():
            return []
        prefix = f"{self.repo_root.name}-"
        return sorted(
            [p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix)]
        )

    def list_task_workspaces(self, task: str) -> list[Path]:
        base = self.workspace_for_task(task)
        prefix = f"{base.name}-attempt-"
        return [
            path
            for path in self.list_workspaces()
            if path == base or path.name.startswith(prefix)
        ]

    def remove_workspace(self, path: Path, *, force: bool = False) -> None:
        target = self.managed_path(path)
        if not target.exists() and not target.is_symlink():
            return

        if self.config.strategy is WorkspaceStrategy.WORKTREE:
            if not self._registered_worktree(target):
                if force:
                    self._remove_tree(target)
                    return
                if target.is_dir() and not any(target.iterdir()):
                    target.rmdir()
                    return
                raise WorkspaceError(
                    f"workspace {target} is not a registered clean worktree; "
                    "refusing removal without force"
                )
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            elif self.current_branch(target) is None and self._has_unpublished_commit(
                target
            ):
                raise WorkspaceError(
                    f"workspace {target} has unpushed commits on detached HEAD; "
                    "refusing removal without force"
                )
            args.append(str(target))
            try:
                self._git(self.repo_root, *args)
            except WorkspaceError as exc:
                if not force:
                    raise WorkspaceError(
                        f"workspace {target} has uncommitted changes or cannot be safely "
                        "removed; inspect it and rerun with force only if disposable"
                    ) from exc
                if target.exists():
                    self._remove_tree(target)
            try:
                self._git(self.repo_root, "worktree", "prune")
            except WorkspaceError:
                pass
        else:
            if not force:
                self._assert_owned_checkout(target)
                if self.has_changes(target):
                    raise WorkspaceError(
                        f"workspace {target} has uncommitted changes; inspect it and "
                        "rerun with force only if disposable"
                    )
                if self._has_unpublished_commit(target):
                    raise WorkspaceError(
                        f"workspace {target} has unpushed commits; inspect it and "
                        "rerun with force only if disposable"
                    )
            self._remove_tree(target)

    def cleanup(self, path: Path, *, success: bool) -> None:
        policy = self.config.cleanup
        if policy is CleanupPolicy.NEVER:
            return
        if policy is CleanupPolicy.ON_SUCCESS and not success:
            return
        self.remove_workspace(path, force=True)

    def _git(
        self,
        cwd: Path,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> str:
        result = self._run(cwd, *args, env=env)
        if result.returncode != 0:
            raise WorkspaceError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def _run(
        self,
        cwd: Path,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            kwargs = {
                "cwd": cwd,
                "capture_output": True,
                "text": True,
                "timeout": _GIT_TIMEOUT_SECONDS,
            }
            if env is not None:
                kwargs["env"] = env
            return self._runner(["git", *args], **kwargs)
        except FileNotFoundError as exc:
            raise WorkspaceError("git not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            operation = args[0] if args else "command"
            raise WorkspaceError(
                f"git {operation} timed out after {_GIT_TIMEOUT_SECONDS} seconds"
            ) from exc

    def _ephemeral_push_environment(self) -> dict[str, str] | None:
        """Use an Actions token for only the controller-owned push subprocess."""
        token = os.environ.get("GH_TOKEN")
        if not token:
            return None
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment = os.environ.copy()
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        environment["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded}"
        return environment

    def _branch_exists(self, cwd: Path, ref: str) -> bool:
        return self._run(cwd, "rev-parse", "--verify", "--quiet", ref).returncode == 0

    def _has_identity(self, cwd: Path) -> bool:
        result = self._run(cwd, "config", "user.email")
        return result.returncode == 0 and bool(result.stdout.strip())

    def _fetch_remote_branch(self, branch: str) -> str | None:
        remote_head = self.remote_sha(self.repo_root, branch)
        if remote_head is None:
            return None
        remote_ref = f"refs/remotes/origin/{branch}"
        self._git(
            self.repo_root,
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:{remote_ref}",
        )
        return self._resolve_commit(self.repo_root, remote_ref)

    def _resolve_commit(self, cwd: Path, ref: str) -> str:
        return self._git(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def _validate_task(self, task: str) -> None:
        if not _TASK_NAME.fullmatch(task) or ".." in task:
            raise WorkspaceError(
                "task name must use 1-128 letters, digits, '.', '_', or '-' "
                "without '..'"
            )

    def _validate_attempt(self, attempt: int | None) -> None:
        if attempt is not None and (
            not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1
        ):
            raise WorkspaceError("attempt must be a positive integer")

    def _validate_branch(self, branch: str) -> None:
        if not branch or branch.startswith("-"):
            raise WorkspaceError(f"invalid Task branch '{branch}'")
        result = self._run(self.repo_root, "check-ref-format", f"refs/heads/{branch}")
        if result.returncode != 0:
            raise WorkspaceError(f"invalid Task branch '{branch}'")

    def _validate_sha(self, sha: str, *, label: str) -> None:
        if not _FULL_SHA.fullmatch(sha):
            raise WorkspaceError(f"{label} must be a full Git SHA")

    def _write_workspace_metadata(
        self, path: Path, *, branch: str, start_sha: str
    ) -> None:
        self._write_git_marker(path, _TARGET_BRANCH_MARKER, branch)
        self._write_git_marker(path, _START_SHA_MARKER, start_sha)

    def _read_target_branch(self, path: Path) -> str | None:
        return self._read_git_marker(path, _TARGET_BRANCH_MARKER)

    def _read_start_sha(self, path: Path) -> str | None:
        return self._read_git_marker(path, _START_SHA_MARKER)

    def _write_git_marker(self, path: Path, name: str, value: str) -> None:
        marker = Path(self._git(path, "rev-parse", "--git-path", name).strip())
        if not marker.is_absolute():
            marker = path / marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(value + "\n")

    def _read_git_marker(self, path: Path, name: str) -> str | None:
        marker = Path(self._git(path, "rev-parse", "--git-path", name).strip())
        if not marker.is_absolute():
            marker = path / marker
        if not marker.is_file():
            return None
        return marker.read_text().strip() or None

    def _has_unpublished_commit(self, path: Path) -> bool:
        head = self.head_sha(path)
        if head == self._read_start_sha(path):
            return False
        target_branch = self._read_target_branch(path)
        if target_branch is None:
            return True
        return self.remote_sha(path, target_branch) != head

    def _common_git_dir(self, path: Path) -> Path:
        raw = Path(self._git(path, "rev-parse", "--git-common-dir").strip())
        return raw.resolve() if raw.is_absolute() else (path / raw).resolve()

    def _assert_owned_checkout(self, path: Path) -> None:
        if self.config.strategy is WorkspaceStrategy.WORKTREE:
            if self._common_git_dir(path) != self._common_git_dir(self.repo_root):
                raise WorkspaceError(
                    f"workspace {path} is not a worktree of {self.repo_root}"
                )
            return
        expected = self._git(self.repo_root, "remote", "get-url", "origin").strip()
        actual = self._git(path, "remote", "get-url", "origin").strip()
        if actual != expected:
            raise WorkspaceError(
                f"workspace {path} origin does not match controller repository"
            )

    def _registered_worktree(self, path: Path) -> bool:
        output = self._git(self.repo_root, "worktree", "list", "--porcelain")
        for line in output.splitlines():
            if not line.startswith("worktree "):
                continue
            registered = Path(line.removeprefix("worktree ")).resolve()
            if registered == path:
                return True
        return False

    def _snapshot_nodes(self, root: Path) -> Iterator[Path]:
        """Yield a changed node tree without following links or Git metadata."""
        yield root
        try:
            if root.is_symlink() or not root.is_dir():
                return
            children = sorted(root.iterdir(), key=lambda child: child.name)
        except OSError as exc:
            raise WorkspaceError(
                f"could not inspect changed path {root}: {exc}"
            ) from exc
        for child in children:
            if child.name == ".git":
                continue
            yield from self._snapshot_nodes(child)

    def _remove_tree(self, path: Path) -> None:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise WorkspaceError(f"could not remove workspace {path}: {exc}") from exc
        if path.exists():
            raise WorkspaceError(f"workspace {path} still exists after removal")
