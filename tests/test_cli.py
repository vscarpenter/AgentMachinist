"""Tests for the machinist CLI."""

from pathlib import Path

from click.testing import CliRunner

from machinist.cli import main
from machinist.config import load_config


def test_help_lists_all_commands():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "spec", "watch", "run", "status"):
        assert command in result.output


def test_init_creates_config_dirs_and_workflows():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"])

        assert result.exit_code == 0, result.output
        assert Path("machinist.yaml").is_file()
        assert Path(".machinist/specs/.gitkeep").is_file()
        assert Path(".github/workflows/machinist-spec.yml").is_file()
        assert Path(".github/workflows/machinist-approve.yml").is_file()


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


def test_init_no_workflows_skips_github_dir():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init", "--no-workflows"])

        assert result.exit_code == 0
        assert not Path(".github").exists()


def test_phase_stubs_exit_nonzero_with_milestone_note():
    runner = CliRunner()
    result = runner.invoke(main, ["watch"])
    assert result.exit_code != 0
    assert "not implemented" in result.output.lower()


def test_run_without_config_points_at_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["run", "42"])

        assert result.exit_code != 0
        assert "machinist init" in result.output


def test_run_wires_config_and_reports_ready_pr(monkeypatch):
    from machinist.github import PullRequest

    seen = {}

    def fake_run_execute_phase(issue_number, config, *, github, harness, workspace, test_runner):
        seen["issue"] = issue_number
        seen["harness"] = harness.name
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
        assert seen == {"issue": 42, "harness": "claude-code"}
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
    monkeypatch.setattr("machinist.cli.pipeline_status", lambda config, github: rows)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0, result.output
        assert "#3" in result.output and "awaiting spec" in result.output
        assert "#57" in result.output and "awaiting approval" in result.output
        assert "Spec: Add dark mode (#42)" in result.output


def test_status_with_no_activity_says_so(monkeypatch):
    monkeypatch.setattr("machinist.cli.pipeline_status", lambda config, github: [])

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

    def fake_run_spec_phase(issue_number, config, *, github, harness, workspace):
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
