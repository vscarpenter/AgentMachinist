"""Tests for the machinist CLI."""

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from machinist.cli import _workflow_drift_notice, main
from machinist.config import load_config
from machinist.workflows import WorkflowDriftError

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
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/x/y.git",
                ],
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
        "update-check",
        "sync-workflows",
        "sync-labels",
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


def test_doctor_json_is_machine_readable_even_when_readiness_fails(monkeypatch):
    from machinist.doctor import CheckLevel, DoctorCheck, DoctorReport

    monkeypatch.setattr(
        "machinist.cli.run_doctor",
        lambda *args, **kwargs: DoctorReport(
            (
                DoctorCheck(
                    CheckLevel.FAIL,
                    "Actions Spec credential",
                    "ANTHROPIC_API_KEY is missing",
                ),
            )
        ),
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["checks"][0]["name"] == "Actions Spec credential"


def test_config_set_renders_atomic_write_failure_without_traceback(monkeypatch):
    runner = CliRunner()
    with runner.isolated_filesystem():
        initialized = runner.invoke(main, ["init", "--no-workflows"])
        assert initialized.exit_code == 0, initialized.output
        original = Path("machinist.yaml").read_text()

        def deny_replace(*args, **kwargs):
            raise PermissionError("injected replace denial")

        monkeypatch.setattr("machinist.config_cli.os.replace", deny_replace)
        result = runner.invoke(
            main,
            ["config", "set", "github.poll_interval_seconds", "120"],
        )

        assert result.exit_code != 0
        assert "Error:" in result.output
        assert "injected replace denial" in result.output
        assert "Traceback" not in result.output
        assert Path("machinist.yaml").read_text() == original


def test_config_validate_reports_machine_readable_failure():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("machinist.yaml").write_text("version: 99\n")

        result = runner.invoke(main, ["config", "validate", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "validation"


def test_config_validate_invalid_utf8_is_machine_readable():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("machinist.yaml").write_bytes(b"version: 1\n# \xff\n")

        result = runner.invoke(main, ["config", "validate", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "validation"
        assert "not valid UTF-8" in payload["error"]["message"]


def test_config_validate_duplicate_keys_is_machine_readable():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("machinist.yaml").write_text(
            "github:\n  manage_workflows: true\n  manage_workflows: false\n"
        )

        result = runner.invoke(main, ["config", "validate", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"]["kind"] == "yaml"
        assert "duplicate key" in payload["error"]["message"]


@pytest.mark.parametrize(
    "repository",
    ["/repo", "owner/", "../repo", "owner/..", "owner /repo", "owner/repo.git"],
)
def test_config_validate_rejects_repository_identity_runtime_would_refuse(
    repository,
):
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("machinist.yaml").write_text(
            f"github:\n  repo: {json.dumps(repository)}\n"
        )

        result = runner.invoke(main, ["config", "validate", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "owner/repo" in payload["error"]["message"]


def test_init_creates_config_dirs_and_workflows():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"])

        assert result.exit_code == 0, result.output
        assert Path("machinist.yaml").is_file()
        assert Path(".machinist/specs/.gitkeep").is_file()
        assert Path(".github/workflows/machinist-approve.yml").is_file()
        assert "Spec dispatch: local" in result.output
        assert "Execute dispatch: local watcher" in result.output
        assert "Start dispatch: machinist watch" in result.output


def test_init_no_workflows_prunes_prior_managed_workflows():
    runner = CliRunner()
    with runner.isolated_filesystem():
        first = runner.invoke(main, ["init"])
        assert first.exit_code == 0, first.output
        config_path = Path("machinist.yaml")
        config_path.write_text(
            config_path.read_text().replace(
                "spec_source: local", "spec_source: github-actions"
            )
        )
        projected = runner.invoke(main, ["sync-workflows"])
        assert projected.exit_code == 0, projected.output
        managed = [
            Path(".github/workflows/machinist-spec.yml"),
            Path(".github/workflows/machinist-approve.yml"),
        ]
        assert all(path.is_file() for path in managed)

        disabled = runner.invoke(main, ["init", "--force", "--no-workflows"])

        assert disabled.exit_code == 0, disabled.output
        assert load_config().github.manage_workflows is False
        assert all(not path.exists() for path in managed)
        assert "removed .github/workflows/machinist-approve.yml" in disabled.output


@pytest.mark.parametrize("no_workflows", [False, True])
def test_init_refuses_custom_workflow_collision_before_any_mutation(no_workflows):
    runner = CliRunner()
    with runner.isolated_filesystem():
        custom = Path(".github/workflows/machinist-approve.yml")
        custom.parent.mkdir(parents=True)
        custom.write_text("name: Custom owner workflow\n")
        args = ["init"]
        if no_workflows:
            args.append("--no-workflows")

        result = runner.invoke(main, args)

        assert result.exit_code != 0
        assert "refusing to replace or remove" in result.output
        assert custom.read_text() == "name: Custom owner workflow\n"
        assert not Path("machinist.yaml").exists()
        assert not Path(".machinist").exists()
        assert not Path(".gitignore").exists()


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


def test_sync_labels_checks_then_applies_missing_labels(monkeypatch):
    calls = []

    class FakeGitHub:
        def __init__(self, repo=None):
            self.repo = repo

        def bind_repository(self, identity, *, hostname):
            self.repo = identity

        def label_names(self):
            return {"agent-task"}

        def ensure_label(self, name, *, color, description):
            calls.append((name, color, description))

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        monkeypatch.setattr("machinist.cli.GitHubClient", FakeGitHub)

        checked = runner.invoke(main, ["sync-labels", "--check"])
        applied = runner.invoke(main, ["sync-labels", "--apply"])

    assert checked.exit_code == 1
    assert "machinist:approved" in checked.output
    assert "--apply" in checked.output
    assert applied.exit_code == 0, applied.output
    assert [call[0] for call in calls] == ["agent-task", "machinist:approved"]


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


def test_init_force_rejects_config_symlink_without_clobbering_target(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem():
        outside = tmp_path / "outside-machinist.yaml"
        outside.write_text("external: keep\n")
        Path("machinist.yaml").symlink_to(outside)

        result = runner.invoke(main, ["init", "--force", "--no-workflows"])

        assert result.exit_code != 0
        assert "symbolic link" in result.output
        assert outside.read_text() == "external: keep\n"
        assert not Path(".machinist").exists()


def test_init_rejects_gitignore_symlink_before_any_setup_write(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem():
        outside = tmp_path / "outside-gitignore"
        outside.write_text("keep-me\n")
        Path(".gitignore").symlink_to(outside)

        result = runner.invoke(main, ["init", "--no-workflows"])

        assert result.exit_code != 0
        assert "symbolic link" in result.output
        assert outside.read_text() == "keep-me\n"
        assert not Path("machinist.yaml").exists()


def test_init_rejects_oversized_gitignore_before_any_setup_write():
    runner = CliRunner()
    with runner.isolated_filesystem():
        with Path(".gitignore").open("wb") as stream:
            stream.truncate(1024 * 1024 + 1)

        result = runner.invoke(main, ["init", "--no-workflows"])

        assert result.exit_code != 0
        assert "exceeds" in result.output
        assert not Path("machinist.yaml").exists()
        assert not Path(".machinist").exists()


@pytest.mark.parametrize("symlink_parent", [True, False])
def test_init_rejects_symlinked_spec_parent_or_gitkeep(tmp_path, symlink_parent):
    runner = CliRunner()
    with runner.isolated_filesystem():
        outside = tmp_path / "outside-specs"
        outside.mkdir()
        if symlink_parent:
            Path(".machinist").symlink_to(outside, target_is_directory=True)
        else:
            Path(".machinist/specs").mkdir(parents=True)
            external_file = outside / "gitkeep"
            external_file.write_text("keep\n")
            Path(".machinist/specs/.gitkeep").symlink_to(external_file)

        result = runner.invoke(main, ["init", "--no-workflows"])

        assert result.exit_code != 0
        assert "link" in result.output.lower()
        assert not Path("machinist.yaml").exists()
        if not symlink_parent:
            assert (outside / "gitkeep").read_text() == "keep\n"


def test_init_rejects_symlinked_workflow_parent_before_any_setup_write(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem():
        outside = tmp_path / "outside-github"
        outside.mkdir()
        Path(".github").symlink_to(outside, target_is_directory=True)

        result = runner.invoke(main, ["init"])

        assert result.exit_code != 0
        assert "symlink" in result.output.lower()
        assert list(outside.iterdir()) == []
        assert not Path("machinist.yaml").exists()


def test_init_rejects_non_regular_config_target():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("machinist.yaml").mkdir()

        result = runner.invoke(main, ["init", "--force", "--no-workflows"])

        assert result.exit_code != 0
        assert "not a regular file" in result.output


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
        lifecycle,
    ):
        assert lifecycle.runs_dir == Path.cwd() / ".machinist/runs"
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


def test_two_fresh_watch_invocations_dedupe_the_same_stale_approval(monkeypatch):
    from machinist.notify import NotificationResult, NotificationStatus

    notified = []

    def fake_notify(config, event, title, message, **kwargs):
        notified.append((event.value, title, message, kwargs["dedupe_key"]))
        return NotificationResult(
            NotificationStatus.DELIVERED,
            "desktop",
            event.value,
            kwargs["dedupe_key"],
        )

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
        lifecycle,
    ):
        notify_stale(42, "PR #57 approval is stale")
        return []

    monkeypatch.setattr("machinist.cli.notify_event", fake_notify)
    monkeypatch.setattr("machinist.cli.watch_once", fake_watch_once)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        first = runner.invoke(main, ["watch", "--once"])
        second = runner.invoke(main, ["watch", "--once"])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert len(notified) == 1
        assert notified[0][:3] == (
            "approval_stale",
            "Machinist approval stale",
            "PR #57 approval is stale",
        )


def test_watch_once_with_empty_pipeline_says_so(monkeypatch):
    monkeypatch.setattr("machinist.cli.watch_once", lambda *args, **kwargs: [])

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--once"])

        assert result.exit_code == 0
        assert "nothing to do" in result.output.lower()


def test_watch_spec_dispatch_wires_durable_cancellation_check(monkeypatch):
    from machinist.github import DraftPR

    seen = []

    def fake_run_spec_phase(*args, cancel_check, **kwargs):
        seen.append(callable(cancel_check))
        return DraftPR(number=57, url="https://github.com/x/y/pull/57")

    def fake_watch_once(*args, run_spec, **kwargs):
        run_spec(42)
        return ["spec dispatched"]

    monkeypatch.setattr("machinist.cli.run_spec_phase", fake_run_spec_phase)
    monkeypatch.setattr("machinist.cli.watch_once", fake_watch_once)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--once"])

    assert result.exit_code == 0, result.output
    assert seen == [True]


def test_watch_rejects_interval_below_rate_safe_minimum():
    result = CliRunner().invoke(main, ["watch", "--once", "--interval", "9"])

    assert result.exit_code != 0
    assert "range" in result.output.lower()


def test_watch_dry_run_lists_tasks_without_dispatching(monkeypatch):
    from machinist.phases.status import StatusRow
    from machinist.phases.watch import WatchTask

    seen = {}

    def fake_plan_watch_tasks(config, github, *, lifecycle):
        seen["runs_dir"] = lifecycle.runs_dir
        return (
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
        )

    monkeypatch.setattr("machinist.cli.plan_watch_tasks", fake_plan_watch_tasks)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["watch", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "eligible: execute issue #42" in result.output
        assert seen["runs_dir"] == Path.cwd() / ".machinist/runs"
        assert not Path(".machinist/runs").exists()


def test_watch_dry_run_renders_corrupt_cancellation_without_traceback(monkeypatch):
    from machinist.phases.status import StatusRow
    from machinist.phases.watch import WatchTask

    task = WatchTask(
        "spec",
        42,
        StatusRow(
            kind="issue",
            number=42,
            title="Unsafe cancellation marker",
            state="awaiting spec",
            url="https://github.com/x/y/issues/42",
            issue_number=42,
        ),
    )
    monkeypatch.setattr(
        "machinist.cli.plan_watch_tasks",
        lambda config, github, *, lifecycle: (task,),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        initialized = runner.invoke(main, ["init", "--no-workflows"])
        assert initialized.exit_code == 0, initialized.output
        marker = Path(".machinist/runs/cancellations/issue-42.json")
        marker.parent.mkdir(parents=True)
        marker.write_text("{not valid json")

        result = runner.invoke(main, ["watch", "--dry-run"])

        assert result.exit_code != 0
        assert "Error:" in result.output
        assert "cancellation marker" in result.output
        assert "corrupt" in result.output
        assert "Traceback" not in result.output


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


def test_queue_write_error_is_rendered_without_traceback(monkeypatch):
    def deny_replace(*args, **kwargs):
        raise PermissionError("injected replace denial")

    monkeypatch.setattr("machinist.queue_control.os.replace", deny_replace)

    result = CliRunner().invoke(main, ["queue", "pause"])

    assert result.exit_code != 0
    assert "Error:" in result.output
    assert "injected replace denial" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["queue", "pause", "--reason", "safety check"],
        ["queue", "show"],
        ["cancel", "42"],
        ["runs"],
        ["watch", "--dry-run"],
    ],
)
def test_runtime_commands_render_symlink_rejections_without_tracebacks(
    tmp_path, monkeypatch, arguments
):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    monkeypatch.chdir(repo)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/x/y.git"],
        check=True,
        capture_output=True,
        text=True,
    )
    runner = CliRunner()
    initialized = runner.invoke(main, ["init", "--no-workflows"])
    assert initialized.exit_code == 0, initialized.output
    runs = repo / ".machinist" / "runs"
    runs.parent.mkdir(parents=True, exist_ok=True)
    runs.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(main, arguments)

    assert result.exit_code != 0
    assert "Error:" in result.output
    assert "symlink component" in result.output
    assert "Traceback" not in result.output
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["status", "--local"],
        ["inspect", "42", "--offline"],
    ],
)
def test_runtime_reports_render_nested_history_symlink_rejections(
    tmp_path, monkeypatch, arguments
):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside-history"
    repo.mkdir()
    outside.mkdir()
    monkeypatch.chdir(repo)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/x/y.git"],
        check=True,
        capture_output=True,
        text=True,
    )
    runner = CliRunner()
    initialized = runner.invoke(main, ["init", "--no-workflows"])
    assert initialized.exit_code == 0, initialized.output
    runs = repo / ".machinist" / "runs"
    runs.mkdir(parents=True)
    (runs / "history").symlink_to(outside, target_is_directory=True)

    result = runner.invoke(main, arguments)

    assert result.exit_code != 0
    assert "Error:" in result.output
    assert "symlink component" in result.output
    assert "Traceback" not in result.output
    assert list(outside.iterdir()) == []


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


@pytest.mark.parametrize("from_file", [False, True])
def test_amend_rejects_whitespace_feedback_before_claim(from_file):
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        arguments = ["amend", "42"]
        if from_file:
            path = Path("feedback.txt")
            path.write_text("  \n\t  ")
            arguments += ["--feedback-file", str(path)]
        else:
            arguments += ["--feedback", "   "]

        result = runner.invoke(main, arguments)

        assert result.exit_code == 2
        assert "non-whitespace" in result.output
        assert not Path(".machinist/runs").exists()


def test_amend_rejects_oversized_feedback_file_before_reading_or_claim():
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        path = Path("feedback.txt")
        with path.open("wb") as stream:
            stream.truncate(200_001)

        result = runner.invoke(main, ["amend", "42", "--feedback-file", str(path)])

        assert result.exit_code == 2
        assert "feedback file is too large" in result.output
        assert not Path(".machinist/runs").exists()


def test_amend_rejects_feedback_file_symlink_without_disclosure(tmp_path):
    outside = tmp_path / "outside-feedback.txt"
    outside.write_text("secret review context")
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        path = Path("feedback.txt")
        path.symlink_to(outside)

        result = runner.invoke(main, ["amend", "42", "--feedback-file", str(path)])

        assert result.exit_code != 0
        assert "could not safely read" in result.output
        assert "secret review context" not in result.output
        assert not Path(".machinist/runs").exists()


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


@pytest.mark.parametrize("invalid_utf8", [False, True])
def test_service_install_renders_config_errors_without_traceback(
    monkeypatch, invalid_utf8
):
    monkeypatch.setattr("machinist.cli.sys.platform", "darwin")
    runner = CliRunner()
    with runner.isolated_filesystem():
        if invalid_utf8:
            Path("machinist.yaml").write_bytes(b"version: 1\n# \xff\n")

        result = runner.invoke(main, ["service", "install"])

        assert result.exit_code != 0
        assert "machinist.yaml" in result.output
        assert "Traceback" not in result.output


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
    monkeypatch.setattr("machinist.cli._launchd_service", lambda **_: service)

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
            "status",
            "start",
            "status",
            "restart",
            "stop",
            "status",
            "uninstall",
        ]


def test_service_logs_bounds_a_large_single_line(monkeypatch):
    from machinist.service import (
        LOG_TAIL_OUTPUT_LIMIT_BYTES,
        LOG_TAIL_TRUNCATION_MARKER,
    )

    class FakeService:
        logs_dir = Path(".machinist/runs/service")
        log_paths = (
            logs_dir / "watch.stdout.log",
            logs_dir / "watch.stderr.log",
        )

    monkeypatch.setattr(
        "machinist.cli._launchd_service",
        lambda: FakeService(),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        FakeService.logs_dir.mkdir(parents=True)
        FakeService.log_paths[0].write_bytes(b"x" * (1024 * 1024) + b"tail")

        result = runner.invoke(main, ["service", "logs", "--lines", "1"])

        assert result.exit_code == 0, result.output
        assert LOG_TAIL_TRUNCATION_MARKER in result.output
        assert result.output.rstrip().endswith("(no log yet)")
        assert "tail" in result.output
        assert len(result.output.encode("utf-8")) < LOG_TAIL_OUTPUT_LIMIT_BYTES + 512


def test_service_start_reloads_an_installed_but_stopped_agent(monkeypatch):
    from machinist.service import ServiceStatus

    calls = []

    class StoppedService:
        label = "io.github.test.machinist"
        plist_path = Path("service.plist")

        def status(self):
            calls.append("status")
            return ServiceStatus(self.label, True, False, 113, error="not loaded")

        def bootstrap(self):
            calls.append("bootstrap")

        def start(self):
            calls.append("start")

    monkeypatch.setattr(
        "machinist.cli._launchd_service",
        lambda: StoppedService(),
    )

    result = CliRunner().invoke(main, ["service", "start"])

    assert result.exit_code == 0, result.output
    assert calls == ["status", "bootstrap", "start"]


def test_service_recovery_commands_need_neither_config_nor_current_path(monkeypatch):
    from machinist.service import ServiceStatus

    managed = []

    class RecoveryService:
        def __init__(self, root):
            self.root = root
            self.label = "io.github.test.recovery"
            self.plist_path = root / "service.plist"
            self.logs_dir = root / ".machinist" / "runs" / "service"
            self.log_paths = (
                self.logs_dir / "watch.stdout.log",
                self.logs_dir / "watch.stderr.log",
            )
            self.calls = []

        @classmethod
        def for_management(cls, root):
            service = cls(root)
            managed.append(service)
            return service

        def status(self):
            self.calls.append("status")
            return ServiceStatus(self.label, True, True, 0, "loaded")

        def start(self):
            self.calls.append("start")

        def restart(self):
            self.calls.append("restart")

        def stop(self):
            self.calls.append("stop")

        def uninstall(self):
            self.calls.append("uninstall")
            return True

    def config_must_not_be_loaded(*args, **kwargs):
        raise AssertionError("management commands must not load machinist.yaml")

    monkeypatch.setattr("machinist.cli.sys.platform", "darwin")
    monkeypatch.setattr("machinist.cli._repository_root", lambda cwd: cwd.resolve())
    monkeypatch.setattr("machinist.cli.LaunchdService", RecoveryService)
    monkeypatch.setattr("machinist.cli.load_config", config_must_not_be_loaded)
    monkeypatch.setenv("PATH", "")

    runner = _BaseCliRunner()
    for config_text in (None, "github: [\n"):
        with runner.isolated_filesystem():
            if config_text is not None:
                Path("machinist.yaml").write_text(config_text)

            status = runner.invoke(main, ["service", "status"])
            started = runner.invoke(main, ["service", "start"])
            restarted = runner.invoke(main, ["service", "restart"])
            stopped = runner.invoke(main, ["service", "stop"])
            logs = runner.invoke(main, ["service", "logs"])
            uninstalled = runner.invoke(main, ["service", "uninstall"])

            for result in (status, started, restarted, stopped, logs, uninstalled):
                assert result.exit_code == 0, result.output
            assert "loaded/scheduled" in status.output
            assert "running" not in status.output
            assert "(no log yet)" in logs.output
            assert "Uninstalled" in uninstalled.output

    assert len(managed) == 12


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
        issue_number,
        config,
        *,
        github,
        harness,
        workspace,
        claim,
        revise,
        attempt,
        cancel_check,
    ):
        seen["issue"] = issue_number
        seen["harness"] = harness.name
        seen["repo"] = github.repo
        seen["repo_host"] = github.repo_host
        seen["strategy"] = workspace.config.strategy.value
        seen["revise"] = revise
        seen["attempt"] = attempt
        seen["cancel_check"] = callable(cancel_check)
        claim.checkpoint(spec_sha="a" * 40)
        return DraftPR(number=57, url="https://github.com/vscarpenter/demo/pull/57")

    monkeypatch.setattr("machinist.cli.run_spec_phase", fake_run_spec_phase)
    monkeypatch.setenv("GH_REPO", "attacker/other")
    monkeypatch.setenv("GH_HOST", "ghe.attacker.test")

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        result = runner.invoke(main, ["spec", "42"])

        assert result.exit_code == 0, result.output
        assert seen == {
            "issue": 42,
            "harness": "claude-code",
            "repo": "x/y",
            "repo_host": "github.com",
            "strategy": "worktree",
            "revise": False,
            "attempt": None,
            "cancel_check": True,
        }
        assert "pull/57" in result.output
        assert f"/machinist-execute {'a' * 40}" in result.output


def test_spec_refuses_configured_repo_that_mismatches_controller_origin(monkeypatch):
    invoked = False

    def fake_run_spec_phase(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("phase must not run for mismatched repository custody")

    monkeypatch.setattr("machinist.cli.run_spec_phase", fake_run_spec_phase)
    runner = CliRunner()
    with runner.isolated_filesystem():
        initialized = runner.invoke(main, ["init", "--no-workflows"])
        assert initialized.exit_code == 0, initialized.output
        config_path = Path("machinist.yaml")
        config_path.write_text(
            config_path.read_text().replace("repo: null", "repo: attacker/other", 1)
        )

        result = runner.invoke(main, ["spec", "42"])

    assert result.exit_code != 0
    assert "origin does not match configured GitHub repository" in result.output
    assert invoked is False


def test_spec_revise_repeats_successful_attempt_on_existing_pr(monkeypatch):
    from machinist.github import DraftPR
    from machinist.lifecycle import Phase, TaskLifecycle

    seen = []

    def fake_run_spec_phase(
        issue_number,
        config,
        *,
        github,
        harness,
        workspace,
        claim,
        revise,
        attempt,
        cancel_check,
    ):
        seen.append(
            (issue_number, revise, claim.attempt, attempt, callable(cancel_check))
        )
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
        assert seen == [(42, True, 2, 2, True)]
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

    seen = []

    def fake_preview(*args, cancel_check, **kwargs):
        seen.append(callable(cancel_check))
        return "## Preview\n\nNo delivery.\n"

    monkeypatch.setattr("machinist.cli.preview_spec_phase", fake_preview)
    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])

        result = runner.invoke(main, ["spec", "42", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "## Preview" in result.output
        assert seen == [True]
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


def test_init_does_not_infer_pytest_from_a_python_manifest_alone():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text("[project]\nname='demo'\n")
        result = runner.invoke(main, ["init", "--no-workflows"])
        assert result.exit_code == 0
        config = load_config("machinist.yaml")
        assert config.tests.command is None
        assert "Verification: NOT CONFIGURED" in result.output


def test_noninteractive_init_reports_detected_test_runner_as_unconfirmed():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text(
            "[project]\nname='demo'\ndependencies=['pytest>=8']\n"
        )
        Path("uv.lock").write_text("version = 1\n")

        result = runner.invoke(main, ["init", "--no-workflows"])

        assert result.exit_code == 0, result.output
        assert "suggested test runner (not enabled): 'uv run pytest'" in result.output
        assert load_config("machinist.yaml").tests.command is None
        assert "--test-cmd" in result.output


def _force_interactive(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli._stdin_is_interactive", lambda: True, raising=False
    )


def test_init_interactive_wizard_applies_answers(monkeypatch):
    _force_interactive(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            ["init"],
            input="local\nn\ncodex\ngo\n\nnone\n",
        )

        assert result.exit_code == 0, result.output
        config = load_config("machinist.yaml")
        assert config.github.spec_source.value == "local"
        assert config.github.manage_workflows is False
        assert config.harness.name.value == "codex"
        assert config.tests.command == "go test ./..."
        assert config.notifications.backend.value == "disabled"
        assert not Path(".github").exists()


def test_init_interactive_github_actions_locks_harness_to_claude_code(monkeypatch):
    _force_interactive(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            ["init"],
            input="github-actions\ny\nskip\n\n",
        )

        assert result.exit_code == 0, result.output
        assert "supports only" in result.output
        config = load_config("machinist.yaml")
        assert config.github.spec_source.value == "github-actions"
        assert config.harness.name.value == "claude-code"
        assert config.tests.command is None
        assert Path(".github/workflows/machinist-spec.yml").is_file()
        assert "gh secret set ANTHROPIC_API_KEY" in result.output
        assert "Execute dispatch: local watcher" in result.output


def test_init_interactive_confirms_detected_test_command(monkeypatch):
    _force_interactive(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text(
            "[project]\nname='demo'\ndependencies=['pytest>=8']\n"
        )
        Path("uv.lock").write_text("version = 1\n")
        result = runner.invoke(
            main,
            ["init"],
            input="\nn\n\ny\nall\n",
        )

        assert result.exit_code == 0, result.output
        config = load_config("machinist.yaml")
        assert config.tests.command == "uv run pytest"
        assert [event.value for event in config.notifications.events] == [
            "failure",
            "spec_ready",
            "approval_stale",
            "pr_ready",
        ]


def test_init_interactive_rejects_detected_then_suggests_by_language(monkeypatch):
    _force_interactive(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("pyproject.toml").write_text(
            "[project]\nname='demo'\ndependencies=['pytest>=8']\n"
        )
        Path("uv.lock").write_text("version = 1\n")
        result = runner.invoke(
            main,
            ["init"],
            input="\nn\n\nn\npython\n\n\n",
        )

        assert result.exit_code == 0, result.output
        config = load_config("machinist.yaml")
        assert config.tests.command == "pytest"


def test_init_interactive_flags_pre_answer_and_skip_questions(monkeypatch):
    _force_interactive(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            [
                "init",
                "--no-workflows",
                "--spec-source",
                "local",
                "--harness",
                "opencode",
                "--test-cmd",
                "make test",
                "--notifications",
                "desktop",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Spec dispatch [local]" not in result.output
        assert "Notify on" not in result.output
        config = load_config("machinist.yaml")
        assert config.harness.name.value == "opencode"
        assert config.tests.command == "make test"
        assert config.notifications.backend.value == "desktop"


def test_init_no_input_skips_wizard(monkeypatch):
    _force_interactive(monkeypatch)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init", "--no-workflows", "--no-input"])

        assert result.exit_code == 0, result.output
        assert "Configuring machinist.yaml" not in result.output
        config = load_config("machinist.yaml")
        assert config.github.spec_source.value == "local"
        assert config.notifications.backend.value == "desktop"


def test_init_spec_source_flag_writes_ci_dispatch_workflow():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init", "--spec-source", "github-actions"])

        assert result.exit_code == 0, result.output
        config = load_config("machinist.yaml")
        assert config.github.spec_source.value == "github-actions"
        assert Path(".github/workflows/machinist-spec.yml").is_file()


def test_init_notifications_flag_disables_backend():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main, ["init", "--no-workflows", "--notifications", "disabled"]
        )

        assert result.exit_code == 0, result.output
        config = load_config("machinist.yaml")
        assert config.notifications.backend.value == "disabled"


def test_init_conflicting_flags_fail_before_writing():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main, ["init", "--spec-source", "github-actions", "--harness", "codex"]
        )

        assert result.exit_code != 0
        assert "claude-code" in result.output
        assert not Path("machinist.yaml").exists()


def test_init_test_cmd_replaces_template_comment_tail():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main, ["init", "--no-workflows", "--test-cmd", "pytest -q"]
        )

        assert result.exit_code == 0, result.output
        text = Path("machinist.yaml").read_text()
        line = next(entry for entry in text.splitlines() if "pytest -q" in entry)
        assert line == '  command: "pytest -q"'


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
        assert "Requested approval for PR #18" in result.output
        assert "workflow will verify the current head" in result.output
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
        assert "Retrying issue #42 execute after attempt 1" in result.output
        assert "is retryable" not in result.output
        assert "PR #18 implemented" in result.output
        assert executed == [(42, "fresh")]


def test_retry_run_does_not_claim_retryable_after_dispatch_failure(monkeypatch):
    from machinist.lifecycle import Phase, RunStatus, TaskLifecycle
    from machinist.phases.execute import ExecutePhaseError

    def fail_execute(*args, **kwargs):
        raise ExecutePhaseError("dispatch failed")

    monkeypatch.setattr("machinist.cli.run_execute_phase", fail_execute)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        with pytest.raises(RuntimeError):
            lifecycle.run(
                42,
                Phase.EXECUTE,
                lambda claim: (_ for _ in ()).throw(RuntimeError("first failure")),
            )

        result = runner.invoke(main, ["retry", "42", "--phase", "execute", "--run"])

        assert result.exit_code != 0
        assert "Retrying issue #42 execute after attempt 1" in result.output
        assert "is retryable" not in result.output
        assert lifecycle.record(42, Phase.EXECUTE).status is RunStatus.FAILED


def test_retry_spec_run_wires_cancellation_and_prior_delivery_evidence(monkeypatch):
    from machinist.github import DraftPR
    from machinist.lifecycle import Phase, TaskLifecycle

    seen = []

    def fake_run_spec_phase(*args, claim, cancel_check, **kwargs):
        seen.append((callable(cancel_check), dict(claim.previous_evidence)))
        return DraftPR(number=57, url="https://github.com/x/y/pull/57")

    monkeypatch.setattr("machinist.cli.run_spec_phase", fake_run_spec_phase)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init", "--no-workflows"])
        lifecycle = TaskLifecycle(Path(".machinist/runs"))

        def fail_after_delivery(claim):
            claim.checkpoint(
                spec_sha="a" * 40,
                push_intended_sha="a" * 40,
                push_observed_sha="a" * 40,
                pr_number=57,
            )
            raise RuntimeError("crashed after PR create")

        with pytest.raises(RuntimeError):
            lifecycle.run(42, Phase.SPEC, fail_after_delivery)

        result = runner.invoke(main, ["retry", "42", "--phase", "spec", "--run"])

    assert result.exit_code == 0, result.output
    assert seen[0][0] is True
    assert seen[0][1]["pr_number"] == 57
    assert "Draft PR #57" in result.output


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
    from machinist.workspace import Workspace

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
        origin = Path(".test-origin.git").resolve()
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(origin)],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], check=True)
        Path("README.md").write_text("test repository\n")
        subprocess.run(["git", "add", "README.md"], check=True)
        subprocess.run(["git", "commit", "-m", "initial"], check=True)
        subprocess.run(["git", "remote", "set-url", "origin", str(origin)], check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], check=True)
        config = load_config()
        workspace = Workspace(Path.cwd(), config.workspace)
        ws1 = workspace.provision("issue-42", "agent/issue-42", "origin/main")
        ws2 = workspace.provision("issue-43", "agent/issue-43", "origin/main")
        victim = config.workspace.resolved_root() / f"{Path.cwd().name}-personal"
        victim.mkdir()
        (victim / "keep.txt").write_text("unrelated\n")

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
        assert (victim / "keep.txt").read_text() == "unrelated\n"


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


def test_runs_rejects_non_positive_issue_without_traceback():
    result = CliRunner().invoke(main, ["runs", "--issue", "0"])

    assert result.exit_code == 2
    assert "Invalid value for '--issue'" in result.output
    assert "Traceback" not in result.output


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


def _update_result(status, **overrides):
    from machinist.updates import UpdateCheck, UpdateStatus

    fields = {
        "status": UpdateStatus(status),
        "installed": "0.6.0",
        "latest": "0.7.1",
        "upgrade_command": "uv tool upgrade agentmachinist",
    }
    fields.update(overrides)
    return UpdateCheck(**fields)


def test_update_check_reports_an_available_release_with_instructions(monkeypatch):
    seen = {}

    def fake_check(installed, **kwargs):
        seen["installed"] = installed
        seen["timeout"] = kwargs.get("timeout_seconds")
        return _update_result("available")

    monkeypatch.setattr("machinist.cli.check_for_update", fake_check)

    result = CliRunner().invoke(main, ["update-check", "--timeout", "9"])

    assert result.exit_code == 0, result.output
    assert seen["timeout"] == 9
    assert "0.7.1" in result.output
    assert "uv tool upgrade agentmachinist" in result.output
    assert "CHANGELOG.md" in result.output


def test_update_check_is_quiet_when_the_installation_is_current(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.check_for_update",
        lambda installed, **kwargs: _update_result("current", latest="0.6.0"),
    )

    result = CliRunner().invoke(main, ["update-check"])

    assert result.exit_code == 0, result.output
    assert "latest release on PyPI" in result.output
    assert "upgrade" not in result.output.lower()


def test_update_check_json_is_scriptable(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.check_for_update",
        lambda installed, **kwargs: _update_result("available"),
    )

    result = CliRunner().invoke(main, ["update-check", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "package": "agentmachinist",
        "installed": "0.6.0",
        "latest": "0.7.1",
        "status": "available",
        "update_available": True,
        "upgrade_command": "uv tool upgrade agentmachinist",
        "error": None,
    }


def test_update_check_exits_nonzero_when_the_index_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.check_for_update",
        lambda installed, **kwargs: _update_result(
            "unknown", latest=None, error="URLError: offline"
        ),
    )

    result = CliRunner().invoke(main, ["update-check"])

    assert result.exit_code == 1
    assert "offline" in result.output
    assert "uv tool upgrade agentmachinist" in result.output
    assert "Traceback" not in result.output


def test_update_check_honors_the_opt_out_environment_variable():
    # conftest sets MACHINIST_NO_UPDATE_CHECK, so this exercises the real
    # check end to end without contacting any index.
    result = CliRunner().invoke(main, ["update-check"])

    assert result.exit_code == 0, result.output
    assert "MACHINIST_NO_UPDATE_CHECK" in result.output


def test_update_check_needs_no_repository_or_config():
    runner = _BaseCliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["update-check"])

    assert result.exit_code == 0, result.output


# --- Issue #22: a managed-workflow fix only lands after sync-workflows ---


def _raise_drift(*args, **kwargs):
    raise WorkflowDriftError("machinist-approve.yml differs from the projection")


def test_update_check_reports_workflow_drift_alongside_the_release(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.check_for_update",
        lambda installed, **kwargs: _update_result("current", latest="0.6.0"),
    )
    monkeypatch.setattr("machinist.cli.project_workflows", _raise_drift)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init"])
        result = runner.invoke(main, ["update-check"])

    assert result.exit_code == 0, result.output
    assert "sync-workflows" in result.output


def test_update_check_json_stays_scriptable_when_workflows_drift(monkeypatch):
    """The advisory is operator prose; it must never contaminate --json."""
    monkeypatch.setattr(
        "machinist.cli.check_for_update",
        lambda installed, **kwargs: _update_result("available"),
    )
    monkeypatch.setattr("machinist.cli.project_workflows", _raise_drift)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init"])
        result = runner.invoke(main, ["update-check", "--json"])

    assert result.exit_code == 0, result.output
    json.loads(result.output)


def test_update_check_is_silent_when_workflows_match(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.check_for_update",
        lambda installed, **kwargs: _update_result("current", latest="0.6.0"),
    )
    monkeypatch.setattr("machinist.cli.project_workflows", lambda *a, **k: None)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init"])
        result = runner.invoke(main, ["update-check"])

    assert "sync-workflows" not in result.output


def test_workflow_drift_notice_is_advisory_and_never_raises(monkeypatch):
    """A broken drift probe must not take down the command that called it."""

    def explode(*args, **kwargs):
        raise RuntimeError("projection blew up")

    monkeypatch.setattr("machinist.cli.project_workflows", explode)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init"])
        assert _workflow_drift_notice() is None


def test_watch_warns_when_managed_workflows_drift(monkeypatch):
    monkeypatch.setattr("machinist.cli.watch_once", lambda *args, **kwargs: [])
    monkeypatch.setattr("machinist.cli.project_workflows", _raise_drift)

    runner = CliRunner()
    with runner.isolated_filesystem():
        runner.invoke(main, ["init"])
        result = runner.invoke(main, ["watch", "--once"])

    assert "sync-workflows" in result.output
