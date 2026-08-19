"""Tests for the machinist CLI."""

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

from click.testing import CliRunner

from machinist.cli import main
from machinist.config import load_config

_BaseCliRunner = CliRunner


class _GitCliRunner(CliRunner):
    @contextmanager
    def isolated_filesystem(self, *args, **kwargs):
        with super().isolated_filesystem(*args, **kwargs) as directory:
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            yield directory


CliRunner = _GitCliRunner


def test_help_lists_all_commands():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in (
        "init",
        "spec",
        "approve",
        "watch",
        "run",
        "amend",
        "retry",
        "cancel",
        "queue",
        "repo",
        "service",
        "status",
        "runs",
        "doctor",
        "sync-workflows",
        "clean",
        "inspect",
        "config",
    ):
        assert command in result.output


def test_config_commands_validate_show_schema_and_set():
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])

        validated = runner.invoke(main, ["config", "validate", "--json"])
        shown = runner.invoke(main, ["config", "show", "--json"])
        schema = runner.invoke(main, ["config", "schema"])
        updated = runner.invoke(
            main,
            ["config", "set", "github.poll_interval_seconds", "120"],
        )

        assert validated.exit_code == 0, validated.output
        assert json.loads(validated.output)["ok"] is True
        assert json.loads(shown.output)["harness"]["spec"]["name"] == "claude-code"
        assert json.loads(schema.output)["properties"]["version"]["const"] == 1
        assert updated.exit_code == 0, updated.output
        assert load_config().github.poll_interval_seconds == 120


