"""CLI contracts for explicit background operation and sparse reporting."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from machinist import background_cli
from machinist.background import BackgroundError
from machinist.cli import main
from machinist.config import ConfigError, MachinistConfig
from machinist.github import GitHubError


@pytest.fixture
def background_command(monkeypatch, tmp_path):
    context = SimpleNamespace(
        root=tmp_path,
        config=MachinistConfig(),
        events=[],
        rows=[],
        error=None,
        notices=[],
    )

    class Worker:
        def __init__(self, config, *, repo_root, notify=None):
            context.events.append(("worker", config, repo_root))
            self.notify = notify

        def doctor(self):
            context.events.append(("doctor",))
            if context.error:
                raise context.error
            return ["Container available", "Repository valid"]

        def run_once(self):
            context.events.append(("run_once",))
            if context.error:
                raise context.error
            if self.notify is not None:
                for notice in context.notices:
                    self.notify(notice)
                context.notices.clear()
            return context.rows

        def status(self):
            context.events.append(("status",))
            return context.rows

        def cancel(self, task_id):
            context.events.append(("cancel", task_id))

        def retry(self, task_id=None, *, issue_number=None):
            if task_id is not None:
                context.events.append(("retry", task_id))
            else:
                context.events.append(("retry_issue", issue_number))

    def load(path):
        context.events.append(("load", path))
        return context.config

    monkeypatch.setattr(background_cli, "BackgroundWorker", Worker)
    monkeypatch.setattr(background_cli, "find_repository_root", lambda cwd: tmp_path)
    monkeypatch.setattr(background_cli, "load_config", load)
    return context


def test_background_commands_are_registered_and_help_needs_no_configuration():
    result = CliRunner().invoke(main, ["background", "--help"])
    assert result.exit_code == 0, result.output
    for name in ("doctor", "run", "status", "cancel", "retry", "--config"):
        assert name in result.output
    assert "background" in CliRunner().invoke(main, ["--help"]).output


def test_doctor_uses_repository_root_configuration_without_executing(
    background_command,
):
    result = CliRunner().invoke(main, ["background", "doctor"])
    assert result.exit_code == 0, result.output
    assert background_command.events[0] == (
        "load",
        background_command.root / "machinist.yaml",
    )
    assert ("doctor",) in background_command.events
    assert ("run_once",) not in background_command.events
    assert "Container available" in result.output


def test_explicit_configuration_path_is_preserved(background_command, tmp_path):
    selected = tmp_path / "pilot.yaml"
    result = CliRunner().invoke(
        main, ["background", "--config", str(selected), "status"]
    )
    assert result.exit_code == 0, result.output
    assert background_command.events[0] == ("load", selected)


def test_explicit_relative_configuration_is_relative_to_callers_directory(
    background_command, monkeypatch, tmp_path
):
    caller = tmp_path / "child"
    caller.mkdir()
    monkeypatch.chdir(caller)
    result = CliRunner().invoke(
        main, ["background", "--config", "pilot.yaml", "status"]
    )
    assert result.exit_code == 0, result.output
    assert background_command.events[0] == ("load", caller / "pilot.yaml")


def test_status_json_is_clean_structured_output_without_readiness(background_command):
    background_command.rows = [
        {"task_id": "T1", "status": "awaiting_ci", "message": "tests: pending"}
    ]
    result = CliRunner().invoke(main, ["background", "status", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == background_command.rows
    assert ("doctor",) not in background_command.events
    assert ("run_once",) not in background_command.events


def test_empty_status_explains_no_background_work(background_command):
    result = CliRunner().invoke(main, ["background", "status"])
    assert result.exit_code == 0, result.output
    assert "No background Tasks" in result.output


def test_generated_background_status_action_selects_task(background_command):
    background_command.rows = [
        {"task_id": "T1", "status": "awaiting_ci", "message": "tests: pending"},
        {"task_id": "T2", "status": "ready", "message": "ready"},
    ]
    result = CliRunner().invoke(main, ["background", "status", "T1", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == background_command.rows[:1]


def test_run_once_performs_one_pass_without_sleep(background_command, monkeypatch):
    monkeypatch.setattr(
        background_cli.time, "sleep", lambda *_: pytest.fail("one-shot slept")
    )
    background_command.rows = [
        {
            "task_id": "T1",
            "status": "ready",
            "title": "Fix escaping",
            "message": "Ready for review",
            "url": "https://github.com/owner/repo/pull/1",
        }
    ]
    result = CliRunner().invoke(main, ["background", "run", "--once"])
    assert result.exit_code == 0, result.output
    assert background_command.events.count(("run_once",)) == 1
    assert "T1" in result.output and "Ready for review" in result.output
    assert "https://github.com/owner/repo/pull/1" in result.output


@pytest.mark.parametrize(
    "status,code",
    [
        ("failed", 1),
        ("needs_attention", 1),
        ("cancelled", 1),
        ("awaiting_ci", 0),
        ("ready", 0),
    ],
)
def test_run_once_exit_code_reflects_current_outcomes(background_command, status, code):
    background_command.rows = [
        {"task_id": "T1", "status": status, "message": "Current result"}
    ]
    result = CliRunner().invoke(main, ["background", "run", "--once"])
    assert result.exit_code == code, result.output
    assert status in result.output


def test_persistent_worker_reloads_config_and_only_prints_new_notices(
    background_command, monkeypatch
):
    background_command.rows = [
        {"task_id": "T1", "status": "awaiting_ci", "message": "unchanged progress"}
    ]
    background_command.notices = ["T1: Ready for review"]
    intervals = []

    def sleep(interval):
        intervals.append(interval)
        if len(intervals) == 1:
            background_command.config = MachinistConfig.model_validate(
                {"background": {"poll_interval_seconds": 15}}
            )
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(background_cli.time, "sleep", sleep)
    result = CliRunner().invoke(main, ["background", "run"])
    assert result.exit_code == 0, result.output
    assert intervals == [60, 15]
    assert (
        len([event for event in background_command.events if event[0] == "load"]) == 2
    )
    assert background_command.events.count(("run_once",)) == 2
    assert result.output.count("T1: Ready for review") == 1
    assert "unchanged progress" not in result.output
    assert "stopped" in result.output.lower()


@pytest.mark.parametrize("command", ["cancel", "retry"])
def test_controls_are_explicit_and_do_not_run_readiness_or_tasks(
    background_command, command
):
    result = CliRunner().invoke(main, ["background", command, "T1"])
    assert result.exit_code == 0, result.output
    assert (command, "T1") in background_command.events
    assert ("doctor",) not in background_command.events
    assert ("run_once",) not in background_command.events
    if command == "retry":
        assert "next worker pass" in result.output


@pytest.mark.parametrize("task_id", ["1", "T0", "T01", "../T1", "t1"])
def test_invalid_task_identifiers_fail_before_control_mutation(
    background_command, task_id
):
    result = CliRunner().invoke(main, ["background", "retry", task_id])
    assert result.exit_code == 2
    assert "T1" in result.output
    assert not any(event[0] == "retry" for event in background_command.events)


@pytest.mark.parametrize(
    "error", [BackgroundError, GitHubError, ConfigError, OSError, ValueError]
)
def test_expected_errors_are_sanitized_and_have_no_traceback(background_command, error):
    background_command.error = error("GH_TOKEN=do-not-print\x1b[31m unavailable")
    result = CliRunner().invoke(main, ["background", "run", "--once"])
    assert result.exit_code == 1
    assert "do-not-print" not in result.output
    assert "\x1b" not in result.output
    assert "unavailable" in result.output
    assert "Traceback" not in result.output


def test_disabled_config_never_constructs_runtime_or_executes_host(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(background_cli, "find_repository_root", lambda cwd: tmp_path)
    monkeypatch.setattr(background_cli, "load_config", lambda path: MachinistConfig())
    monkeypatch.setattr(
        "machinist.background.ContainerRuntime",
        lambda *a, **k: pytest.fail("runtime constructed"),
    )
    result = CliRunner().invoke(main, ["background", "run", "--once"])
    assert result.exit_code == 1
    assert "disabled" in result.output
    assert not (tmp_path / ".machinist/runs").exists()


def test_configuration_error_is_sanitized_before_worker_construction(
    background_command, monkeypatch
):
    def fail(path):
        raise ConfigError("password=private configuration unavailable")

    monkeypatch.setattr(background_cli, "load_config", fail)
    result = CliRunner().invoke(main, ["background", "status"])
    assert result.exit_code == 1
    assert "private" not in result.output
    assert not any(event[0] == "worker" for event in background_command.events)


def test_failed_intake_can_be_retried_by_issue_without_runtime_or_execution(
    background_command,
):
    result = CliRunner().invoke(main, ["background", "retry", "--issue", "12"])
    assert result.exit_code == 0, result.output
    assert ("retry_issue", 12) in background_command.events
    assert ("doctor",) not in background_command.events
    assert ("run_once",) not in background_command.events
    assert "issue #12" in result.output


@pytest.mark.parametrize("args", [[], ["T1", "--issue", "12"], ["--issue", "0"]])
def test_retry_requires_exactly_one_valid_selector(background_command, args):
    result = CliRunner().invoke(main, ["background", "retry", *args])
    assert result.exit_code == 2, result.output
    assert not any(
        event[0] in {"retry", "retry_issue"} for event in background_command.events
    )


def test_background_subcommand_help_does_not_require_git_or_configuration(monkeypatch):
    monkeypatch.setattr(
        background_cli,
        "find_repository_root",
        lambda *_: pytest.fail("help inspected Git"),
    )
    result = CliRunner().invoke(main, ["background", "run", "--help"])
    assert result.exit_code == 0, result.output
    assert "--once" in result.output


def test_human_status_sanitizes_external_text(background_command):
    background_command.rows = [
        {
            "task_id": "T1",
            "status": "ready",
            "title": "Bad\x1b]52;private\x07 title",
            "message": "GH_TOKEN=secret unavailable",
        }
    ]
    result = CliRunner().invoke(main, ["background", "status"])
    assert result.exit_code == 0, result.output
    assert "private" not in result.output and "secret" not in result.output
    assert "\x1b" not in result.output
