"""Isolated per-task workspaces.

Each agent task gets its own checkout under workspace.root so the user's
working tree is never touched. The default strategy is a git worktree
(shares the object store); 'clone' makes a fully independent copy whose
origin points at the same remote.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from machinist.config import CleanupPolicy, WorkspaceConfig, WorkspaceStrategy
from machinist.github import normalize_repository_identity
from machinist.process import credential_reduced_environment

Runner = Callable[..., subprocess.CompletedProcess]

_BOT_NAME = "AgentMachinist"
_BOT_EMAIL = "machinist@users.noreply.github.com"
_GIT_TIMEOUT_SECONDS = 300
_AUTH_TIMEOUT_SECONDS = 10
_MAX_AUTH_TOKEN_CHARS = 16_384
_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40,64}")
_TARGET_BRANCH_MARKER = "agentmachinist-target-branch"
_START_SHA_MARKER = "agentmachinist-start-sha"
_OWNER_MARKER = "agentmachinist-owner.json"
_OWNER_SCHEMA_VERSION = 1
_CUSTODY_VERSION = 1
_MAX_CUSTODY_METADATA_FILE_BYTES = 1024 * 1024
_MAX_CUSTODY_METADATA_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_CUSTODY_METADATA_ENTRIES = 4096
_MAX_CUSTODY_METADATA_DEPTH = 64
_SAFE_GIT_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.askPass=/usr/bin/false",
    "core.sshCommand=ssh",
    "credential.helper=",
    "credential.interactive=never",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "fetch.recurseSubmodules=false",
    "push.recurseSubmodules=no",
    "submodule.recurse=false",
    "protocol.ext.allow=never",
)


@dataclass
class _MetadataFingerprintBudget:
    entries: int = 0
    total_bytes: int = 0

    def consume_entry(self, node: Path) -> None:
        self.entries += 1
        if self.entries > _MAX_CUSTODY_METADATA_ENTRIES:
            raise WorkspaceError(
                "Git metadata exceeds the custody entry limit "
                f"({_MAX_CUSTODY_METADATA_ENTRIES}): {node}"
            )

    def consume_bytes(self, node: Path, size: int) -> None:
        if size > _MAX_CUSTODY_METADATA_FILE_BYTES:
            raise WorkspaceError(
                "Git metadata exceeds the per-file custody limit "
                f"({_MAX_CUSTODY_METADATA_FILE_BYTES} bytes): {node}"
            )
        if self.total_bytes + size > _MAX_CUSTODY_METADATA_TOTAL_BYTES:
            raise WorkspaceError(
                "Git metadata exceeds the aggregate custody limit "
                f"({_MAX_CUSTODY_METADATA_TOTAL_BYTES} bytes): {node}"
            )
        self.total_bytes += size


@dataclass(frozen=True)
class _PreviewClaim:
    sidecar: Path
    payload: bytes
    sidecar_device: int
    sidecar_inode: int
    target_device: int
    target_inode: int


class WorkspaceError(Exception):
    """A git workspace operation failed."""


class Workspace:
    def __init__(
        self,
        repo_root: Path,
        config: WorkspaceConfig,
        runner: Runner = subprocess.run,
        auth_runner: Runner = subprocess.run,
    ):
        self.repo_root = repo_root
        self.config = config
        self._runner = runner
        self._auth_runner = auth_runner
        self._origin_url: str | None = None
        self._custody: dict[Path, dict[str, object]] = {}
        self._github_tokens: dict[str, str] = {}
        self._preview_claims: dict[Path, _PreviewClaim] = {}

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
        origin_url = self._bind_controller_origin()
        self._git(
            self.repo_root,
            "fetch",
            "--no-tags",
            origin_url,
            "+refs/heads/*:refs/remotes/origin/*",
            env=self._ephemeral_network_environment(origin_url),
        )
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
            self._git(
                root,
                "clone",
                "--origin",
                "origin",
                origin_url,
                str(path),
                env=self._ephemeral_network_environment(origin_url),
            )
            if remote_head is not None:
                self._git(
                    path,
                    "fetch",
                    "--no-tags",
                    origin_url,
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                    env=self._ephemeral_network_environment(origin_url),
                )
            if attempt is not None:
                self._git(path, "checkout", "--detach", start_sha)
            else:
                self._git(path, "checkout", "-B", branch, start_sha)

        self._write_workspace_metadata(path, branch=branch, start_sha=start_sha)
        self.capture_git_custody(path)
        self.assert_head(path, start_sha)
        return path

    def provision_preview(self, task: str, branch: str, base_ref: str) -> Path:
        """Create an ephemeral detached clone without changing controller refs."""
        self._validate_task(task)
        self._validate_branch(branch)
        root = self.config.resolved_root()
        root.mkdir(parents=True, exist_ok=True)
        path = self.workspace_for_task(task)
        if path.exists() or path.is_symlink():
            raise WorkspaceError(
                f"workspace {path} already exists; remove it and retry"
            )
        origin_url = self._bind_controller_origin()
        self._reserve_preview(path)
        try:
            self._git(
                root,
                "clone",
                "--no-checkout",
                "--origin",
                "origin",
                origin_url,
                str(path),
                env=self._ephemeral_network_environment(origin_url),
            )
            remote_branch = f"refs/remotes/origin/{branch}"
            start_ref = (
                remote_branch if self._branch_exists(path, remote_branch) else base_ref
            )
            start_sha = self._resolve_commit(path, start_ref)
            self._git(path, "checkout", "--detach", start_sha)
            self._write_workspace_metadata(
                path,
                branch=branch,
                start_sha=start_sha,
                kind="preview",
            )
            self.capture_git_custody(path, standalone=True)
            self.assert_head(path, start_sha)
            return path
        except Exception:
            self.cleanup_preview(path)
            raise

    def cleanup_preview(self, path: Path) -> None:
        """Always remove an ephemeral preview clone, independent of policy."""
        target = self._preview_target(path)
        claim = self._preview_claims.get(target)
        if claim is None:
            raise WorkspaceError(
                f"preview workspace {target} has no live controller ownership claim"
            )
        self._assert_preview_claim(target, claim)
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            self._remove_preview_sidecar(target, claim)
            return
        except OSError as exc:
            raise WorkspaceError(
                f"could not inspect preview workspace {target}: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != claim.target_device
            or metadata.st_ino != claim.target_inode
        ):
            raise WorkspaceError(
                f"preview workspace {target} no longer matches its ownership claim"
            )
        self._custody.pop(target, None)
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise WorkspaceError(
                f"could not remove preview workspace {target}: {exc}"
            ) from exc
        if target.exists() or target.is_symlink():
            raise WorkspaceError(
                f"preview workspace {target} still exists after removal"
            )
        self._remove_preview_sidecar(target, claim)

    def _reserve_preview(self, path: Path) -> None:
        """Atomically claim a preview path before Git can create content in it."""
        target = self._preview_target(path)
        if target.exists() or target.is_symlink():
            raise WorkspaceError(f"preview workspace {target} already exists")
        claim_key = hashlib.sha256(str(target).encode()).hexdigest()
        sidecar = target.parent / f".agentmachinist-preview-{claim_key}.json"
        record = self._owner_record(kind="preview")
        record.update(
            {
                "target": target.name,
                "nonce": secrets.token_hex(32),
            }
        )
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(sidecar, flags, 0o600)
        except FileExistsError as exc:
            raise WorkspaceError(
                f"preview workspace {target} already has an ownership claim"
            ) from exc
        except OSError as exc:
            raise WorkspaceError(
                f"could not create preview ownership claim {sidecar}: {exc}"
            ) from exc

        sidecar_metadata = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            target.mkdir(mode=0o700)
            target_metadata = target.lstat()
            if not stat.S_ISDIR(target_metadata.st_mode):
                raise WorkspaceError(
                    f"preview workspace reservation is not a directory: {target}"
                )
        except Exception:
            self._unlink_if_identity(
                sidecar,
                device=sidecar_metadata.st_dev,
                inode=sidecar_metadata.st_ino,
            )
            raise

        self._preview_claims[target] = _PreviewClaim(
            sidecar=sidecar,
            payload=payload,
            sidecar_device=sidecar_metadata.st_dev,
            sidecar_inode=sidecar_metadata.st_ino,
            target_device=target_metadata.st_dev,
            target_inode=target_metadata.st_ino,
        )

    def _preview_target(self, path: Path) -> Path:
        raw = Path(path).expanduser()
        root = self.config.resolved_root()
        try:
            parent = raw.parent.resolve()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(
                f"could not resolve preview workspace parent {raw.parent}: {exc}"
            ) from exc
        prefix = f"{self.repo_root.name}-preview-"
        if parent != root or not raw.name.startswith(prefix):
            raise WorkspaceError(
                f"preview workspace {path} is outside managed workspace root {root}"
            )
        return parent / raw.name

    def _assert_preview_claim(self, target: Path, claim: _PreviewClaim) -> None:
        sidecar = claim.sidecar
        try:
            metadata = sidecar.lstat()
        except OSError as exc:
            raise WorkspaceError(
                f"could not inspect preview ownership claim {sidecar}: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != claim.sidecar_device
            or metadata.st_ino != claim.sidecar_inode
            or metadata.st_size != len(claim.payload)
        ):
            raise WorkspaceError(
                f"preview workspace {target} has an invalid ownership claim"
            )
        try:
            descriptor = os.open(
                sidecar,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    opened.st_dev != claim.sidecar_device
                    or opened.st_ino != claim.sidecar_inode
                ):
                    raise WorkspaceError(
                        f"preview ownership claim changed during inspection: {sidecar}"
                    )
                payload = stream.read(len(claim.payload) + 1)
        except OSError as exc:
            raise WorkspaceError(
                f"could not read preview ownership claim {sidecar}: {exc}"
            ) from exc
        if payload != claim.payload:
            raise WorkspaceError(
                f"preview workspace {target} has an invalid ownership claim"
            )

    def _remove_preview_sidecar(self, target: Path, claim: _PreviewClaim) -> None:
        self._assert_preview_claim(target, claim)
        try:
            claim.sidecar.unlink()
        except OSError as exc:
            raise WorkspaceError(
                f"could not remove preview ownership claim {claim.sidecar}: {exc}"
            ) from exc
        self._preview_claims.pop(target, None)

    @staticmethod
    def _unlink_if_identity(path: Path, *, device: int, inode: int) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if metadata.st_dev == device and metadata.st_ino == inode:
            try:
                path.unlink()
            except OSError:
                pass

    def commit_all(self, path: Path, message: str) -> None:
        self._assert_bound_custody(path)
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
        self._assert_bound_custody(path)

    def has_changes(self, path: Path) -> bool:
        self._assert_bound_custody(path)
        return bool(self._git(path, "status", "--porcelain").strip())

    def push(self, path: Path, branch: str, *, expected_sha: str | None = None) -> None:
        self._validate_branch(branch)
        self._assert_bound_custody(path)
        origin_url = self._origin_for(path)
        args = ["push"]
        if expected_sha is not None:
            self._validate_sha(expected_sha, label="expected push SHA")
            args.append(f"--force-with-lease=refs/heads/{branch}:{expected_sha}")
        # Publish the Workshop's actual HEAD. Explicit fresh attempts are
        # detached by design, and a stale local Task branch must never be used
        # as an implicit push source.
        push_env = self._ephemeral_network_environment(origin_url)
        self._git(
            path,
            *args,
            origin_url,
            f"HEAD:refs/heads/{branch}",
            env=push_env,
        )
        self._assert_bound_custody(path)

    def head_sha(self, path: Path) -> str:
        self._assert_bound_custody(path)
        return self._git(path, "rev-parse", "HEAD").strip()

    def remote_sha(self, path: Path, branch: str) -> str | None:
        self._validate_branch(branch)
        self._assert_bound_custody(path)
        origin_url = self._origin_for(path)
        output = self._git(
            path,
            "ls-remote",
            "--heads",
            origin_url,
            f"refs/heads/{branch}",
            env=self._ephemeral_network_environment(origin_url),
        )
        return output.split()[0] if output.strip() else None

    def current_branch(self, path: Path) -> str | None:
        self._assert_bound_custody(path)
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
        self._assert_bound_custody(path)
        tracked = self._git(
            path, "diff", "--no-ext-diff", "--name-only", "-z", "HEAD"
        ).split("\0")
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
        self._assert_bound_custody(path)
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
        self._assert_bound_custody(path)
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
        self._assert_bound_custody(target)
        self._assert_owned_checkout(target)
        self.assert_branch(target, branch)
        self.assert_head(target, expected_sha)
        return target

    def list_workspaces(self) -> list[Path]:
        root = self.config.resolved_root()
        if not root.exists():
            return []
        prefix = f"{self.repo_root.name}-"
        owned: list[Path] = []
        for path in root.iterdir():
            if not path.is_dir() or not path.name.startswith(prefix):
                continue
            if self._workspace_owned(path):
                owned.append(path.resolve())
        return sorted(owned)

    def list_task_workspaces(self, task: str) -> list[Path]:
        base = self.workspace_for_task(task)
        prefix = f"{base.name}-attempt-"
        owned = [
            path
            for path in self.list_workspaces()
            if path == base or path.name.startswith(prefix)
        ]
        return sorted(owned)

    def remove_workspace(self, path: Path, *, force: bool = False) -> None:
        target = self.managed_path(path)
        if not target.exists() and not target.is_symlink():
            return
        if not self._workspace_owned(target):
            raise WorkspaceError(
                f"workspace {target} has no valid controller ownership marker"
            )
        self._assert_bound_custody(target)

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
            command = ["git"]
            for setting in _SAFE_GIT_CONFIG:
                command.extend(("-c", setting))
            command.extend(args)
            kwargs = {
                "cwd": cwd,
                "capture_output": True,
                "text": True,
                "timeout": _GIT_TIMEOUT_SECONDS,
                "env": self._controller_git_environment(env),
            }
            return self._runner(command, **kwargs)
        except FileNotFoundError as exc:
            raise WorkspaceError("git not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            operation = args[0] if args else "command"
            raise WorkspaceError(
                f"git {operation} timed out after {_GIT_TIMEOUT_SECONDS} seconds"
            ) from exc

    def _ephemeral_push_environment(self) -> dict[str, str] | None:
        """Use an Actions token for only the controller-owned push subprocess."""
        return self._ephemeral_network_environment("https://github.com/")

    def _ephemeral_network_environment(self, origin_url: str) -> dict[str, str] | None:
        """Authorize a controller-owned GitHub network operation in memory only."""
        scope = self._github_header_scope(origin_url)
        if scope is None:
            return None
        authority = urlsplit(scope).netloc
        token = None
        if authority == "github.com":
            token = os.environ.get("GH_TOKEN")
        elif authority == self._normalized_gh_host(os.environ.get("GH_HOST")):
            token = os.environ.get("GH_ENTERPRISE_TOKEN")
        token = token or self._github_auth_token(authority)
        if not token:
            return None
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment = self._controller_git_environment()
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = f"http.{scope}.extraheader"
        environment["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded}"
        return environment

    @staticmethod
    def _github_header_scope(origin_url: str) -> str | None:
        try:
            parsed = urlsplit(origin_url)
        except ValueError:
            return None
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return None
        authority = parsed.hostname.lower()
        try:
            port = parsed.port
        except ValueError:
            return None
        if port not in (None, 443):
            authority += f":{port}"
        allowed_hosts = {"github.com"}
        configured_host = Workspace._normalized_gh_host(os.environ.get("GH_HOST"))
        if configured_host is not None:
            allowed_hosts.add(configured_host)
        if authority not in allowed_hosts:
            return None
        return f"https://{authority}/"

    @staticmethod
    def _normalized_gh_host(raw_host: str | None) -> str | None:
        if not raw_host or not raw_host.strip():
            return None
        supplied = raw_host.strip().lower()
        try:
            parsed = urlsplit(supplied if "://" in supplied else f"//{supplied}")
        except ValueError:
            return None
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return None
        authority = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            return None
        if port not in (None, 443):
            authority += f":{port}"
        return authority

    def _github_auth_token(self, authority: str) -> str | None:
        """Read the authenticated gh token without persisting or logging it."""
        cached = self._github_tokens.get(authority)
        if cached is not None:
            return cached
        environment = credential_reduced_environment()
        environment.update(
            {
                "GH_PROMPT_DISABLED": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        try:
            completed = self._auth_runner(
                ["gh", "auth", "token", "--hostname", authority],
                capture_output=True,
                text=True,
                timeout=_AUTH_TIMEOUT_SECONDS,
                env=environment,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if completed.returncode != 0:
            return None
        token = completed.stdout.strip()
        if (
            not token
            or len(token) > _MAX_AUTH_TOKEN_CHARS
            or any(character.isspace() for character in token)
            or any(character in token for character in ("\0", "\r", "\n"))
        ):
            return None
        self._github_tokens[authority] = token
        return token

    def _controller_git_environment(
        self, source: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Build the minimal non-interactive environment for controller Git.

        Ambient Git variables can redirect the repository, object database,
        executable helpers, config stack, or trace credentials. Strip all of
        them and then add back only fixed controller policy. SSH agent access
        is retained because it is a credential specifically related to an SSH
        Git remote; unrelated controller and cloud credentials remain absent.
        """
        raw = os.environ if source is None else source
        environment = credential_reduced_environment(
            raw,
            allow=("SSH_AUTH_SOCK",),
        )
        for name in tuple(environment):
            if name.upper().startswith("GIT_"):
                environment.pop(name, None)
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "/usr/bin/false",
                "SSH_ASKPASS": "/usr/bin/false",
                "SSH_ASKPASS_REQUIRE": "never",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
            }
        )

        # ``env`` is used only for the short-lived Actions push credential.
        # Preserve it after the ambient Git-variable purge when it has exactly
        # the shape created by ``_ephemeral_push_environment``.
        if source is not None and self._valid_ephemeral_auth_config(source):
            for name in (
                "GIT_CONFIG_COUNT",
                "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0",
            ):
                environment[name] = source[name]
        return environment

    @staticmethod
    def _valid_ephemeral_auth_config(source: Mapping[str, str]) -> bool:
        key = source.get("GIT_CONFIG_KEY_0", "")
        return (
            source.get("GIT_CONFIG_COUNT") == "1"
            and key.startswith("http.https://")
            and key.endswith("/.extraheader")
            and source.get("GIT_CONFIG_VALUE_0", "").startswith("AUTHORIZATION: basic ")
        )

    def capture_git_custody(
        self, path: Path, *, standalone: bool = False
    ) -> dict[str, object]:
        """Capture controller-owned Git metadata before an untrusted phase.

        The checkpoint is deliberately JSON-safe so lifecycle evidence can
        bind a retained workspace across processes. It contains hashes rather
        than config contents, avoiding credential disclosure in run records.
        """
        target = Path(path).resolve()
        origin_url = self._bind_controller_origin()
        git_dir, common_dir, git_entry = self._resolve_git_layout_raw(target)
        controller_common = self._resolve_git_layout_raw(self.repo_root)[1]
        if (
            not standalone
            and self.config.strategy is WorkspaceStrategy.WORKTREE
            and common_dir != controller_common
        ):
            raise WorkspaceError(
                f"workspace {target} is not a worktree of {self.repo_root}"
            )

        effective_config = self._effective_local_config(target)
        watched: dict[str, bool] = {}

        def watch(node: Path, *, recursive: bool = False) -> None:
            key = str(node.absolute())
            watched[key] = watched.get(key, False) or recursive
            if node.is_symlink():
                try:
                    resolved = node.resolve(strict=False)
                except (OSError, RuntimeError) as exc:
                    raise WorkspaceError(
                        f"could not resolve Git metadata path {node}: {exc}"
                    ) from exc
                watched[str(resolved)] = watched.get(str(resolved), False)

        watch(git_entry)
        watch(git_dir / "commondir")
        watch(git_dir / _TARGET_BRANCH_MARKER)
        watch(git_dir / _START_SHA_MARKER)
        watch(git_dir / _OWNER_MARKER)
        for node in (
            common_dir / "config",
            common_dir / "config.worktree",
            git_dir / "config.worktree",
            common_dir / "info" / "attributes",
            common_dir / "info" / "exclude",
            common_dir / "info" / "grafts",
            common_dir / "objects" / "info" / "alternates",
            common_dir / "shallow",
        ):
            watch(node)
        watch(common_dir / "hooks", recursive=True)
        watch(common_dir / "refs" / "replace", recursive=True)
        for origin_path in self._config_origin_paths(effective_config, target):
            watch(origin_path)

        fingerprint_budget = _MetadataFingerprintBudget()
        records = [
            {
                "path": name,
                "recursive": recursive,
                "digest": self._fingerprint_metadata(
                    Path(name),
                    recursive=recursive,
                    budget=fingerprint_budget,
                ),
            }
            for name, recursive in sorted(watched.items())
        ]
        token: dict[str, object] = {
            "version": _CUSTODY_VERSION,
            "workspace": str(target),
            "git_dir": str(git_dir),
            "common_git_dir": str(common_dir),
            "origin_identity_sha256": self._origin_identity(origin_url),
            "origin_display": self._redact_origin(origin_url),
            "effective_config_sha256": hashlib.sha256(
                effective_config.encode(errors="surrogateescape")
            ).hexdigest(),
            "watched": records,
        }
        self._custody[target] = token
        return token

    def repository_identity(self) -> str:
        """Return the canonical GitHub owner/repo bound to controller origin."""
        return self.repository_target()[1]

    def repository_target(self) -> tuple[str, str]:
        """Return the exact GitHub host and owner/repo bound to origin."""
        target = github_repository_target(self._bind_controller_origin())
        if target is None:
            raise WorkspaceError(
                "controller origin is not a recognized GitHub HTTPS or SSH repository"
            )
        return target

    def assert_git_custody(
        self, path: Path, expected: Mapping[str, object] | None = None
    ) -> None:
        """Verify raw Git metadata before invoking Git in a retained checkout."""
        target = Path(path).resolve()
        token = dict(expected) if expected is not None else self._custody.get(target)
        if token is None:
            raise WorkspaceError(
                f"workspace {target} has no controller Git-custody checkpoint"
            )
        self._validate_custody_token(target, token)

        # This raw filesystem comparison intentionally happens before the
        # first repository Git subprocess. It catches a replaced .git pointer,
        # planted hook, fsmonitor/filter config, included config, or origin
        # rewrite without giving that metadata a chance to execute.
        git_dir, common_dir, _git_entry = self._resolve_git_layout_raw(target)
        if (
            str(git_dir) != token["git_dir"]
            or str(common_dir) != token["common_git_dir"]
        ):
            raise WorkspaceError(
                f"workspace {target} Git directory identity changed during an "
                "untrusted phase"
            )
        watched = token["watched"]
        assert isinstance(watched, list)  # narrowed by _validate_custody_token
        fingerprint_budget = _MetadataFingerprintBudget()
        for record in watched:
            assert isinstance(record, dict)
            node = Path(str(record["path"]))
            actual = self._fingerprint_metadata(
                node,
                recursive=bool(record["recursive"]),
                budget=fingerprint_budget,
            )
            if actual != record["digest"]:
                raise WorkspaceError(
                    "controller-owned Git metadata changed during an untrusted "
                    f"phase: {node}"
                )

        expected_identity = str(token["origin_identity_sha256"])
        if (
            self._origin_url is not None
            and self._origin_identity(self._origin_url) != expected_identity
        ):
            raise WorkspaceError("workspace origin identity does not match controller")

        effective = self._effective_local_config(target)
        effective_digest = hashlib.sha256(
            effective.encode(errors="surrogateescape")
        ).hexdigest()
        if effective_digest != token["effective_config_sha256"]:
            raise WorkspaceError(
                "effective local Git config changed during an untrusted phase"
            )
        actual_origin = self._git(target, "remote", "get-url", "origin").strip()
        self._validate_origin_url(actual_origin)
        if self._origin_identity(actual_origin) != expected_identity:
            raise WorkspaceError(
                f"workspace {target} origin does not match its custody checkpoint"
            )
        self._origin_url = actual_origin
        self._custody[target] = token

    def _assert_bound_custody(self, path: Path) -> None:
        token = self._custody.get(Path(path).resolve())
        if token is not None:
            self.assert_git_custody(path, token)

    def _bind_controller_origin(self) -> str:
        if self._origin_url is not None:
            return self._origin_url
        origin = self._git(self.repo_root, "remote", "get-url", "origin").strip()
        self._validate_origin_url(origin)
        self._origin_url = origin
        return origin

    def _origin_for(self, _path: Path) -> str:
        return self._bind_controller_origin()

    @staticmethod
    def _validate_origin_url(origin: str) -> None:
        if (
            not origin
            or origin.startswith("-")
            or origin.startswith("ext::")
            or any(character in origin for character in ("\0", "\n", "\r"))
        ):
            raise WorkspaceError("origin URL is empty or uses an unsafe Git transport")

    @staticmethod
    def _origin_identity(origin: str) -> str:
        return hashlib.sha256(origin.encode("utf-8")).hexdigest()

    @staticmethod
    def _redact_origin(origin: str) -> str:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return "<redacted origin>"
        if parsed.scheme and parsed.hostname:
            host = parsed.hostname
            try:
                port = parsed.port
            except ValueError:
                return "<redacted origin>"
            if port is not None:
                host += f":{port}"
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        if ":" in origin and "@" in origin.split(":", 1)[0]:
            authority, path = origin.split(":", 1)
            return f"{authority.rsplit('@', 1)[-1]}:{path}"
        return origin

    @classmethod
    def _github_repository_target(cls, origin: str) -> tuple[str, str] | None:
        if "://" not in origin:
            scp = re.fullmatch(
                r"(?:[^@\s/:]+@)?(?P<host>[^/\s:]+):(?P<path>[^?#]+)",
                origin,
            )
            if scp is not None:
                authority = cls._allowed_github_authority(
                    scp.group("host"), port=None, scheme="ssh"
                )
                identity = cls._identity_from_remote_path(scp.group("path"))
                if authority is not None and identity is not None:
                    return authority, identity
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return None
        if (
            parsed.scheme.lower() not in {"https", "ssh"}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        authority = cls._allowed_github_authority(
            parsed.hostname,
            port=port,
            scheme=parsed.scheme.lower(),
        )
        identity = cls._identity_from_remote_path(parsed.path)
        if authority is None or identity is None:
            return None
        return authority, identity

    @staticmethod
    def _identity_from_remote_path(path: str) -> str | None:
        if "%" in path:
            return None
        return normalize_repository_identity(path.strip("/"))

    @classmethod
    def _allowed_github_authority(
        cls, hostname: str, *, port: int | None, scheme: str
    ) -> str | None:
        default_port = 443 if scheme == "https" else 22
        authority = hostname.casefold()
        if port not in (None, default_port):
            authority += f":{port}"
        allowed = {"github.com"}
        configured = cls._normalized_gh_host(os.environ.get("GH_HOST"))
        if configured is not None:
            allowed.add(configured.casefold())
        return authority if authority in allowed else None

    def _effective_local_config(self, path: Path) -> str:
        return self._git(
            path,
            "config",
            "--local",
            "--includes",
            "--show-origin",
            "--null",
            "--list",
        )

    @staticmethod
    def _config_origin_paths(config_output: str, cwd: Path) -> set[Path]:
        parts = config_output.split("\0")
        origins: set[Path] = set()
        for index in range(0, len(parts) - 1, 2):
            raw = parts[index]
            if not raw.startswith("file:"):
                continue
            node = Path(raw.removeprefix("file:"))
            if not node.is_absolute():
                node = cwd / node
            origins.add(node.absolute())
        return origins

    @staticmethod
    def _resolve_git_layout_raw(path: Path) -> tuple[Path, Path, Path]:
        entry = path / ".git"
        try:
            entry_mode = entry.lstat().st_mode
        except OSError as exc:
            raise WorkspaceError(f"could not inspect Git entry {entry}: {exc}") from exc
        if stat.S_ISLNK(entry_mode):
            raise WorkspaceError(f"Git entry {entry} is a symbolic link")
        if stat.S_ISDIR(entry_mode):
            git_dir = entry.resolve()
        elif stat.S_ISREG(entry_mode):
            try:
                line = entry.read_text().strip()
            except OSError as exc:
                raise WorkspaceError(
                    f"could not read Git entry {entry}: {exc}"
                ) from exc
            if not line.startswith("gitdir: ") or "\n" in line:
                raise WorkspaceError(f"Git entry {entry} is not a valid gitdir file")
            raw_git_dir = Path(line.removeprefix("gitdir: "))
            git_dir = (
                raw_git_dir.resolve()
                if raw_git_dir.is_absolute()
                else (path / raw_git_dir).resolve()
            )
        else:
            raise WorkspaceError(f"Git entry {entry} has an unsupported file type")
        if not git_dir.is_dir():
            raise WorkspaceError(f"Git directory {git_dir} does not exist")

        commondir = git_dir / "commondir"
        if commondir.is_file():
            try:
                raw_common = Path(commondir.read_text().strip())
            except OSError as exc:
                raise WorkspaceError(
                    f"could not read Git common-dir pointer {commondir}: {exc}"
                ) from exc
            common_dir = (
                raw_common.resolve()
                if raw_common.is_absolute()
                else (git_dir / raw_common).resolve()
            )
        else:
            common_dir = git_dir
        if not common_dir.is_dir():
            raise WorkspaceError(f"Git common directory {common_dir} does not exist")
        return git_dir, common_dir, entry

    @staticmethod
    def _fingerprint_metadata(
        path: Path,
        *,
        recursive: bool,
        budget: _MetadataFingerprintBudget | None = None,
    ) -> str:
        digest = hashlib.sha256()
        fingerprint_budget = budget or _MetadataFingerprintBudget()

        def visit(node: Path, relative: str, depth: int) -> None:
            if depth > _MAX_CUSTODY_METADATA_DEPTH:
                raise WorkspaceError(
                    "Git metadata exceeds the custody nesting limit "
                    f"({_MAX_CUSTODY_METADATA_DEPTH}): {node}"
                )
            fingerprint_budget.consume_entry(node)
            digest.update(relative.encode(errors="surrogateescape"))
            digest.update(b"\0")
            try:
                metadata = node.lstat()
            except FileNotFoundError:
                digest.update(b"missing\0")
                return
            except OSError as exc:
                raise WorkspaceError(
                    f"could not inspect Git metadata {node}: {exc}"
                ) from exc
            mode = metadata.st_mode
            digest.update(f"{mode:o}".encode())
            digest.update(b"\0")
            if stat.S_ISLNK(mode):
                try:
                    link_target = os.fsencode(os.readlink(node))
                except OSError as exc:
                    raise WorkspaceError(
                        f"could not read Git metadata symlink {node}: {exc}"
                    ) from exc
                fingerprint_budget.consume_bytes(node, len(link_target))
                digest.update(link_target)
            elif stat.S_ISREG(mode):
                fingerprint_budget.consume_bytes(node, metadata.st_size)
                try:
                    descriptor = os.open(
                        node,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    )
                    with os.fdopen(descriptor, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or opened.st_dev != metadata.st_dev
                            or opened.st_ino != metadata.st_ino
                            or opened.st_size != metadata.st_size
                        ):
                            raise WorkspaceError(
                                f"Git metadata changed while it was inspected: {node}"
                            )
                        consumed = 0
                        while chunk := stream.read(1024 * 1024):
                            consumed += len(chunk)
                            if consumed > metadata.st_size:
                                raise WorkspaceError(
                                    f"Git metadata grew while it was inspected: {node}"
                                )
                            digest.update(chunk)
                except OSError as exc:
                    raise WorkspaceError(
                        f"could not read Git metadata {node}: {exc}"
                    ) from exc
            elif stat.S_ISDIR(mode) and recursive:
                try:
                    children: list[Path] = []
                    with os.scandir(node) as entries:
                        for entry in entries:
                            if (
                                fingerprint_budget.entries + len(children)
                                >= _MAX_CUSTODY_METADATA_ENTRIES
                            ):
                                raise WorkspaceError(
                                    "Git metadata exceeds the custody entry limit "
                                    f"({_MAX_CUSTODY_METADATA_ENTRIES}): {node}"
                                )
                            children.append(Path(entry.path))
                    children.sort(key=lambda child: child.name)
                except OSError as exc:
                    raise WorkspaceError(
                        f"could not list Git metadata directory {node}: {exc}"
                    ) from exc
                for child in children:
                    child_relative = (
                        f"{relative}/{child.name}" if relative else child.name
                    )
                    visit(child, child_relative, depth + 1)

        visit(path, path.name, 0)
        return digest.hexdigest()

    @staticmethod
    def _validate_custody_token(target: Path, token: Mapping[str, object]) -> None:
        if token.get("version") != _CUSTODY_VERSION:
            raise WorkspaceError(
                "unsupported or missing Git-custody checkpoint version"
            )
        if token.get("workspace") != str(target):
            raise WorkspaceError("Git-custody checkpoint belongs to another workspace")
        for name in (
            "git_dir",
            "common_git_dir",
            "origin_identity_sha256",
            "origin_display",
            "effective_config_sha256",
        ):
            if not isinstance(token.get(name), str) or not token[name]:
                raise WorkspaceError(f"invalid Git-custody checkpoint field: {name}")
        watched = token.get("watched")
        if not isinstance(watched, list) or not watched:
            raise WorkspaceError("Git-custody checkpoint has no watched metadata")
        for record in watched:
            if not isinstance(record, dict):
                raise WorkspaceError("invalid Git-custody metadata record")
            if (
                not isinstance(record.get("path"), str)
                or not Path(record["path"]).is_absolute()
                or not isinstance(record.get("recursive"), bool)
                or not isinstance(record.get("digest"), str)
            ):
                raise WorkspaceError("invalid Git-custody metadata record")

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
            self._origin_for(self.repo_root),
            f"+refs/heads/{branch}:{remote_ref}",
            env=self._ephemeral_network_environment(self._origin_for(self.repo_root)),
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
        self,
        path: Path,
        *,
        branch: str,
        start_sha: str,
        kind: str = "task",
    ) -> None:
        self._write_git_marker(path, _TARGET_BRANCH_MARKER, branch)
        self._write_git_marker(path, _START_SHA_MARKER, start_sha)
        self._write_git_marker(
            path,
            _OWNER_MARKER,
            json.dumps(
                self._owner_record(kind=kind),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _owner_record(self, *, kind: str = "task") -> dict[str, object]:
        controller = str(self.repo_root.expanduser().resolve())
        origin = self._bind_controller_origin()
        return {
            "schema_version": _OWNER_SCHEMA_VERSION,
            "kind": kind,
            "controller_repo_sha256": hashlib.sha256(controller.encode()).hexdigest(),
            "origin_identity_sha256": self._origin_identity(origin),
        }

    def _workspace_owned(self, path: Path) -> bool:
        try:
            raw_path = Path(path)
            if raw_path.is_symlink():
                return False
            target = raw_path.resolve()
            git_dir, _common_dir, _git_entry = self._resolve_git_layout_raw(target)
        except (OSError, RuntimeError, WorkspaceError):
            return False
        marker = git_dir / _OWNER_MARKER
        if marker.is_symlink():
            return False
        if marker.is_file():
            try:
                raw = marker.read_text()
                if len(raw) > 4096:
                    return False
                record = json.loads(raw)
            except (OSError, UnicodeError, json.JSONDecodeError):
                return False
            if record != self._owner_record(kind="task"):
                return False
            return self._checkout_matches_controller(target)
        return False

    def _checkout_matches_controller(self, path: Path) -> bool:
        try:
            _git_dir, common_dir, _entry = self._resolve_git_layout_raw(path)
            controller_common = self._resolve_git_layout_raw(self.repo_root)[1]
            if common_dir == controller_common:
                return True
            expected = self._bind_controller_origin()
            actual = self._git(path, "remote", "get-url", "origin").strip()
            self._validate_origin_url(actual)
            return self._origin_identity(actual) == self._origin_identity(expected)
        except (OSError, RuntimeError, WorkspaceError):
            return False

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
        expected = self._origin_for(path)
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


def github_repository_target(origin: str) -> tuple[str, str] | None:
    """Parse a GitHub remote into its exact host and owner/repository target."""
    return Workspace._github_repository_target(origin)