def test_config_validate_reports_machine_readable_failure():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("machinist.yaml").write_text("version: 99\n")

        result = runner.invoke(main, ["config", "validate", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "validation"


def test_init_creates_config_dirs_and_workflows():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"])

        assert result.exit_code == 0, result.output
        assert Path("machinist.yaml").is_file()
        assert Path(".machinist/specs/.gitkeep").is_file()
        assert Path(".github/workflows/machinist-approve.yml").is_file()


def test_init_refuses_non_git_directory_before_writing_anything():
    runner = _BaseCliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"])

        assert result.exit_code != 0
        assert "Git repository" in result.output
        assert not Path("machinist.yaml").exists()
        assert not Path(".machinist").exists()


def test_init_safely_serializes_yaml_significant_test_command():
    runner = CliRunner()
    with runner.isolated_filesystem():
        command = "python -c 'print(\"a: b # c\")'"
        result = runner.invoke(
            main,
            ["init", "--no-workflows", "--test-cmd", command],
        )

        assert result.exit_code == 0, result.output
        assert load_config().tests.command == command


def test_init_idempotently_ignores_runtime_state_only():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path(".gitignore").write_text("dist/\n")

        first = runner.invoke(main, ["init", "--no-workflows"])
        second = runner.invoke(main, ["init", "--no-workflows", "--force"])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        lines = Path(".gitignore").read_text().splitlines()
        assert lines.count("/.machinist/runs/") == 1
        assert "/.machinist/" not in lines


def test_sync_workflows_projects_configured_ci_dispatcher():
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        path = Path("machinist.yaml")
        path.write_text(
            path.read_text()
            .replace("spec_source: local", "spec_source: github-actions")
            .replace("manage_workflows: false", "manage_workflows: true")
        )

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

        assert result.exit_code != 0
        assert "issue #7" in result.output
        assert "failed: boom" in result.output
        assert "dispatch" in result.output.lower()


def test_watch_once_wires_notifier_with_watch_title(monkeypatch):
    from machinist.notify import NotificationResult, NotificationStatus

    notified = []

    def fake_notify(config, event, title, message, **kwargs):
        notified.append((event.value, title, message))
        return NotificationResult(
            NotificationStatus.DELIVERED,
            "desktop",
            event.value,
            "test-key",
        )

    monkeypatch.setattr("machinist.cli.notify_event", fake_notify)

    def fake_watch_once(
        config,
        github,
        *,
        run_spec,
        run_execute,
        state,
        notify,
        notify_stale,
        max_tasks,
        admit,
    ):
        notify("spec for issue #7 failed: boom")
        return []

    monkeypatch.setattr("machinist.cli.watch_once", fake_watch_once)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--once"])

        assert result.exit_code == 0, result.output
        assert notified == [
            (
                "failure",
                "Machinist task failed",
                "spec for issue #7 failed: boom",
            )
        ]


def test_watch_once_with_empty_pipeline_says_so(monkeypatch):
    monkeypatch.setattr("machinist.cli.watch_once", lambda *args, **kwargs: [])

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--once"])

        assert result.exit_code == 0
        assert "nothing to do" in result.output.lower()


def test_watch_rejects_interval_below_rate_safe_minimum():
    result = CliRunner().invoke(main, ["watch", "--once", "--interval", "9"])

    assert result.exit_code != 0
    assert "range" in result.output.lower()


def test_watch_dry_run_lists_tasks_without_dispatching(monkeypatch):
    from machinist.phases.status import StatusRow
    from machinist.phases.watch import WatchTask

    monkeypatch.setattr(
        "machinist.cli.plan_watch_tasks",
        lambda config, github: (
            WatchTask(
                "execute",
                42,
                StatusRow(
                    kind="pr",
                    number=57,
                    title="Spec",
                    state="approved",
                    url="https://github.com/x/y/pull/57",
                    issue_number=42,
                ),
            ),
        ),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "eligible: execute issue #42" in result.output


def test_queue_commands_pause_defer_show_resume_and_allow():
    runner = CliRunner()
    with runner.isolated_filesystem():
        paused = runner.invoke(main, ["queue", "pause", "--reason", "on battery"])
        deferred = runner.invoke(
            main,
            ["queue", "defer", "42", "--reason", "waiting for review"],
        )
        shown = runner.invoke(main, ["queue", "show", "--json"])

        assert paused.exit_code == 0, paused.output
        assert deferred.exit_code == 0, deferred.output
        state = json.loads(shown.output)
        assert state["paused"] is True
        assert state["deferred"]["42"]["reason"] == "waiting for review"

        resumed = runner.invoke(main, ["queue", "resume"])
        allowed = runner.invoke(main, ["queue", "allow", "42"])
        final = json.loads(runner.invoke(main, ["queue", "show", "--json"]).output)

        assert resumed.exit_code == 0
        assert allowed.exit_code == 0
        assert final["paused"] is False
        assert final["deferred"] == {}


def test_run_without_config_points_at_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["run", "42"])

        assert result.exit_code != 0
        assert "machinist init" in result.output


def test_run_wires_config_and_reports_ready_pr(monkeypatch):
    from machinist.github import PullRequest

    seen = {}

    def fake_run_execute_phase(
        issue_number,
        config,
        *,
        github,
        harness,
        workspace,
        test_runner,
        force,
        claim,
        recovery,
        cancel_check,
    ):
        seen["issue"] = issue_number
        seen["harness"] = harness.name
        seen["force"] = force
        seen["recovery"] = recovery
        assert callable(cancel_check)
        return PullRequest(
            number=57,
            title="Spec: Add dark mode (#42)",
            url="https://github.com/x/y/pull/57",
            branch="agent/issue-42",
            is_draft=False,
            labels=["machinist:approved"],
        )

    monkeypatch.setattr("machinist.cli.run_execute_phase", fake_run_execute_phase)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["run", "42"])

        assert result.exit_code == 0, result.output
        assert seen == {
            "issue": 42,
            "harness": "claude-code",
            "force": False,
            "recovery": "fresh",
        }
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


def test_amend_repeats_successful_execute_with_explicit_feedback(monkeypatch):
    from machinist.github import PullRequest
    from machinist.lifecycle import Phase, TaskLifecycle

    seen = {}

    def fake_execute(issue_number, config, **kwargs):
        seen.update(
            issue=issue_number,
            force=kwargs["force"],
            recovery=kwargs["recovery"],
            feedback=kwargs["feedback"],
            attempt=kwargs["claim"].attempt,
        )
        return PullRequest(
            number=57,
            title="Task",
            url="https://github.com/x/y/pull/57",
            branch="agent/issue-42",
            is_draft=False,
        )

    monkeypatch.setattr("machinist.cli.run_execute_phase", fake_execute)
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        TaskLifecycle(Path(".machinist/runs")).run(
            42,
            Phase.EXECUTE,
            lambda claim: PullRequest(
                number=57,
                title="Task",
                url="https://github.com/x/y/pull/57",
                branch="agent/issue-42",
                is_draft=False,
            ),
        )

        result = runner.invoke(
            main,
            ["amend", "42", "--feedback", "Keep the public API stable."],
        )

        assert result.exit_code == 0, result.output
        assert seen == {
            "issue": 42,
            "force": True,
            "recovery": "fresh",
            "feedback": "Keep the public API stable.",
            "attempt": 2,
        }


def test_status_without_config_points_at_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["status"])

        assert result.exit_code != 0
        assert "machinist init" in result.output


