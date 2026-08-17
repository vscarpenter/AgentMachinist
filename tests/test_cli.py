"""Tests for the machinist CLI."""

from pathlib import Path

from click.testing import CliRunner

from machinist.cli import main
from machinist.config import load_config


def test_help_lists_all_commands():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "spec", "approve", "watch", "run", "retry", "status", "doctor", "sync-workflows"):
        assert command in result.output


def test_init_creates_config_dirs_and_workflows():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"])

        assert result.exit_code == 0, result.output
        assert Path("machinist.yaml").is_file()
        assert Path(".machinist/specs/.gitkeep").is_file()
        assert Path(".github/workflows/machinist-approve.yml").is_file()


def test_sync_workflows_projects_configured_ci_dispatcher():
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        path = Path("machinist.yaml")
        path.write_text(path.read_text().replace("spec_source: local", "spec_source: github-actions"))

        result = runner.invoke(main, ["sync-workflows"])

        assert result.exit_code == 0, result.output
        assert Path(".github/workflows/machinist-spec.yml").is_file()


def test_sync_workflows_check_fails_on_drift():
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init"])
        approval = Path(".github/workflows/machinist-approve.yml")
        approval.write_text("drift\n")

        result = runner.invoke(main, ["sync-workflows", "--check"])

        assert result.exit_code != 0
        assert "drift" in result.output.lower()


def test_init_template_round_trips_through_schema():
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init"])
        config = load_config("machinist.yaml")  # raises ConfigError if template drifts
        assert config.harness.name.value == "claude-code"


def test_init_refuses_to_overwrite_without_force():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("machinist.yaml").write_text("version: 1\n")
        result = runner.invoke(main, ["init"])

        assert result.exit_code != 0
        assert "--force" in result.output
        assert Path("machinist.yaml").read_text() == "version: 1\n"


def test_init_force_overwrites():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("machinist.yaml").write_text("version: 1\n")
        result = runner.invoke(main, ["init", "--force"])

        assert result.exit_code == 0
        assert "harness" in Path("machinist.yaml").read_text()


def test_init_ensures_pipeline_labels(monkeypatch):
    ensured = []

    class FakeGitHub:
        def __init__(self, repo=None):
            pass

        def ensure_label(self, name, *, color, description):
            ensured.append(name)

    monkeypatch.setattr("machinist.cli.GitHubClient", FakeGitHub)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init", "--no-workflows"])

        assert result.exit_code == 0, result.output
        assert ensured == ["agent-task", "machinist:approved"]


def test_init_survives_label_creation_failure(monkeypatch):
    from machinist.github import GitHubError

    class FailingGitHub:
        def __init__(self, repo=None):
            pass

        def ensure_label(self, name, *, color, description):
            raise GitHubError("no git remotes found")

    monkeypatch.setattr("machinist.cli.GitHubClient", FailingGitHub)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init", "--no-workflows"])

        assert result.exit_code == 0, result.output
        assert "could not create" in result.output


def test_init_no_workflows_skips_github_dir():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init", "--no-workflows"])

        assert result.exit_code == 0
        assert not Path(".github").exists()


def test_watch_without_config_points_at_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["watch", "--once"])

        assert result.exit_code != 0
        assert "machinist init" in result.output


def test_watch_once_prints_dispatch_events(monkeypatch):
    events = [
        "spec: issue #7 → draft PR #97 (https://github.com/x/y/pull/97)",
        "error: execute for issue #8 failed: boom",
    ]
    monkeypatch.setattr("machinist.cli.watch_once", lambda *args, **kwargs: events)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--once"])

        assert result.exit_code == 0, result.output
        assert "issue #7" in result.output
        assert "failed: boom" in result.output


def test_watch_once_wires_notifier_with_watch_title(monkeypatch):
    notified = []
    monkeypatch.setattr(
        "machinist.cli.notify",
        lambda title, message: notified.append((title, message)),
    )

    def fake_watch_once(config, github, *, run_spec, run_execute, state, notify):
        notify("spec for issue #7 failed: boom")
        return []

    monkeypatch.setattr("machinist.cli.watch_once", fake_watch_once)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--once"])

        assert result.exit_code == 0, result.output
        assert notified == [("machinist watch", "spec for issue #7 failed: boom")]


