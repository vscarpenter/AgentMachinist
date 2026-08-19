"""Tests for isolated task workspaces, run against real git repos."""

import base64
import subprocess
from pathlib import Path

import pytest

from machinist.config import CleanupPolicy, WorkspaceConfig, WorkspaceStrategy
from machinist.workspace import Workspace, WorkspaceError


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A local clone of a bare 'origin' repo, with one commit on main."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True,
        check=True,
    )
    clone = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)], capture_output=True, check=True
    )
    git(clone, "config", "user.email", "test@example.com")
    git(clone, "config", "user.name", "Test User")
    (clone / "README.md").write_text("hello\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "init")
    git(clone, "push", "-u", "origin", "main")
    return clone


def make_workspace(repo, tmp_path, **overrides):
    config = WorkspaceConfig(root=tmp_path / "ws", **overrides)
    return Workspace(repo_root=repo, config=config)


def test_provision_worktree_creates_branch_from_base(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert path == tmp_path / "ws" / "repo-issue-7"
    assert (path / "README.md").read_text() == "hello\n"
    assert git(path, "branch", "--show-current") == "agent/issue-7"


def test_provision_reuses_existing_branch(repo, tmp_path):
    git(repo, "branch", "agent/issue-7", "origin/main")
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert git(path, "branch", "--show-current") == "agent/issue-7"


def test_provision_fails_cleanly_when_path_exists(repo, tmp_path):
    (tmp_path / "ws" / "repo-issue-7").mkdir(parents=True)
    workspace = make_workspace(repo, tmp_path)

    with pytest.raises(WorkspaceError, match="repo-issue-7"):
        workspace.provision("issue-7", "agent/issue-7", "origin/main")


def test_provision_rejects_task_names_that_escape_workspace_root(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)

    with pytest.raises(WorkspaceError, match="task name"):
        workspace.provision("../../outside", "agent/issue-7", "origin/main")

    assert not (tmp_path / "outside").exists()


def test_commit_all_commits_new_files(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "spec.md").write_text("the spec\n")

    workspace.commit_all(path, "docs(spec): add spec")

    assert git(path, "log", "-1", "--format=%s") == "docs(spec): add spec"
    assert git(path, "status", "--porcelain") == ""


def test_commit_all_falls_back_to_bot_identity(repo, tmp_path, monkeypatch):
    # Hide global/system git config and the repo's local identity so the
    # environment looks like a bare CI runner.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    git(repo, "config", "--unset", "user.email")
    git(repo, "config", "--unset", "user.name")
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "spec.md").write_text("the spec\n")

    workspace.commit_all(path, "docs(spec): add spec")

    assert "AgentMachinist" in git(path, "log", "-1", "--format=%an")


def test_push_publishes_branch_to_origin(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "spec.md").write_text("the spec\n")
    workspace.commit_all(path, "docs(spec): add spec")

    workspace.push(path, "agent/issue-7")

    origin = tmp_path / "origin.git"
    heads = git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert "agent/issue-7" in heads.splitlines()


def test_actions_push_credential_is_ephemeral_git_process_config(
    repo, tmp_path, monkeypatch
):
    workspace = make_workspace(repo, tmp_path)
    monkeypatch.setenv("GH_TOKEN", "test-token")

    environment = workspace._ephemeral_push_environment()

    assert environment is not None
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    expected = base64.b64encode(b"x-access-token:test-token").decode()
    assert environment["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    assert "test-token" not in environment["GIT_CONFIG_VALUE_0"]


def test_push_with_lease_refuses_when_remote_changed(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    expected = git(path, "rev-parse", "HEAD")
    (path / "impl.md").write_text("implementation\n")
    workspace.commit_all(path, "implementation")

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)], check=True
    )
    git(other, "config", "user.email", "other@example.com")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", "-b", "agent/issue-7", "origin/main")
    (other / "spec.md").write_text("changed spec\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "change spec")
    git(other, "push", "origin", "agent/issue-7")

    with pytest.raises(WorkspaceError, match="push"):
        workspace.push(path, "agent/issue-7", expected_sha=expected)


def test_push_publishes_detached_fresh_attempt_head_not_stale_local_branch(
    repo, tmp_path
):
    workspace = make_workspace(repo, tmp_path)
    first = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (first / "spec.md").write_text("spec\n")
    workspace.commit_all(first, "spec")
    workspace.push(first, "agent/issue-7")
    remote_spec_sha = workspace.remote_sha(first, "agent/issue-7")

    fresh = workspace.provision("issue-7", "agent/issue-7", "origin/main", attempt=2)
    assert git(fresh, "branch", "--show-current") == ""
    assert workspace.head_sha(fresh) == remote_spec_sha
    (fresh / "implementation.md").write_text("implementation\n")
    workspace.commit_all(fresh, "implementation")
    implementation_sha = workspace.head_sha(fresh)

    workspace.push(fresh, "agent/issue-7", expected_sha=remote_spec_sha)

    assert workspace.remote_sha(fresh, "agent/issue-7") == implementation_sha


def test_head_sha_returns_current_commit(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    assert workspace.head_sha(path) == git(path, "rev-parse", "HEAD")


def test_branch_head_and_changed_file_helpers(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    expected = workspace.head_sha(path)

    assert workspace.current_branch(path) == "agent/issue-7"
    workspace.assert_branch(path, "agent/issue-7")
    workspace.assert_head(path, expected)
    assert workspace.changed_files(path) == []

    (path / "README.md").write_text("changed\n")
    (path / "new.txt").write_text("new\n")
    assert workspace.changed_files(path) == ["README.md", "new.txt"]

    with pytest.raises(WorkspaceError, match="expected HEAD"):
        workspace.assert_head(path, "f" * 40)

    with pytest.raises(WorkspaceError, match="expected branch"):
        workspace.assert_branch(path, "agent/issue-8")


def test_change_snapshot_detects_status_and_content_mutations(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    clean = workspace.change_snapshot(path)
    assert workspace.change_snapshot(path) == clean

    new_file = path / "new.txt"
    new_file.write_text("one\n")
    untracked = workspace.change_snapshot(path)
    assert untracked != clean
    assert workspace.change_snapshot(path) == untracked

    new_file.write_text("two\n")
    different_content = workspace.change_snapshot(path)
    assert different_content not in {clean, untracked}

    git(path, "add", "new.txt")
    staged = workspace.change_snapshot(path)
    assert staged not in {clean, untracked, different_content}

    new_file.write_text("three\n")
    staged_and_unstaged = workspace.change_snapshot(path)
    assert staged_and_unstaged not in {
        clean,
        untracked,
        different_content,
        staged,
    }


def test_cleanup_on_success_removes_worktree(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.ON_SUCCESS)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    workspace.cleanup(path, success=True)

    assert not path.exists()
    assert str(path) not in git(repo, "worktree", "list")


def test_cleanup_on_success_keeps_failed_workspace_for_debugging(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.ON_SUCCESS)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    workspace.cleanup(path, success=False)

    assert path.exists()


def test_cleanup_policy_never_keeps_workspace(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.NEVER)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    workspace.cleanup(path, success=True)

    assert path.exists()


def test_cleanup_policy_always_removes_even_on_failure(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.ALWAYS)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    workspace.cleanup(path, success=False)

    assert not path.exists()


def test_non_force_remove_preserves_dirty_worktree(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    evidence = path / "diagnostic.txt"
    evidence.write_text("keep me\n")

    with pytest.raises(WorkspaceError, match="uncommitted changes|worktree remove"):
        workspace.remove_workspace(path)

    assert path.exists()
    assert evidence.read_text() == "keep me\n"

    workspace.remove_workspace(path, force=True)
    assert not path.exists()


def test_non_force_remove_preserves_dirty_clone(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, strategy=WorkspaceStrategy.CLONE)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    evidence = path / "diagnostic.txt"
    evidence.write_text("keep me\n")

    with pytest.raises(WorkspaceError, match="uncommitted changes"):
        workspace.remove_workspace(path)

    assert path.exists()
    assert evidence.read_text() == "keep me\n"

    workspace.remove_workspace(path, force=True)
    assert not path.exists()


def test_non_force_remove_preserves_unpushed_clone_commit(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, strategy=WorkspaceStrategy.CLONE)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "diagnostic.txt").write_text("committed evidence\n")
    workspace.commit_all(path, "unpublished diagnostic commit")

    with pytest.raises(WorkspaceError, match="unpushed commits"):
        workspace.remove_workspace(path)

    assert path.exists()
    assert (path / "diagnostic.txt").exists()


def test_non_force_remove_preserves_unpushed_detached_attempt(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main", attempt=2)
    (path / "diagnostic.txt").write_text("committed evidence\n")
    workspace.commit_all(path, "unpublished diagnostic commit")

    with pytest.raises(WorkspaceError, match="unpushed commits"):
        workspace.remove_workspace(path)

    assert path.exists()
    assert (path / "diagnostic.txt").exists()


def test_remove_rejects_path_outside_managed_root_even_with_force(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evidence.txt").write_text("keep me\n")

    with pytest.raises(WorkspaceError, match="outside managed workspace root"):
        workspace.remove_workspace(outside, force=True)

    assert (outside / "evidence.txt").read_text() == "keep me\n"


def test_remove_rejects_managed_symlink_alias_even_with_force(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "repo-issue-8"
    target.mkdir()
    evidence = target / "evidence.txt"
    evidence.write_text("keep me\n")
    alias = root / "repo-issue-7"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="symbolic link"):
        workspace.remove_workspace(alias, force=True)

    assert alias.is_symlink()
    assert evidence.read_text() == "keep me\n"


def test_provision_from_remote_only_branch(repo, tmp_path):
    # Simulate another machine having pushed the spec branch: it exists on
    # origin (one commit ahead of main) but not locally.
    git(repo, "checkout", "-q", "-b", "agent/issue-7", "origin/main")
    (repo / "extra.md").write_text("from spec phase\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "spec")
    git(repo, "push", "origin", "agent/issue-7")
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-D", "agent/issue-7")
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert git(path, "branch", "--show-current") == "agent/issue-7"
    assert (path / "extra.md").exists()


def test_provision_discovers_remote_branch_from_single_branch_clone(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "agent/issue-7", "origin/main")
    (repo / "spec.md").write_text("remote spec\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "spec")
    git(repo, "push", "origin", "agent/issue-7")
    remote_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")

    narrow = tmp_path / "narrow"
    subprocess.run(
        [
            "git",
            "clone",
            "--single-branch",
            "--branch",
            "main",
            str(tmp_path / "origin.git"),
            str(narrow),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    git(narrow, "config", "user.email", "test@example.com")
    git(narrow, "config", "user.name", "Test User")
    assert (
        subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/remotes/origin/agent/issue-7",
            ],
            cwd=narrow,
            capture_output=True,
        ).returncode
        != 0
    )
    workspace = Workspace(
        repo_root=narrow,
        config=WorkspaceConfig(root=tmp_path / "narrow-ws"),
    )

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert workspace.head_sha(path) == remote_sha
    assert (path / "spec.md").read_text() == "remote spec\n"


def test_provision_fast_forwards_stale_local_branch(repo, tmp_path):
    # Local branch exists at origin/main, but origin's copy is one commit
    # ahead (e.g. the spec was edited on GitHub). Provision must land on
    # the origin tip, not the stale local one.
    git(repo, "branch", "agent/issue-7", "origin/main")
    git(repo, "checkout", "-q", "agent/issue-7")
    (repo / "spec-edit.md").write_text("edited on GitHub\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "spec edit")
    git(repo, "push", "origin", "agent/issue-7")
    git(repo, "reset", "--hard", "-q", "origin/main")
    git(repo, "checkout", "-q", "main")
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert (path / "spec-edit.md").exists()


def test_provision_discards_rejected_local_commit_before_retry(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    first = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (first / "spec.md").write_text("approved spec\n")
    workspace.commit_all(first, "approved spec")
    workspace.push(first, "agent/issue-7")
    approved_sha = workspace.remote_sha(first, "agent/issue-7")

    # Simulate a Harness violating Git custody. The rejected commit remains on
    # the local Task branch after the failed Workshop is force-cleaned.
    (first / "rejected.txt").write_text("must never be published\n")
    workspace.commit_all(first, "harness-created rejected commit")
    rejected_sha = workspace.head_sha(first)
    assert rejected_sha != approved_sha
    workspace.remove_workspace(first, force=True)
    assert git(repo, "rev-parse", "agent/issue-7") == rejected_sha

    retried = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert workspace.head_sha(retried) == approved_sha
    assert not (retried / "rejected.txt").exists()
    assert git(repo, "rev-parse", "agent/issue-7") == approved_sha


def test_provision_uses_remote_when_local_task_branch_has_diverged(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    first = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (first / "local-only.txt").write_text("rejected\n")
    workspace.commit_all(first, "rejected local commit")
    workspace.remove_workspace(first, force=True)

    other = tmp_path / "other-diverged"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)],
        capture_output=True,
        check=True,
    )
    git(other, "config", "user.email", "other@example.com")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", "-b", "agent/issue-7", "origin/main")
    (other / "remote-only.txt").write_text("approved remote\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "remote spec")
    git(other, "push", "origin", "agent/issue-7")
    remote_sha = git(other, "rev-parse", "HEAD")

    retried = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert workspace.head_sha(retried) == remote_sha
    assert (retried / "remote-only.txt").exists()
    assert not (retried / "local-only.txt").exists()


def test_has_changes_reflects_working_tree(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert workspace.has_changes(path) is False
    (path / "new.md").write_text("hi\n")
    assert workspace.has_changes(path) is True


def test_clone_strategy_provisions_independent_clone(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, strategy=WorkspaceStrategy.CLONE)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert (path / "README.md").read_text() == "hello\n"
    assert git(path, "branch", "--show-current") == "agent/issue-7"
    # The clone's origin is the real origin, so push targets the same remote.
    assert git(path, "remote", "get-url", "origin") == str(tmp_path / "origin.git")


def test_explicit_attempt_path_starts_fresh_while_failed_worktree_is_retained(
    repo, tmp_path
):
    workspace = make_workspace(repo, tmp_path)
    retained = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (retained / "failed-change.txt").write_text("diagnostic evidence\n")
    expected_sha = workspace.head_sha(retained)

    fresh = workspace.provision("issue-7", "agent/issue-7", "origin/main", attempt=2)

    assert retained.exists()
    assert (retained / "failed-change.txt").exists()
    assert fresh == tmp_path / "ws" / "repo-issue-7-attempt-2"
    assert workspace.head_sha(fresh) == expected_sha
    assert not (fresh / "failed-change.txt").exists()


def test_resume_validates_managed_checkout_branch_and_head(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main", attempt=2)
    expected_sha = workspace.head_sha(path)
    (path / "diagnostic.txt").write_text("unfinished work\n")

    resumed = workspace.resume(path, branch="agent/issue-7", expected_sha=expected_sha)

    assert resumed == path.resolve()
    assert (resumed / "diagnostic.txt").exists()

    with pytest.raises(WorkspaceError, match="expected HEAD"):
        workspace.resume(path, branch="agent/issue-7", expected_sha="f" * 40)

    with pytest.raises(WorkspaceError, match="expected branch"):
        workspace.resume(path, branch="agent/issue-8", expected_sha=expected_sha)


def test_workspace_management_helpers(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    # workspace_for_task
    ws_path = workspace.workspace_for_task("issue-7")
    assert ws_path == tmp_path / "ws" / f"{repo.name}-issue-7"

    # Initially empty list_workspaces
    assert workspace.list_workspaces() == []

    # Provision a worktree
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    assert path.exists()

    workspaces = workspace.list_workspaces()
    assert len(workspaces) == 1
    assert workspaces[0] == path

    # remove_workspace
    workspace.remove_workspace(path)
    assert not path.exists()
    assert workspace.list_workspaces() == []


def test_git_timeout_becomes_typed_workspace_error(repo, tmp_path):
    def timeout_runner(args, **kwargs):
        assert kwargs["timeout"] == 300
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    workspace = Workspace(
        repo_root=repo,
        config=WorkspaceConfig(root=tmp_path / "ws"),
        runner=timeout_runner,
    )

    with pytest.raises(WorkspaceError, match="timed out"):
        workspace.head_sha(repo)