def test_status_renders_rows(monkeypatch):
    from machinist.phases.status import StatusRow

    rows = [
        StatusRow(
            kind="issue",
            number=3,
            title="Fix frobnicator",
            state="awaiting spec",
            url="https://github.com/x/y/issues/3",
        ),
        StatusRow(
            kind="pr",
            number=57,
            title="Spec: Add dark mode (#42)",
            state="awaiting approval",
            url="https://github.com/x/y/pull/57",
        ),
    ]
    monkeypatch.setattr(
        "machinist.cli.pipeline_status", lambda config, github, **kwargs: rows
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0, result.output
        assert "#3" in result.output and "awaiting spec" in result.output
        assert "#57" in result.output and "awaiting approval" in result.output
        assert "Spec: Add dark mode (#42)" in result.output


def test_status_with_no_activity_says_so(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.pipeline_status", lambda config, github, **kwargs: []
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "no machinist activity" in result.output.lower()


def test_portfolio_repo_commands_and_status_all_json():
    from machinist.lifecycle import Phase, TaskLifecycle

    runner = CliRunner()
    with runner.isolated_filesystem():
        registry = Path("portfolio.json").resolve()
        TaskLifecycle(Path(".machinist/runs")).run(7, Phase.SPEC, lambda claim: None)

        added = runner.invoke(
            main,
            ["repo", "add", ".", "--registry", str(registry)],
        )
        listed = runner.invoke(
            main,
            ["repo", "list", "--json", "--registry", str(registry)],
        )
        all_status = runner.invoke(
            main,
            ["status", "--all", "--json", "--registry", str(registry)],
        )

        assert added.exit_code == 0, added.output
        assert json.loads(listed.output)["repositories"] == [str(Path.cwd())]
        payload = json.loads(all_status.output)
        assert payload["repositories"][0]["report"]["current"][0]["issue"] == 7


def test_service_commands_manage_launchd_and_show_bounded_logs(monkeypatch):
    from machinist.service import ServiceStatus

    calls = []

    class FakeService:
        label = "io.github.test.machinist"
        plist_path = Path("service.plist")
        logs_dir = Path(".machinist/service")
        log_paths = (
            logs_dir / "watch.stdout.log",
            logs_dir / "watch.stderr.log",
        )

        def stop(self):
            calls.append("stop")

        def install(self):
            calls.append("install")
            return self.plist_path

        def bootstrap(self):
            calls.append("bootstrap")

        def start(self):
            calls.append("start")

        def restart(self):
            calls.append("restart")

        def status(self):
            calls.append("status")
            return ServiceStatus(self.label, True, True, 0, "loaded")

        def uninstall(self):
            calls.append("uninstall")
            return True

    service = FakeService()
    monkeypatch.setattr("machinist.cli._launchd_service", lambda: service)

    runner = CliRunner()
    with runner.isolated_filesystem():
        service.logs_dir.mkdir(parents=True)
        service.log_paths[0].write_text("old\nnew\n")

        installed = runner.invoke(main, ["service", "install"])
        started = runner.invoke(main, ["service", "start"])
        restarted = runner.invoke(main, ["service", "restart"])
        stopped = runner.invoke(main, ["service", "stop"])
        status = runner.invoke(main, ["service", "status", "--json"])
        logs = runner.invoke(main, ["service", "logs", "--lines", "1"])
        uninstalled = runner.invoke(main, ["service", "uninstall"])

        assert installed.exit_code == 0, installed.output
        assert started.exit_code == 0, started.output
        assert restarted.exit_code == 0, restarted.output
        assert stopped.exit_code == 0, stopped.output
        assert json.loads(status.output)["loaded"] is True
        assert "new" in logs.output and "old" not in logs.output
        assert "(no log yet)" in logs.output
        assert uninstalled.exit_code == 0, uninstalled.output
        assert calls == [
            "stop",
            "install",
            "bootstrap",
            "start",
            "start",
            "restart",
            "stop",
            "status",
            "uninstall",
        ]


def test_spec_without_config_points_at_init():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["spec", "42"])

        assert result.exit_code != 0
        assert "machinist init" in result.output


def test_spec_wires_config_and_reports_pr_url(monkeypatch):
    from machinist.github import DraftPR

    seen = {}

    def fake_run_spec_phase(
        issue_number, config, *, github, harness, workspace, claim, revise, attempt
    ):
        seen["issue"] = issue_number
        seen["harness"] = harness.name
        seen["repo"] = github.repo
        seen["strategy"] = workspace.config.strategy.value
        seen["revise"] = revise
        seen["attempt"] = attempt
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
            "revise": False,
            "attempt": None,
        }
        assert "pull/57" in result.output