def test_watch_once_with_empty_pipeline_says_so(monkeypatch):
    monkeypatch.setattr("machinist.cli.watch_once", lambda *args, **kwargs: [])

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--once"])

        assert result.exit_code == 0
        assert "nothing to do" in result.output.lower()


def test_run_without_config_points_at_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["run", "42"])

        assert result.exit_code != 0
        assert "machinist init" in result.output


def test_run_wires_config_and_reports_ready_pr(monkeypatch):
    from machinist.github import PullRequest

    seen = {}

    def fake_run_execute_phase(issue_number, config, *, github, harness, workspace, test_runner, force, claim):
        seen["issue"] = issue_number
        seen["harness"] = harness.name
        seen["force"] = force
        return PullRequest(
            number=57, title="Spec: Add dark mode (#42)",
            url="https://github.com/x/y/pull/57",
            branch="agent/issue-42", is_draft=False, labels=["machinist:approved"],
        )

    monkeypatch.setattr("machinist.cli.run_execute_phase", fake_run_execute_phase)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["run", "42"])

        assert result.exit_code == 0, result.output
        assert seen == {"issue": 42, "harness": "claude-code", "force": False}
        assert "pull/57" in result.output
        assert "ready for review" in result.output.lower()


def test_run_renders_machinist_errors_without_traceback(monkeypatch):
    from machinist.phases.execute import ExecutePhaseError

    def failing(*args, **kwargs):
        raise ExecutePhaseError("PR #57 is not approved")

    monkeypatch.setattr("machinist.cli.run_execute_phase", failing)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["run", "42"])

        assert result.exit_code != 0
        assert "not approved" in result.output
        assert "Traceback" not in result.output


def test_status_without_config_points_at_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["status"])

        assert result.exit_code != 0
        assert "machinist init" in result.output


def test_status_renders_rows(monkeypatch):
    from machinist.phases.status import StatusRow

    rows = [
        StatusRow(kind="issue", number=3, title="Fix frobnicator",
                  state="awaiting spec", url="https://github.com/x/y/issues/3"),
        StatusRow(kind="pr", number=57, title="Spec: Add dark mode (#42)",
                  state="awaiting approval", url="https://github.com/x/y/pull/57"),
    ]
    monkeypatch.setattr("machinist.cli.pipeline_status", lambda config, github, **kwargs: rows)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0, result.output
        assert "#3" in result.output and "awaiting spec" in result.output
        assert "#57" in result.output and "awaiting approval" in result.output
        assert "Spec: Add dark mode (#42)" in result.output


def test_status_with_no_activity_says_so(monkeypatch):
    monkeypatch.setattr("machinist.cli.pipeline_status", lambda config, github, **kwargs: [])

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "no machinist activity" in result.output.lower()


def test_spec_without_config_points_at_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["spec", "42"])

        assert result.exit_code != 0
        assert "machinist init" in result.output


def test_spec_wires_config_and_reports_pr_url(monkeypatch):
    from machinist.github import DraftPR

    seen = {}

    def fake_run_spec_phase(issue_number, config, *, github, harness, workspace, claim):
        seen["issue"] = issue_number
        seen["harness"] = harness.name
        seen["repo"] = github.repo
        seen["strategy"] = workspace.config.strategy.value
        return DraftPR(number=57, url="https://github.com/vscarpenter/demo/pull/57")

    monkeypatch.setattr("machinist.cli.run_spec_phase", fake_run_spec_phase)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["spec", "42"])

        assert result.exit_code == 0, result.output
        assert seen == {
            "issue": 42,
            "harness": "claude-code",
            "repo": None,
            "strategy": "worktree",
        }
        assert "pull/57" in result.output


def test_spec_renders_machinist_errors_without_traceback(monkeypatch):
    from machinist.harness.base import HarnessError

    def failing_run_spec_phase(*args, **kwargs):
        raise HarnessError("claude-code timed out after 10 minutes")

    monkeypatch.setattr("machinist.cli.run_spec_phase", failing_run_spec_phase)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["spec", "42"])

        assert result.exit_code != 0
        assert "timed out" in result.output
        assert "Traceback" not in result.output
