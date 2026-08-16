"""Tests for isolated task workspaces, run against real git repos."""

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
        capture_output=True, check=True,
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


def test_clone_strategy_provisions_independent_clone(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, strategy=WorkspaceStrategy.CLONE)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert (path / "README.md").read_text() == "hello\n"
    assert git(path, "branch", "--show-current") == "agent/issue-7"
    # The clone's origin is the real origin, so push targets the same remote.
    assert git(path, "remote", "get-url", "origin") == str(tmp_path / "origin.git")