def test_spec_revise_repeats_successful_attempt_on_existing_pr(monkeypatch):
    from machinist.github import DraftPR
    from machinist.lifecycle import Phase, TaskLifecycle

    seen = []

    def fake_run_spec_phase(
        issue_number, config, *, github, harness, workspace, claim, revise, attempt
    ):
        seen.append((issue_number, revise, claim.attempt, attempt))
        return DraftPR(number=57, url="https://github.com/x/y/pull/57")

    monkeypatch.setattr("machinist.cli.run_spec_phase", fake_run_spec_phase)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        TaskLifecycle(Path(".machinist/runs")).run(
            42,
            Phase.SPEC,
            lambda claim: DraftPR(number=57, url="https://github.com/x/y/pull/57"),
        )

        result = runner.invoke(main, ["spec", "42", "--revise"])

        assert result.exit_code == 0, result.output
        assert seen == [(42, True, 2, 2)]
        assert "Revised draft PR #57" in result.output


def test_spec_abandon_closes_pr_removes_labels_and_records_state(monkeypatch):
    from machinist.github import DraftPR, Issue, PullRequest
    from machinist.lifecycle import Phase, RunStatus, TaskLifecycle

    calls = []

    class FakeGitHub:
        def __init__(self, repo=None):
            pass

        def pr_for_branch(self, branch):
            return PullRequest(
                number=57,
                title="Spec",
                url="https://github.com/x/y/pull/57",
                branch=branch,
                is_draft=True,
                head_sha="a" * 40,
                labels=["machinist:approved"],
            )

        def get_issue(self, number):
            return Issue(
                number=number,
                title="Task",
                body="",
                url=f"https://github.com/x/y/issues/{number}",
                labels=["agent-task"],
            )

        def remove_issue_label(self, number, label):
            calls.append(("remove_issue_label", number, label))

        def remove_pr_label(self, number, label):
            calls.append(("remove_pr_label", number, label))

        def close_pr(self, number):
            calls.append(("close_pr", number))

    monkeypatch.setattr("machinist.cli.GitHubClient", FakeGitHub)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        lifecycle.run(
            42,
            Phase.SPEC,
            lambda claim: DraftPR(number=57, url="https://github.com/x/y/pull/57"),
        )

        result = runner.invoke(
            main,
            ["spec", "42", "--abandon", "--reason", "requirements changed"],
        )

        assert result.exit_code == 0, result.output
        assert lifecycle.record(42, Phase.SPEC).status is RunStatus.ABANDONED
        assert calls == [
            ("remove_issue_label", 42, "agent-task"),
            ("remove_pr_label", 57, "machinist:approved"),
            ("close_pr", 57),
        ]


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


