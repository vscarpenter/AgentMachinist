"""Draft setup-PR delivery keeps adoption reviewable and bounded."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from machinist.cli import main
from machinist.doctor import DoctorReport
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

    def ensure_label(self, *args, **kwargs):
        pass

    def pr_for_branch(self, branch):
        if not self.calls:
            return None
        return SimpleNamespace(
            number=12,
            url="https://github.com/x/y/pull/12",
            state="OPEN",
            is_draft=True,
            branch=branch,
            base="main",
        )

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


def test_setup_pr_runs_readiness_validation_before_commit(tmp_path) -> None:
    repo, _remote = repository(tmp_path)

    def initialize() -> None:
        (repo / "machinist.yaml").write_text("version: 1\n")

    def validate() -> None:
        raise OnboardingError("setup preflight failed: verification")

    with pytest.raises(OnboardingError, match="setup preflight failed"):
        deliver_setup_pr(
            repo,
            github=FakeGitHub(),
            initialize=initialize,
            validate=validate,
        )

    assert git(repo, "log", "-1", "--format=%s") == "initial"
    assert git(repo, "branch", "--show-current") == "chore/agentmachinist-setup"
    assert (repo / "machinist.yaml").exists()


def test_setup_pr_resumes_after_preflight_failure_without_replacing_config(tmp_path):
    repo, remote = repository(tmp_path)
    github = FakeGitHub()
    config = "version: 1\nharness:\n  name: codex\n"

    def initialize():
        target = repo / "machinist.yaml"
        if not target.exists():
            target.write_text(config)

    def fail():
        raise OnboardingError("harness authentication unavailable")

    with pytest.raises(OnboardingError, match="authentication"):
        deliver_setup_pr(repo, github=github, initialize=initialize, validate=fail)
    result = deliver_setup_pr(repo, github=github, initialize=initialize)

    assert (repo / "machinist.yaml").read_text() == config
    assert git(remote, "rev-parse", result.branch) == result.commit_sha
    assert len(github.calls) == 1


def test_setup_pr_reuses_existing_draft_after_delivery(tmp_path):
    repo, _ = repository(tmp_path)
    github = FakeGitHub()

    def initialize():
        (repo / "machinist.yaml").write_text("version: 1\n")

    first = deliver_setup_pr(repo, github=github, initialize=initialize)
    second = deliver_setup_pr(repo, github=github, initialize=initialize)

    assert first == second
    assert len(github.calls) == 1


def test_setup_pr_resumes_its_existing_branch_from_clean_default_branch(tmp_path):
    repo, _ = repository(tmp_path)
    github = FakeGitHub()

    def initialize():
        (repo / "machinist.yaml").write_text("version: 1\n")

    first = deliver_setup_pr(repo, github=github, initialize=initialize)
    git(repo, "switch", "main")
    second = deliver_setup_pr(repo, github=github, initialize=initialize)

    assert first == second
    assert len(github.calls) == 1


def test_resuming_setup_refuses_unrelated_dirty_files_before_initialization(tmp_path):
    repo, _ = repository(tmp_path)
    git(repo, "switch", "-c", "chore/agentmachinist-setup")
    (repo / "notes.txt").write_text("user work\n")
    called = False

    def initialize():
        nonlocal called
        called = True

    with pytest.raises(OnboardingError, match="outside the setup allowlist"):
        deliver_setup_pr(repo, github=FakeGitHub(), initialize=initialize)

    assert not called
    assert (repo / "notes.txt").read_text() == "user work\n"


def test_resuming_setup_refuses_unrelated_commits(tmp_path):
    repo, _ = repository(tmp_path)
    git(repo, "switch", "-c", "chore/agentmachinist-setup")
    (repo / "README.md").write_text("unrelated branch work\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "unrelated work")

    with pytest.raises(OnboardingError, match="outside the setup allowlist"):
        deliver_setup_pr(repo, github=FakeGitHub(), initialize=lambda: None)


def test_onboard_setup_pr_validates_before_publish_without_deployment_gate(
    tmp_path, monkeypatch
):
    repo, _ = repository(tmp_path)
    github = FakeGitHub()
    monkeypatch.chdir(repo)
    monkeypatch.setattr("machinist.cli.GitHubClient", lambda: github)
    monkeypatch.setattr("machinist.cli._bound_github_client", lambda *a, **k: github)
    calls = []

    def doctor(*args, **kwargs):
        calls.append(kwargs)
        return DoctorReport(())

    monkeypatch.setattr("machinist.cli.run_doctor", doctor)
    result = CliRunner().invoke(main, ["onboard", "--setup-pr", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls and calls[0].get("check_deployment") is False
    assert "merge" in result.output.lower()
    assert result.output.rfind("merge") < result.output.rfind("doctor --run-gates")


def test_onboard_resumes_config_verbatim_and_repairs_missing_managed_files(
    tmp_path, monkeypatch
):
    repo, _ = repository(tmp_path)
    monkeypatch.chdir(repo)
    github = FakeGitHub()
    monkeypatch.setattr("machinist.cli._bound_github_client", lambda *a, **k: github)
    config = "# Our chosen model\nversion: 1\nharness:\n  name: codex\n  model: example-model\n"
    (repo / "machinist.yaml").write_text(config)
    (repo / ".gitignore").write_text("# user rules\n.env\n")
    (repo / "notes.txt").write_text("Unrelated developer notes\n")

    first = CliRunner().invoke(main, ["onboard", "--yes"])
    snapshot = {
        path.relative_to(repo): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    second = CliRunner().invoke(main, ["onboard", "--yes"])

    assert first.exit_code == second.exit_code == 0, first.output + second.output
    assert (repo / "machinist.yaml").read_text() == config
    assert (repo / "notes.txt").read_text() == "Unrelated developer notes\n"
    assert (repo / ".github/workflows/machinist-approve.yml").exists()
    assert (repo / ".gitignore").read_text() == (
        "# user rules\n.env\n/.machinist/runs/\n"
    )
    assert snapshot == {
        path.relative_to(repo): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert "preserv" in second.output.lower()


def test_onboard_rejects_conflicting_flags_without_overwriting_config(
    tmp_path, monkeypatch
):
    repo, _ = repository(tmp_path)
    monkeypatch.chdir(repo)
    config = "version: 1\nharness:\n  name: codex\n"
    (repo / "machinist.yaml").write_text(config)

    result = CliRunner().invoke(main, ["onboard", "--harness", "claude-code"])

    assert result.exit_code != 0
    assert "config set harness.name" in result.output
    assert "--force" not in result.output
    assert (repo / "machinist.yaml").read_text() == config


def test_onboard_receipt_checks_deployment_after_publishing(tmp_path, monkeypatch):
    repo, _ = repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "machinist.cli._bound_github_client", lambda *a, **k: FakeGitHub()
    )

    result = CliRunner().invoke(main, ["onboard", "--yes"])

    assert result.exit_code == 0, result.output
    assert result.output.index("git push") < result.output.index("doctor --run-gates")


def test_setup_pr_is_a_noop_when_adoption_is_already_on_default_branch(tmp_path):
    repo, _ = repository(tmp_path)
    (repo / "machinist.yaml").write_text("version: 1\n")
    git(repo, "add", "machinist.yaml")
    git(repo, "commit", "-m", "adoption already merged")

    result = deliver_setup_pr(repo, github=FakeGitHub(), initialize=lambda: None)

    assert result.pr is None
    assert git(repo, "branch", "--show-current") == "main"


def test_setup_resume_does_not_publish_unrelated_then_reverted_history(tmp_path):
    repo, _ = repository(tmp_path)
    git(repo, "switch", "-c", "chore/agentmachinist-setup")
    (repo / "private-notes.txt").write_text("unrelated content\n")
    git(repo, "add", "private-notes.txt")
    git(repo, "commit", "-m", "unrelated file")
    git(repo, "rm", "private-notes.txt")
    git(repo, "commit", "-m", "remove unrelated file")

    with pytest.raises(OnboardingError, match="outside the setup allowlist"):
        deliver_setup_pr(repo, github=FakeGitHub(), initialize=lambda: None)


def test_onboard_setup_pr_uses_origin_bound_github_client(tmp_path, monkeypatch):
    repo, _ = repository(tmp_path)
    monkeypatch.chdir(repo)
    github = FakeGitHub()
    bound = []

    def bind(config, *, repo_root):
        bound.append(repo_root)
        return github

    def unbound():
        raise AssertionError("setup must use origin-bound GitHub discovery")

    monkeypatch.setattr("machinist.cli._bound_github_client", bind)
    monkeypatch.setattr("machinist.cli.GitHubClient", unbound)
    monkeypatch.setattr("machinist.cli.run_doctor", lambda *a, **k: DoctorReport(()))

    result = CliRunner().invoke(main, ["onboard", "--setup-pr", "--yes"])

    assert result.exit_code == 0, result.output
    assert bound and all(root == repo for root in bound)
