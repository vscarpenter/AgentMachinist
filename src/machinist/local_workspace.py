"""Exact local Git candidates, isolated Workshops, and human integration.

The Git mechanics and metadata guard remain owned by Workspace. This adapter
supplies local repository identity and durable refs without inventing an origin.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from bisect import bisect_left
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from machinist.config import CleanupPolicy, WorkspaceConfig, WorkspaceStrategy
from machinist.gitlab import gitlab_command_environment
from machinist.managed_paths import (
    ManagedPathError,
    read_managed_text,
    write_managed_text,
)
from machinist.process import credential_reduced_environment
from machinist.workspace import (
    Workspace,
    WorkspaceError,
    _MetadataFingerprintBudget,
)


class LocalWorkspace:
    def __init__(self, repo_root: Path, config: WorkspaceConfig):
        self.repo_root = Path(repo_root).resolve()
        self.config = config
        if config.resolved_root().is_relative_to(self.repo_root):
            raise WorkspaceError("Workshop root must be outside the repository")
        self._workspace = Workspace(self.repo_root, config)
        self._publication_auth: tuple[str, str] | None = None
        self._workspace.capture_git_custody(
            self.repo_root, standalone=True, local_repository=True
        )

    @property
    def cancel_check(self) -> Callable[[], bool] | None:
        return self._workspace.cancel_check

    @cancel_check.setter
    def cancel_check(self, value: Callable[[], bool] | None) -> None:
        self._workspace.cancel_check = value

    def resolve_commit(self, ref: str = "HEAD") -> str:
        self._controller_custody()
        if not ref or ref.startswith("-") or any(c in ref for c in "\0\n\r"):
            raise WorkspaceError("invalid local commit reference")
        return self._workspace._resolve_commit(self.repo_root, ref)

    def runtime_exclusion_needs_update(self) -> bool:
        """Read-only setup preflight; pending ignore precedence is verified on write."""
        self._controller_custody()
        if self._workspace._git(self.repo_root, "ls-files", ".machinist/runs/"):
            raise WorkspaceError("controller runtime files must not be tracked by Git")
        if (
            self._workspace._run(
                self.repo_root, "check-ignore", "-q", ".machinist/runs/local/probe"
            ).returncode
            == 0
        ):
            return False
        if any(path != self.repo_root for path in self._workspace._custody):
            raise WorkspaceError(
                "configure runtime exclusion before provisioning Workshops"
            )
        common = self._workspace._resolve_git_layout_raw(self.repo_root)[1]
        try:
            read_managed_text(common, "info/exclude", max_bytes=1024 * 1024)
        except ManagedPathError as exc:
            raise WorkspaceError(f"cannot safely exclude local runtime: {exc}") from exc
        parent = common / "info"
        if not parent.exists():
            parent = common
        if not os.access(parent, os.W_OK | os.X_OK):
            raise WorkspaceError(
                f"cannot safely exclude local runtime: directory is not writable: {parent}"
            )
        return True

    def ensure_runtime_ignored(self) -> None:
        """Exclude runtime records locally before any Workshop is provisioned."""
        if not self.runtime_exclusion_needs_update():
            return
        common = self._workspace._resolve_git_layout_raw(self.repo_root)[1]
        try:
            content = (
                read_managed_text(common, "info/exclude", max_bytes=1024 * 1024) or ""
            )
            content = content.rstrip("\n") + "\n/.machinist/runs/\n"
            write_managed_text(common, "info/exclude", content)
        except ManagedPathError as exc:
            raise WorkspaceError(f"cannot safely exclude local runtime: {exc}") from exc
        self._workspace.capture_git_custody(
            self.repo_root, standalone=True, local_repository=True
        )
        if (
            self._workspace._run(
                self.repo_root, "check-ignore", "-q", ".machinist/runs/local/probe"
            ).returncode
            != 0
        ):
            raise WorkspaceError(
                "repository ignore rules override the local runtime exclusion; "
                "add '/.machinist/runs/' to .gitignore"
            )

    def read_at_commit(self, sha: str, relative: str, *, max_bytes: int) -> str:
        """Read a bounded regular blob from the exact local commit."""
        self._controller_custody()
        self._workspace._validate_sha(sha, label="Spec commit")
        path = PurePosixPath(relative)
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != relative
            or any(c in relative for c in "\0\n\r")
        ):
            raise WorkspaceError("Spec path must be a safe repository-relative file")
        if max_bytes < 1:
            raise WorkspaceError("Spec byte limit must be positive")
        listing = self._workspace._git(
            self.repo_root, "ls-tree", "-z", sha, "--", relative
        )
        rows = [entry for entry in listing.split("\0") if entry]
        if len(rows) != 1 or not rows[0].startswith(("100644 blob ", "100755 blob ")):
            raise WorkspaceError("Spec must name a regular file at the approved commit")
        object_name = f"{sha}:{relative}"
        size = int(self._workspace._git(self.repo_root, "cat-file", "-s", object_name))
        if size > max_bytes:
            raise WorkspaceError(f"Spec exceeds its {max_bytes}-byte limit")
        return self._workspace._git(self.repo_root, "cat-file", "blob", object_name)

    def branch_sha(self, branch: str) -> str | None:
        self._controller_custody()
        self._workspace._validate_branch(branch)
        ref = f"refs/heads/{branch}"
        if not self._workspace._branch_exists(self.repo_root, ref):
            return None
        return self.resolve_commit(ref)

    def provision(
        self,
        task: str,
        branch: str,
        base_sha: str,
        *,
        attempt: int | None = None,
    ) -> Path:
        self._controller_custody()
        self._workspace._validate_sha(base_sha, label="local base")
        self._workspace._validate_branch(branch)
        start = self.resolve_commit(base_sha)
        path = self._workspace.workspace_for_task(task, attempt=attempt)
        if path.exists() or path.is_symlink():
            raise WorkspaceError(f"Workshop {path} already exists; use a fresh attempt")
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.config.strategy is WorkspaceStrategy.WORKTREE:
            self._workspace._git(
                self.repo_root, "worktree", "add", "--detach", str(path), start
            )
        else:
            self._workspace._git(
                path.parent,
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                str(self.repo_root),
                str(path),
            )
            # The local path is only an object-copy source, never a publishing
            # destination. The isolated clone has no remote after provisioning.
            self._workspace._git(path, "remote", "remove", "origin")
            self._workspace._git(path, "checkout", "--detach", start)
        for name, value in (
            ("agentmachinist-target-branch", branch),
            ("agentmachinist-start-sha", start),
            ("agentmachinist-owner.json", json.dumps(self._owner(), sort_keys=True)),
        ):
            self._workspace._write_git_marker(path, name, value)
        self._workspace.capture_git_custody(path, local_repository=True)
        self._workspace.assert_head(path, start)
        return path

    def git_custody(self, path: Path) -> dict[str, object]:
        token = self._workspace.git_custody(path)
        if token is None:
            raise WorkspaceError("Workshop has no local Git-custody checkpoint")
        return token

    def assert_git_custody(
        self, path: Path, expected: Mapping[str, object] | None = None
    ) -> None:
        self._workspace.assert_git_custody(path, expected)

    def resume(
        self,
        path: Path,
        *,
        branch: str,
        expected_sha: str,
        git_custody: Mapping[str, object] | None = None,
    ) -> Path:
        target = self._workspace.managed_path(path)
        token = (
            git_custody
            if git_custody is not None
            else self._workspace.git_custody(target)
        )
        if token is None or token.get("repository_mode") != "local":
            raise WorkspaceError("retained local Workshop needs a custody checkpoint")
        # No Git subprocess in a retained Workshop before its raw metadata
        # matches the persisted token, including ownership and branch markers.
        self._workspace.assert_git_custody(target, token)
        self._assert_owned(target)
        self._workspace.assert_branch(target, branch)
        self._workspace.assert_head(target, expected_sha)
        return target

    def head_sha(self, path: Path) -> str:
        return self._workspace.head_sha(path)

    def current_branch(self, path: Path | None = None) -> str | None:
        return self._workspace.current_branch(path or self.repo_root)

    def assert_head(self, path: Path, expected_sha: str) -> None:
        self._workspace.assert_head(path, expected_sha)

    def changed_files(self, path: Path) -> list[str]:
        return self._workspace.changed_files(path)

    def change_snapshot(self, path: Path) -> str:
        return self._workspace.change_snapshot(path)

    def has_changes(self, path: Path) -> bool:
        return self._workspace.has_changes(path)

    def path_changed(self, path: Path, relative: str) -> bool:
        return self._workspace.path_changed(path, relative)

    def diff_against(self, path: Path, base_ref: str, *, max_bytes: int) -> str:
        return self._workspace.diff_against(path, base_ref, max_bytes=max_bytes)

    def read_spec(self, path: Path, relative: str | Path) -> str:
        self._workspace.assert_git_custody(path)
        content = read_managed_text(path, relative)
        if content is None or not content.strip():
            raise WorkspaceError(f"local Spec {relative} is missing or empty")
        return content

    def commit_all(self, path: Path, message: str) -> None:
        self._workspace.commit_all(path, message)

    def prepare_commit(self, path: Path) -> str:
        """Stage verified output and return the tree to journal before commit."""
        self._workspace.assert_git_custody(path)
        self._workspace._git(path, "add", "-A")
        tree = self._workspace._git(path, "write-tree").strip()
        self._workspace._validate_sha(tree, label="prepared commit tree")
        self._workspace.assert_git_custody(path)
        return tree

    def commit_identity(self, path: Path) -> dict[str, object]:
        """Describe one exact HEAD for authenticated interrupted-commit recovery."""
        sha = self.head_sha(path)
        details = self._workspace._git(
            path, "show", "-s", "--format=%T%x00%P%x00%B", sha
        ).split("\0", 2)
        if len(details) != 3:
            raise WorkspaceError("could not inspect the exact local commit")
        tree, raw_parents, message = details
        self._workspace._validate_sha(tree, label="commit tree")
        parents = raw_parents.split()
        for parent in parents:
            self._workspace._validate_sha(parent, label="commit parent")
        return {
            "sha": sha,
            "tree": tree,
            "parents": parents,
            "message": message.rstrip("\n"),
        }

    def capture_harness_state(self, path: Path) -> dict[str, object]:
        self._controller_custody()
        self._workspace.assert_git_custody(path)
        return {
            "head": self.head_sha(path),
            "branch": self.current_branch(path),
            "refs": self._refs(path),
            "controller_head": self.head_sha(self.repo_root),
            "controller_branch": self.current_branch(),
            "controller_refs": self._refs(self.repo_root),
            "managed": self._managed_snapshot(path),
            "changes": self.change_snapshot(path),
        }

    def assert_harness_state(
        self,
        path: Path,
        before: Mapping[str, object],
        *,
        read_only: bool = False,
    ) -> None:
        observed = self.capture_harness_state(path)
        for field in (
            "head",
            "branch",
            "refs",
            "controller_head",
            "controller_branch",
            "controller_refs",
            "managed",
        ):
            if field not in before or observed[field] != before[field]:
                raise WorkspaceError(f"Harness changed controller-owned {field}")
        if read_only and observed["changes"] != before.get("changes"):
            raise WorkspaceError("Harness modified its read-only Workshop")

    def retain_candidate(
        self, path: Path, branch: str, *, expected_sha: str | None
    ) -> str:
        self._controller_custody()
        self._workspace.assert_git_custody(path)
        self._assert_owned(path)
        if self._workspace._read_target_branch(path) != branch:
            raise WorkspaceError("local candidate must use its Workshop target branch")
        candidate = self.head_sha(path)
        current = self.branch_sha(branch)
        if current == candidate:
            return candidate
        if current != expected_sha:
            raise WorkspaceError(
                "local candidate branch changed; refusing to overwrite"
            )
        worktrees = self._workspace._git(
            self.repo_root, "worktree", "list", "--porcelain"
        )
        if f"branch refs/heads/{branch}" in worktrees.splitlines():
            raise WorkspaceError(
                "candidate branch is checked out; refusing to move its ref"
            )
        if expected_sha is not None:
            self._workspace._validate_sha(
                expected_sha, label="expected local candidate"
            )
        if self.config.strategy is WorkspaceStrategy.CLONE:
            self._workspace._git(
                self.repo_root,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(path),
                candidate,
            )
        self._workspace._git(
            self.repo_root,
            "update-ref",
            f"refs/heads/{branch}",
            candidate,
            expected_sha or "0" * len(candidate),
        )
        if self.branch_sha(branch) != candidate:
            raise WorkspaceError(
                "local candidate ref did not retain the intended commit"
            )
        return candidate

    def origin_url(self) -> str:
        """Read one unambiguous publishing origin without contacting it."""
        self._controller_custody()
        for pattern, reason in (
            (r"^remote\.origin\.pushurl$", "a separate pushurl"),
            (r"^url\..*\.(insteadof|pushinsteadof)$", "Git URL rewrites"),
        ):
            result = self._workspace._run(
                self.repo_root, "config", "--get-regexp", pattern
            )
            if result.returncode == 0 and result.stdout.strip():
                raise WorkspaceError(f"publication does not accept {reason}")
            if result.returncode not in (0, 1):
                raise WorkspaceError(
                    "could not verify publishing transport configuration"
                )
        result = self._workspace._run(
            self.repo_root, "config", "--get-all", "remote.origin.url"
        )
        urls = result.stdout.splitlines()
        if result.returncode != 0 or len(urls) != 1:
            raise WorkspaceError("publication requires exactly one origin URL")
        origin = urls[0]
        self._validate_transport(origin)
        return origin

    def bind_publication_auth(self, provider: str, *, origin_url: str) -> None:
        """Select credentials only after publication binds an explicit forge."""
        if provider not in {"github", "gitlab"}:
            raise WorkspaceError("unsupported publication authentication provider")
        self._require_origin(origin_url)
        self._publication_auth = (provider, origin_url)

    def remote_sha(self, branch: str, *, origin_url: str) -> str | None:
        self._require_origin(origin_url)
        self._workspace._validate_branch(branch)
        output = self._network_git(
            "ls-remote", "--heads", origin_url, f"refs/heads/{branch}"
        )
        rows = output.splitlines()
        if not rows:
            return None
        fields = rows[0].split()
        if len(rows) != 1 or len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
            raise WorkspaceError("remote did not return the exact requested branch")
        self._workspace._validate_sha(fields[0], label="remote head")
        return fields[0]

    def push_candidate(
        self,
        branch: str,
        *,
        expected_candidate_sha: str,
        expected_remote_sha: str | None,
        origin_url: str,
    ) -> str:
        self._require_origin(origin_url)
        self._workspace._validate_sha(expected_candidate_sha, label="candidate commit")
        if expected_remote_sha is not None:
            self._workspace._validate_sha(
                expected_remote_sha, label="expected remote head"
            )
        if self.branch_sha(branch) != expected_candidate_sha:
            raise WorkspaceError("local candidate branch changed before publication")
        current = self.remote_sha(branch, origin_url=origin_url)
        if current == expected_candidate_sha:
            return current
        if current != expected_remote_sha:
            raise WorkspaceError("remote branch changed; refusing to overwrite")
        self._require_origin(origin_url)
        self._network_git(
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected_remote_sha or ''}",
            origin_url,
            f"{expected_candidate_sha}:refs/heads/{branch}",
        )
        observed = self.remote_sha(branch, origin_url=origin_url)
        if observed != expected_candidate_sha:
            raise WorkspaceError(
                "publication did not retain the exact candidate commit"
            )
        return observed

    def _require_origin(self, expected: str) -> None:
        self._validate_transport(expected)
        if self.origin_url() != expected:
            raise WorkspaceError("origin changed since publication was prepared")

    def _network_git(self, *args: str) -> str:
        origin = self.origin_url()
        if self._publication_auth == ("gitlab", origin):
            environment = self._gitlab_network_environment(origin)
        else:
            environment = self._workspace._ephemeral_network_environment(origin)
        return self._workspace._git(
            self.repo_root,
            *args,
            env=environment,
        )

    def _gitlab_network_environment(self, origin: str) -> dict[str, str] | None:
        parsed = urlsplit(origin)
        if parsed.scheme != "https":
            return None
        host = parsed.netloc.casefold()
        auth_environment = credential_reduced_environment(
            gitlab_command_environment(host),
            allow=("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN"),
        )
        try:
            result = self._workspace._auth_runner(
                ["glab", "config", "get", "token", "--host", host],
                capture_output=True,
                text=True,
                timeout=10,
                env=auth_environment,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise WorkspaceError(
                f"GitLab authentication is unavailable; run glab auth login --hostname {host}"
            ) from exc
        token = result.stdout.strip()
        if (
            result.returncode != 0
            or not token
            or len(token) > 16_384
            or any(character.isspace() or character == "\0" for character in token)
        ):
            raise WorkspaceError(
                f"GitLab authentication returned no usable token; run glab auth login --hostname {host}"
            )
        encoded = base64.b64encode(f"oauth2:{token}".encode()).decode()
        environment = self._workspace._controller_git_environment()
        scope = f"https://{host}{parsed.path.rstrip('/')}/"
        environment.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0=f"http.{scope}.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {encoded}",
        )
        return environment

    @staticmethod
    def _validate_transport(origin: str) -> None:
        if not origin or any(c in origin for c in "\0\n\r") or origin.startswith("-"):
            raise WorkspaceError("unsafe publishing origin")
        if Path(origin).is_absolute():
            return
        if (
            "://" not in origin
            and not origin.startswith("ext::")
            and re.fullmatch(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[^\s?#]+", origin)
        ):
            return
        try:
            parsed = urlsplit(origin)
            valid = (
                parsed.scheme in {"https", "ssh"}
                and parsed.hostname
                and parsed.path not in ("", "/")
                and not parsed.query
                and not parsed.fragment
                and parsed.password is None
                and (parsed.scheme == "ssh" or parsed.username is None)
                and parsed.port != 0
            )
        except ValueError:
            valid = False
        if not valid:
            raise WorkspaceError(
                "origin must use SSH or HTTPS without embedded credentials"
            )

    def integrate(
        self,
        branch: str,
        *,
        base_branch: str,
        expected_base_sha: str,
        expected_candidate_sha: str,
    ) -> str:
        """CAS a clean checked-out base to the exact candidate, then reconcile it.

        The caller journals intent before entry and completion afterward. If
        interrupted after the ref update, only an index/worktree still exactly
        at the old base is eligible for automatic checkout reconciliation.
        """
        self._controller_custody()
        self._workspace._validate_branch(base_branch)
        for value in (expected_base_sha, expected_candidate_sha):
            self._workspace._validate_sha(value, label="integration commit")
        if branch == base_branch:
            raise WorkspaceError(
                "candidate and integration base must be different branches"
            )
        if self.branch_sha(branch) != expected_candidate_sha:
            raise WorkspaceError("candidate branch changed since human review")
        if self.current_branch() != base_branch:
            raise WorkspaceError(
                f"check out base branch {base_branch!r} before integration"
            )
        actual_base = self.branch_sha(base_branch)
        if actual_base not in (expected_base_sha, expected_candidate_sha):
            raise WorkspaceError("base branch changed; revalidate the candidate")
        if (
            self._workspace._run(
                self.repo_root,
                "merge-base",
                "--is-ancestor",
                expected_base_sha,
                expected_candidate_sha,
            ).returncode
            != 0
        ):
            raise WorkspaceError("local integration requires a fast-forward candidate")
        if actual_base == expected_candidate_sha and self._tree_matches(
            expected_candidate_sha
        ):
            return expected_candidate_sha
        if not self._tree_matches(expected_base_sha):
            raise WorkspaceError(
                "integration requires a clean checkout at the expected base"
            )
        self._assert_no_untracked_collisions(expected_candidate_sha)
        if actual_base == expected_base_sha:
            self._workspace._git(
                self.repo_root,
                "update-ref",
                f"refs/heads/{base_branch}",
                expected_candidate_sha,
                expected_base_sha,
            )
        self._checkout_candidate(expected_base_sha, expected_candidate_sha, base_branch)
        if self.branch_sha(
            base_branch
        ) != expected_candidate_sha or not self._tree_matches(expected_candidate_sha):
            raise WorkspaceError(
                "local integration did not reach the exact clean candidate"
            )
        return expected_candidate_sha

    def _checkout_candidate(self, base: str, candidate: str, base_branch: str) -> None:
        if self.current_branch() != base_branch:
            raise WorkspaceError("checked-out branch changed during integration")
        self._assert_no_untracked_collisions(candidate)
        self._workspace._git(self.repo_root, "read-tree", "-m", "-u", base, candidate)

    def _assert_no_untracked_collisions(self, candidate: str) -> None:
        """Protect ignored user data that Git's read-tree otherwise discards."""
        self._controller_custody()
        untracked = self._workspace._git(self.repo_root, "ls-files", "--others", "-z")
        if not untracked:
            return
        tracked = self._workspace._git(
            self.repo_root, "ls-tree", "-r", "--name-only", "-z", candidate
        )
        case_setting = self._workspace._run(
            self.repo_root, "config", "--bool", "core.ignorecase"
        )
        ignore_case = (
            case_setting.returncode == 0 and case_setting.stdout.strip() == "true"
        )
        candidate_paths = {
            name.casefold() if ignore_case else name
            for name in tracked.split("\0")
            if name
        }
        ordered = sorted(candidate_paths)
        for raw in untracked.split("\0"):
            if not raw:
                continue
            name = raw.rstrip("/")
            if ignore_case:
                name = name.casefold()
            descendants = name + "/"
            index = bisect_left(ordered, descendants)
            ancestor_collision = any(
                str(parent) in candidate_paths for parent in PurePosixPath(name).parents
            )
            if (
                name in candidate_paths
                or ancestor_collision
                or (index < len(ordered) and ordered[index].startswith(descendants))
            ):
                raise WorkspaceError(
                    f"candidate would overwrite untracked or ignored path {raw!r}; "
                    "move that local data before integration"
                )

    def _tree_matches(self, sha: str) -> bool:
        self._controller_custody()
        for args in (
            ("diff", "--quiet", "--no-ext-diff"),
            ("diff", "--quiet", "--no-ext-diff", "--cached", sha, "--"),
        ):
            if self._workspace._run(self.repo_root, *args).returncode != 0:
                return False
        return not self._workspace._git(
            self.repo_root, "ls-files", "--others", "--exclude-standard", "-z"
        )

    def cleanup(self, path: Path, *, success: bool) -> None:
        if self.config.cleanup is CleanupPolicy.NEVER or (
            self.config.cleanup is CleanupPolicy.ON_SUCCESS and not success
        ):
            return
        target = self._workspace.managed_path(path)
        self._workspace.assert_git_custody(target)
        self._assert_owned(target)
        head = self.head_sha(target)
        start = self._workspace._read_start_sha(target)
        branch = self._workspace._read_target_branch(target)
        if head != start and (branch is None or self.branch_sha(branch) != head):
            raise WorkspaceError(
                "retain the local candidate before removing its Workshop"
            )
        self._controller_custody()
        if self.config.strategy is WorkspaceStrategy.WORKTREE:
            self._workspace._git(
                self.repo_root, "worktree", "remove", "--force", str(target)
            )
        else:
            shutil.rmtree(target)

    def _controller_custody(self) -> None:
        self._workspace.assert_git_custody(self.repo_root)

    def _refs(self, path: Path) -> str:
        return self._workspace._git(
            path, "for-each-ref", "--format=%(refname) %(objectname)"
        )

    def _managed_snapshot(self, path: Path) -> str:
        return self._workspace._fingerprint_metadata(
            path / ".machinist", recursive=True, budget=_MetadataFingerprintBudget()
        )

    def _owner(self) -> dict[str, str | int]:
        return {
            "schema_version": 1,
            "kind": "local-task",
            "controller_repository": str(self.repo_root),
        }

    def _assert_owned(self, path: Path) -> None:
        raw = self._workspace._read_git_marker(path, "agentmachinist-owner.json")
        try:
            valid = raw is not None and json.loads(raw) == self._owner()
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise WorkspaceError(
                "local Workshop has no valid controller ownership marker"
            )