def test_spec_dry_run_prints_preview_without_lifecycle_record(monkeypatch):
    from machinist.lifecycle import Phase, TaskLifecycle

    monkeypatch.setattr(
        "machinist.cli.preview_spec_phase",
        lambda *args, **kwargs: "## Preview\n\nNo delivery.\n",
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])

        result = runner.invoke(main, ["spec", "42", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "## Preview" in result.output
        assert TaskLifecycle(Path(".machinist/runs")).record(42, Phase.SPEC) is None


def test_init_with_harness_and_test_cmd_flags():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            [
                "init",
                "--no-workflows",
                "--harness",
                "opencode",
                "--test-cmd",
                "pytest -k fast",
            ],
        )
        assert result.exit_code == 0
        config = load_config("machinist.yaml")
        assert config.harness.name.value == "opencode"
        assert config.tests.command == "pytest -k fast"


def test_init_auto_detects_test_runner():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text("[project]\nname='demo'\n")
        result = runner.invoke(main, ["init", "--no-workflows"])
        assert result.exit_code == 0
        assert "auto-detected test runner: 'uv run pytest'" in result.output
        config = load_config("machinist.yaml")
        assert config.tests.command == "uv run pytest"


def test_approve_resolves_issue_number(monkeypatch):
    from machinist.github import PullRequest

    approved_prs = []

    class FakeGitHub:
        def __init__(self, repo=None):
            pass

        def open_machinist_prs(self, branch_prefix):
            return [
                PullRequest(
                    number=18,
                    title="Spec: Add export (#42)",
                    url="https://github.com/x/y/pull/18",
                    branch="agent/issue-42",
                    is_draft=True,
                    head_sha="0123456789abcdef0123456789abcdef01234567",
                )
            ]

        def pr_for_branch(self, branch):
            return self.open_machinist_prs("agent/")[0]

        def approve_pr(self, number, *, label, head_sha):
            approved_prs.append((number, label, head_sha))

    monkeypatch.setattr("machinist.cli.GitHubClient", FakeGitHub)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        # Approve using the issue number 42 rather than PR number 18
        result = runner.invoke(main, ["approve", "42"])
        assert result.exit_code == 0, result.output
        assert "Approved PR #18" in result.output
        assert approved_prs == [
            (18, "machinist:approved", "0123456789abcdef0123456789abcdef01234567")
        ]


def test_approve_refuses_ambiguous_target_and_supports_explicit_issue_or_pr(
    monkeypatch,
):
    from machinist.github import PullRequest

    approved_prs = []

    class FakeGitHub:
        def __init__(self, repo=None):
            pass

        def open_machinist_prs(self, branch_prefix):
            return [
                PullRequest(
                    number=42,
                    title="Spec: Different task (#9)",
                    url="https://github.com/x/y/pull/42",
                    branch="agent/issue-9",
                    is_draft=True,
                    head_sha="1" * 40,
                ),
                PullRequest(
                    number=57,
                    title="Spec: Target task (#42)",
                    url="https://github.com/x/y/pull/57",
                    branch="agent/issue-42",
                    is_draft=True,
                    head_sha="2" * 40,
                ),
            ]

        def approve_pr(self, number, *, label, head_sha):
            approved_prs.append((number, head_sha))

    monkeypatch.setattr("machinist.cli.GitHubClient", FakeGitHub)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])

        ambiguous = runner.invoke(main, ["approve", "42"])
        by_issue = runner.invoke(main, ["approve", "--issue", "42"])
        by_pr = runner.invoke(main, ["approve", "--pr", "42"])

        assert ambiguous.exit_code != 0
        assert "ambiguous" in ambiguous.output.lower()
        assert by_issue.exit_code == 0, by_issue.output
        assert by_pr.exit_code == 0, by_pr.output
        assert approved_prs == [(57, "2" * 40), (42, "1" * 40)]


def test_retry_with_run_flag(monkeypatch):
    from machinist.github import PullRequest
    from machinist.lifecycle import Phase, TaskLifecycle

    executed = []

    def fake_run_execute_phase(
        issue_number,
        config,
        *,
        github,
        harness,
        workspace,
        test_runner,
        claim,
        recovery,
        cancel_check,
    ):
        executed.append((issue_number, recovery))
        return PullRequest(
            number=18,
            title="Spec: Task (#42)",
            url="https://github.com/x/y/pull/18",
            branch="agent/issue-42",
            is_draft=False,
            labels=["machinist:approved"],
        )

    monkeypatch.setattr("machinist.cli.run_execute_phase", fake_run_execute_phase)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        # Simulate a failed execute phase
        try:
            lifecycle.run(
                42,
                Phase.EXECUTE,
                lambda claim: (_ for _ in ()).throw(RuntimeError("fail")),
            )
        except RuntimeError:
            pass

        result = runner.invoke(main, ["retry", "42", "--phase", "execute", "--run"])
        assert result.exit_code == 0, result.output
        assert "Issue #42 execute is retryable" in result.output
        assert "PR #18 implemented" in result.output
        assert executed == [(42, "fresh")]


def test_retry_expected_lifecycle_error_is_rendered_without_traceback():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["retry", "42", "--phase", "execute"])

        assert result.exit_code != 0
        assert "no Task Run" in result.output
        assert "Traceback" not in result.output
        assert not isinstance(result.exception, TypeError)


