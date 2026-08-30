"""Draft setup-PR delivery keeps adoption reviewable and bounded."""

import subprocess
from pathlib import Path

import pytest

from machinist.github import DraftPR
from machinist.onboarding import OnboardingError, deliver_setup_pr


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "README.md").write_text("demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def default_branch(self):
        return "main"

    def create_draft_pr(self, **kwargs):
        self.calls.append(kwargs)
        return DraftPR(number=12, url="https://github.com/x/y/pull/12")


def test_setup_pr_commits_only_generated_allowlist_and_opens_draft(tmp_path):
    repo, remote = repository(tmp_path)
    github = FakeGitHub()

    def initialize():
        (repo / "machinist.yaml").write_text("version: 1\n")
        (repo / ".machinist/specs").mkdir(parents=True)
        (repo / ".machinist/specs/.gitkeep").write_text("")

    result = deliver_setup_pr(repo, github=github, initialize=initialize)

    assert result.branch == "chore/agentmachinist-setup"
    assert result.pr.number == 12
    assert git(repo, "branch", "--show-current") == result.branch
    assert set(git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()) == {
        ".machinist/specs/.gitkeep",
        "machinist.yaml",
    }
    assert git(remote, "rev-parse", result.branch) == git(repo, "rev-parse", "HEAD")
    assert github.calls[0]["base"] == "main"


def test_setup_pr_rejects_dirty_repository_before_initializer(tmp_path):
    repo, _remote = repository(tmp_path)
    (repo / "notes.txt").write_text("user work\n")
    called = False

    def initialize():
        nonlocal called
        called = True

    with pytest.raises(OnboardingError, match="clean worktree"):
        deliver_setup_pr(repo, github=FakeGitHub(), initialize=initialize)

    assert called is False
    assert git(repo, "branch", "--show-current") == "main"


def test_setup_pr_refuses_unmanaged_initializer_output_before_commit(tmp_path):
    repo, _remote = repository(tmp_path)

    def initialize():
        (repo / "machinist.yaml").write_text("version: 1\n")
        (repo / "surprise.txt").write_text("do not include\n")

    with pytest.raises(OnboardingError, match="outside the setup allowlist"):
        deliver_setup_pr(repo, github=FakeGitHub(), initialize=initialize)

    assert git(repo, "log", "-1", "--format=%s") == "initial"
    assert git(repo, "branch", "--show-current") == "chore/agentmachinist-setup"
    assert (repo / "surprise.txt").exists()
