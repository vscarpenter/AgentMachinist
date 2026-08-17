"""Isolated per-task workspaces.

Each agent task gets its own checkout under workspace.root so the user's
working tree is never touched. The default strategy is a git worktree
(shares the object store); 'clone' makes a fully independent copy whose
origin points at the same remote.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from machinist.config import CleanupPolicy, WorkspaceConfig, WorkspaceStrategy

Runner = Callable[..., subprocess.CompletedProcess]

_BOT_NAME = "AgentMachinist"
_BOT_EMAIL = "machinist@users.noreply.github.com"


class WorkspaceError(Exception):
    """A git workspace operation failed."""


class Workspace:
    def __init__(self, repo_root: Path, config: WorkspaceConfig, runner: Runner = subprocess.run):
        self.repo_root = repo_root
        self.config = config
        self._runner = runner

    def provision(self, task: str, branch: str, base_ref: str) -> Path:
        root = self.config.resolved_root()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{self.repo_root.name}-{task}"
        if path.exists():
            raise WorkspaceError(
                f"workspace {path} already exists; remove it (or 'git worktree remove' it) and retry"
            )
        self._git(self.repo_root, "fetch", "origin")
        if self.config.strategy is WorkspaceStrategy.WORKTREE:
            remote_branch = f"refs/remotes/origin/{branch}"
            if self._branch_exists(self.repo_root, f"refs/heads/{branch}"):
                self._git(self.repo_root, "worktree", "add", str(path), branch)
                if self._branch_exists(self.repo_root, remote_branch):
                    # Origin is the source of truth for machinist branches;
                    # ff-only refuses (loudly) if the two have diverged.
                    self._git(path, "merge", "--ff-only", f"origin/{branch}")
            elif self._branch_exists(self.repo_root, remote_branch):
                self._git(self.repo_root, "worktree", "add", str(path), "-b", branch, f"origin/{branch}")
            else:
                self._git(self.repo_root, "worktree", "add", str(path), "-b", branch, base_ref)
        else:
            origin_url = self._git(self.repo_root, "remote", "get-url", "origin").strip()
            self._git(root, "clone", origin_url, str(path))
            if self._branch_exists(path, f"refs/remotes/origin/{branch}"):
                self._git(path, "checkout", branch)
            else:
                self._git(path, "checkout", "-b", branch, base_ref)
        return path

    def commit_all(self, path: Path, message: str) -> None:
        self._git(path, "add", "-A")
        args = ["commit", "-m", message]
        if not self._has_identity(path):
            # Bare CI runners have no git identity configured.
            args = ["-c", f"user.name={_BOT_NAME}", "-c", f"user.email={_BOT_EMAIL}", *args]
        self._git(path, *args)

    def has_changes(self, path: Path) -> bool:
        return bool(self._git(path, "status", "--porcelain").strip())

    def push(self, path: Path, branch: str, *, expected_sha: str | None = None) -> None:
        args = ["push", "-u"]
        if expected_sha is not None:
            args.append(f"--force-with-lease=refs/heads/{branch}:{expected_sha}")
        self._git(path, *args, "origin", branch)

    def head_sha(self, path: Path) -> str:
        return self._git(path, "rev-parse", "HEAD").strip()

    def remote_sha(self, path: Path, branch: str) -> str | None:
        output = self._git(path, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        return output.split()[0] if output.strip() else None

    def path_changed(self, path: Path, relative: str) -> bool:
        return bool(self._git(path, "status", "--porcelain", "--", relative).strip())

    def cleanup(self, path: Path, *, success: bool) -> None:
        policy = self.config.cleanup
        if policy is CleanupPolicy.NEVER:
            return
        if policy is CleanupPolicy.ON_SUCCESS and not success:
            return
        if self.config.strategy is WorkspaceStrategy.WORKTREE:
            self._git(self.repo_root, "worktree", "remove", "--force", str(path))
        else:
            shutil.rmtree(path)

    def _git(self, cwd: Path, *args: str) -> str:
        result = self._run(cwd, *args)
        if result.returncode != 0:
            raise WorkspaceError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def _run(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        try:
            return self._runner(["git", *args], cwd=cwd, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise WorkspaceError("git not found on PATH") from exc

    def _branch_exists(self, cwd: Path, ref: str) -> bool:
        return self._run(cwd, "rev-parse", "--verify", "--quiet", ref).returncode == 0

    def _has_identity(self, cwd: Path) -> bool:
        result = self._run(cwd, "config", "user.email")
        return result.returncode == 0 and bool(result.stdout.strip())