def test_run_with_retry_flag(monkeypatch):
    from machinist.github import PullRequest
    from machinist.lifecycle import Phase, TaskLifecycle

    executed = []

    def fake_run_execute_phase(
        issue_number,
        config,
        *,
        github,
        harness,
        workspace,
        test_runner,
        force,
        claim,
        recovery,
        cancel_check,
    ):
        executed.append((issue_number, recovery))
        return PullRequest(
            number=18,
            title="Spec: Task (#42)",
            url="https://github.com/x/y/pull/18",
            branch="agent/issue-42",
            is_draft=False,
            labels=["machinist:approved"],
        )

    monkeypatch.setattr("machinist.cli.run_execute_phase", fake_run_execute_phase)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        # Simulate a failed execute phase
        try:
            lifecycle.run(
                42,
                Phase.EXECUTE,
                lambda claim: (_ for _ in ()).throw(RuntimeError("fail")),
            )
        except RuntimeError:
            pass

        result = runner.invoke(main, ["run", "42", "--retry"])
        assert result.exit_code == 0, result.output
        assert "PR #18 implemented" in result.output
        assert executed == [(42, "fresh")]


def test_retry_resume_wires_retained_workspace_mode(monkeypatch):
    from machinist.github import PullRequest
    from machinist.lifecycle import Phase, TaskLifecycle

    recoveries = []

    def fake_run_execute_phase(*args, recovery, **kwargs):
        recoveries.append(recovery)
        return PullRequest(
            number=18,
            title="Spec: Task (#42)",
            url="https://github.com/x/y/pull/18",
            branch="agent/issue-42",
            is_draft=False,
        )

    monkeypatch.setattr("machinist.cli.run_execute_phase", fake_run_execute_phase)
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        try:
            lifecycle.run(
                42,
                Phase.EXECUTE,
                lambda claim: (_ for _ in ()).throw(RuntimeError("fail")),
            )
        except RuntimeError:
            pass

        result = runner.invoke(
            main,
            ["retry", "42", "--phase", "execute", "--run", "--resume"],
        )

        assert result.exit_code == 0, result.output
        assert recoveries == ["resume"]


def test_retry_recovery_flags_are_safe_and_explicit():
    runner = CliRunner()

    both = runner.invoke(
        main,
        ["retry", "42", "--run", "--resume", "--fresh"],
    )
    without_run = runner.invoke(main, ["retry", "42", "--resume"])

    assert both.exit_code != 0
    assert "mutually exclusive" in both.output
    assert without_run.exit_code != 0
    assert "require --run" in without_run.output


def test_cancel_request_and_clear_are_durable():
    from machinist.cancellation import CancellationStore

    runner = CliRunner()
    with runner.isolated_filesystem():
        requested = runner.invoke(
            main,
            ["cancel", "42", "--reason", "runaway harness"],
        )

        assert requested.exit_code == 0, requested.output
        assert CancellationStore(Path(".machinist/runs")).requested(42)

        cleared = runner.invoke(main, ["cancel", "42", "--clear"])

        assert cleared.exit_code == 0, cleared.output
        assert not CancellationStore(Path(".machinist/runs")).requested(42)


def test_clean_command():
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        config_path = Path("machinist.yaml")
        config_path.write_text(
            config_path.read_text().replace(
                "root: ~/.machinist/workspaces",
                "root: .test-workspaces",
            )
        )
        ws_root = Path(".test-workspaces").resolve()
        ws_root.mkdir(parents=True, exist_ok=True)
        # Create mock task workspace directories
        ws1 = ws_root / f"{Path.cwd().name}-issue-42"
        ws2 = ws_root / f"{Path.cwd().name}-issue-43"
        ws1.mkdir(parents=True, exist_ok=True)
        ws2.mkdir(parents=True, exist_ok=True)

        result_list = runner.invoke(main, ["clean"])
        assert result_list.exit_code == 0
        assert "Found 2 workspace(s)" in result_list.output

        result_issue = runner.invoke(main, ["clean", "--issue", "42", "--force"])
        assert result_issue.exit_code == 0
        assert not ws1.exists()
        assert ws2.exists()

        result_all = runner.invoke(main, ["clean", "--all", "--force"])
        assert result_all.exit_code == 0
        assert not ws2.exists()


