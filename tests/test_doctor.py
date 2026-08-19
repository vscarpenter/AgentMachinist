"""Read-only installation diagnostics."""

import json
import subprocess
from types import SimpleNamespace

from machinist.config import MachinistConfig
from machinist.doctor import CheckLevel, run_doctor


def _runner_for(root, *, auth_returncode=0, ignored=True, calls=None):
    """Return deterministic Git/gh responses for read-only doctor probes."""
    calls = calls if calls is not None else []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, f"{root.resolve()}\n", "")
        if args == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                args, 0, "git@github.com:owner/project.git\n", ""
            )
        if args[:2] == ["git", "check-ignore"]:
            return subprocess.CompletedProcess(args, 0 if ignored else 1, "", "")
        if args == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(
                args, auth_returncode, "", "not logged in"
            )
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "nameWithOwner": "owner/project",
                        "defaultBranchRef": {"name": "main"},
                    }
                ),
                "",
            )
        if args[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps([{"name": "agent-task"}, {"name": "machinist:approved"}]),
                "",
            )
        raise AssertionError(f"unexpected doctor probe: {args}")

    return runner


def test_doctor_accumulates_pass_warn_and_fail_without_writing(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig()

    def which(name):
        return f"/usr/bin/{name}" if name in {"git", "gh"} else None

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=which,
        runner=_runner_for(tmp_path, auth_returncode=1),
    )

    levels = {check.name: check.level for check in report.checks}
    assert levels["repository"] is CheckLevel.PASS
    assert levels["GitHub authentication"] is CheckLevel.FAIL
    assert levels["harness"] is CheckLevel.FAIL
    assert levels["test gate"] is CheckLevel.WARN
    assert not (tmp_path / ".github").exists(), "doctor must remain read-only"


def test_doctor_reports_workflow_drift(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig()
    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    workflow = next(check for check in report.checks if check.name == "workflows")
    assert workflow.level is CheckLevel.FAIL
    assert "sync-workflows" in workflow.detail


def test_doctor_warns_about_failed_or_abandoned_task_runs(tmp_path):
    (tmp_path / ".git").mkdir()
    runs = tmp_path / ".machinist/runs"
    runs.mkdir(parents=True)
    (runs / "issue-7-spec.json").write_text(json.dumps({"status": "running"}))

    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )

    task_runs = next(check for check in report.checks if check.name == "Task Runs")
    assert task_runs.level is CheckLevel.WARN
    assert "retry" in task_runs.detail


def test_doctor_detects_origin_config_mismatch_and_missing_labels(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig.model_validate({"github": {"repo": "other/project"}})

    def runner(args, **kwargs):
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, str(tmp_path.resolve()), "")
        if args == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                args, 0, "https://github.com/owner/project.git", ""
            )
        if args[:2] == ["git", "check-ignore"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "nameWithOwner": "other/project",
                        "defaultBranchRef": {"name": "trunk"},
                    }
                ),
                "",
            )
        if args[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([]), "")
        raise AssertionError(args)

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=runner,
    )
    by_name = {check.name: check for check in report.checks}

    assert by_name["repository identity"].level is CheckLevel.FAIL
    assert "origin resolves to owner/project" in by_name["repository identity"].detail
    assert by_name["default branch"].detail == "trunk"
    assert by_name["labels"].level is CheckLevel.FAIL
    assert "agent-task" in by_name["labels"].detail


def test_doctor_bounds_subprocesses_and_reports_timeout(tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []
    regular = _runner_for(tmp_path, calls=calls)

    def runner(args, **kwargs):
        if args[:3] == ["gh", "repo", "view"]:
            calls.append((args, kwargs))
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return regular(args, **kwargs)

    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}" if name in {"git", "gh"} else None,
        runner=runner,
    )
    github = next(check for check in report.checks if check.name == "GitHub repository")

    assert github.level is CheckLevel.FAIL
    assert "timed out after 10 seconds" in github.detail
    assert calls
    assert all(kwargs["timeout"] == 10 for _, kwargs in calls)


def test_doctor_checks_workspace_safety_and_runtime_ignore_without_writing(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig.model_validate({"workspace": {"root": "/"}})
    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}" if name in {"git", "gh"} else None,
        runner=_runner_for(tmp_path, ignored=False),
    )
    by_name = {check.name: check for check in report.checks}

    assert by_name["workspace"].level is CheckLevel.FAIL
    assert "filesystem root" in by_name["workspace"].detail
    assert by_name["runtime state"].level is CheckLevel.FAIL
    assert not (tmp_path / ".machinist").exists()


def test_doctor_allows_custom_ci_harness_and_skips_unmanaged_drift(tmp_path):
    (tmp_path / ".git").mkdir()
    base = MachinistConfig.model_validate({"github": {"spec_source": "github-actions"}})
    codex = MachinistConfig.model_validate({"harness": {"name": "codex"}}).harness
    config = SimpleNamespace(
        harness=codex,
        github=SimpleNamespace(
            repo=base.github.repo,
            spec_source=base.github.spec_source,
            labels=base.github.labels,
            manage_workflows=False,
        ),
        workspace=base.workspace,
        tests=base.tests,
    )
    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )
    by_name = {check.name: check for check in report.checks}

    assert by_name["Spec source"].level is CheckLevel.PASS
    assert "externally managed" in by_name["Spec source"].detail
    assert by_name["workflows"].level is CheckLevel.PASS
    assert "skipped" in by_name["workflows"].detail


def test_doctor_rejects_non_claude_managed_github_actions(tmp_path):
    (tmp_path / ".git").mkdir()
    base = MachinistConfig()
    codex = MachinistConfig.model_validate({"harness": {"name": "codex"}}).harness
    config = SimpleNamespace(
        # Bypass schema validation to verify a legacy/external config is still
        # diagnosed rather than crashing doctor.
        harness=codex,
        github=SimpleNamespace(
            repo=base.github.repo,
            spec_source="github-actions",
            labels=base.github.labels,
            manage_workflows=True,
        ),
        workspace=base.workspace,
        tests=base.tests,
    )
    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )
    spec_source = next(check for check in report.checks if check.name == "Spec source")

    assert spec_source.level is CheckLevel.FAIL
    assert "installs only claude-code" in spec_source.detail