def test_clean_refuses_active_task_even_with_force(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.TaskLifecycle.claim_held",
        lambda self, issue: issue == 42,
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        config_path = Path("machinist.yaml")
        config_path.write_text(
            config_path.read_text().replace(
                "root: ~/.machinist/workspaces",
                "root: .test-workspaces",
            )
        )
        target = Path(".test-workspaces").resolve() / f"{Path.cwd().name}-issue-42"
        target.mkdir(parents=True)
        (target / "evidence.txt").write_text("keep\n")

        result = runner.invoke(main, ["clean", "--issue", "42", "--force"])

        assert result.exit_code != 0
        assert "actively claimed" in result.output
        assert (target / "evidence.txt").read_text() == "keep\n"


def test_inspect_command(monkeypatch):
    from machinist.github import Issue, PullRequest
    from machinist.lifecycle import Phase, TaskLifecycle

    class FakeGitHub:
        def __init__(self, repo=None):
            pass

        def get_issue(self, number):
            return Issue(
                number=42,
                title="CSV Export",
                body="export criteria",
                url="https://github.com/x/y/issues/42",
                labels=["agent-task"],
            )

        def open_machinist_prs(self, branch_prefix):
            return [
                PullRequest(
                    number=18,
                    title="Spec: CSV Export (#42)",
                    url="https://github.com/x/y/pull/18",
                    branch="agent/issue-42",
                    is_draft=True,
                    head_sha="0123456789abcdef0123456789abcdef01234567",
                )
            ]

        def pr_for_branch(self, branch):
            return self.open_machinist_prs("agent/")[0]

        def approval_sha(self, number):
            return "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr("machinist.cli.GitHubClient", FakeGitHub)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        lifecycle.run(42, Phase.SPEC, lambda claim: None)

        result = runner.invoke(main, ["inspect", "42"])
        assert result.exit_code == 0, result.output
        assert "Task Inspection: Issue #42" in result.output
        assert "CSV Export" in result.output
        assert "PR:    #18" in result.output
        assert "Phase [spec]: succeeded" in result.output


def test_inspect_json_offline_preserves_local_history_without_github(monkeypatch):
    from machinist.lifecycle import Phase, TaskLifecycle

    class ExplodingGitHub:
        def __init__(self, repo=None):
            raise AssertionError("offline inspect must not construct or call GitHub")

    monkeypatch.setattr("machinist.cli.GitHubClient", ExplodingGitHub)
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        TaskLifecycle(Path(".machinist/runs")).run(42, Phase.SPEC, lambda claim: None)

        result = runner.invoke(main, ["inspect", "42", "--offline", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["history"][0]["issue"] == 42
        assert "github_issue" not in payload["sources"]


def test_inspect_keeps_local_output_when_pr_lookup_fails(monkeypatch):
    from machinist.github import GitHubError, Issue
    from machinist.lifecycle import Phase, TaskLifecycle

    class PartialGitHub:
        def __init__(self, repo=None):
            pass

        def get_issue(self, number):
            return Issue(number, "Local-safe", "", "https://example/issues/42")

        def pr_for_branch(self, branch):
            raise GitHubError("network unavailable")

    monkeypatch.setattr("machinist.cli.GitHubClient", PartialGitHub)
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        TaskLifecycle(Path(".machinist/runs")).run(42, Phase.SPEC, lambda claim: None)

        result = runner.invoke(main, ["inspect", "42"])

        assert result.exit_code == 0, result.output
        assert "Local-safe" in result.output
        assert "network unavailable" in result.output
        assert "Phase [spec]: succeeded" in result.output


def test_runs_json_lists_orphan_safe_local_inventory():
    from machinist.lifecycle import Phase, TaskLifecycle

    runner = CliRunner()
    with runner.isolated_filesystem():
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        lifecycle.run(7, Phase.SPEC, lambda claim: None)

        result = runner.invoke(main, ["runs", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["current"][0]["issue"] == 7


def test_status_verbose(monkeypatch):
    from machinist.lifecycle import Phase, TaskLifecycle
    from machinist.phases.status import StatusRow

    rows = [
        StatusRow(
            kind="pr",
            number=18,
            title="Spec: Export (#42)",
            state="execute failed",
            url="https://github.com/x/y/pull/18",
            issue_number=42,
        ),
    ]
    monkeypatch.setattr(
        "machinist.cli.pipeline_status", lambda config, github, **kwargs: rows
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        try:
            lifecycle.run(
                42,
                Phase.EXECUTE,
                lambda claim: (_ for _ in ()).throw(RuntimeError("test gate failed")),
            )
        except RuntimeError:
            pass

        result = runner.invoke(main, ["status", "-v"])
        assert result.exit_code == 0, result.output
        assert "Error: test gate failed" in result.output
