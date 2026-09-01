"""Read-only installation diagnostics."""

import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from machinist.config import MachinistConfig
from machinist.doctor import (
    DOCTOR_CHECK_NAMES,
    CheckLevel,
    canonical_check_name,
    fix_hint_for_check_name,
    run_doctor,
)
from machinist.lifecycle import Phase, TaskLifecycle
from machinist.task_intake import sync_task_template
from machinist.workflows import sync_workflows


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
        if args[:3] == ["gh", "auth", "status"]:
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
                        "viewerPermission": "ADMIN",
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
        if args[:2] == ["claude", "--version"]:
            return subprocess.CompletedProcess(args, 0, "2.1.251\n", "")
        if args[:3] == ["claude", "auth", "status"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"loggedIn": True}), ""
            )
        if args and args[0] == "claude" and "--help" in args:
            return subprocess.CompletedProcess(args, 0, "usage\n", "")
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


def test_doctor_binds_null_repo_to_origin_and_ignores_ambient_routing(
    tmp_path, monkeypatch
):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.setenv("GH_REPO", "attacker/other")
    monkeypatch.setenv("GH_HOST", "ghe.attacker.test")
    monkeypatch.setenv("GH_TOKEN", "dotcom-token")

    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path, calls=calls),
    )

    by_name = {check.name: check for check in report.checks}
    assert by_name["repository identity"].level is CheckLevel.PASS
    assert by_name["GitHub repository"].level is CheckLevel.PASS
    gh_calls = [(args, kwargs) for args, kwargs in calls if args[0] == "gh"]
    assert gh_calls
    assert any(
        args[:4] == ["gh", "repo", "view", "owner/project"]
        for args, _kwargs in gh_calls
    )
    assert ["--repo", "owner/project"] in [
        args[index : index + 2]
        for args, _kwargs in gh_calls
        for index in range(len(args) - 1)
    ]
    assert any(
        args[:3] == ["gh", "auth", "status"]
        and args[-2:] == ["--hostname", "github.com"]
        for args, _kwargs in gh_calls
    )
    for _args, kwargs in gh_calls:
        assert "GH_REPO" not in kwargs["env"]
        assert "GH_HOST" not in kwargs["env"]


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


def _write_run_projection(root, status):
    runs = root / ".machinist/runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "issue-7-spec.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issue": 7,
                "phase": "spec",
                "status": status,
                "attempt": 1,
                "started_at": "2026-08-19T00:00:00+00:00",
                "updated_at": "2026-08-19T00:00:01+00:00",
                "error": None,
                "evidence": {},
            }
        )
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("failed", CheckLevel.WARN),
        ("running", CheckLevel.WARN),
        ("cancelled", CheckLevel.WARN),
        ("abandoned", CheckLevel.WARN),
        ("retryable", CheckLevel.WARN),
        ("succeeded", CheckLevel.PASS),
    ],
)
def test_doctor_classifies_every_current_task_run_status(tmp_path, status, expected):
    (tmp_path / ".git").mkdir()
    _write_run_projection(tmp_path, status)

    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )

    task_runs = next(check for check in report.checks if check.name == "Task Runs")
    assert task_runs.level is expected
    assert status in task_runs.detail


def test_doctor_fails_closed_for_non_object_projection_and_corrupt_journal(tmp_path):
    (tmp_path / ".git").mkdir()
    runs = tmp_path / ".machinist/runs"
    history = runs / "history/issue-8-spec"
    history.mkdir(parents=True)
    (runs / "issue-7-spec.json").write_text("[]\n")
    (history / "attempt-000001.jsonl").write_text("{not-json}\n")

    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )
    task_runs = next(check for check in report.checks if check.name == "Task Runs")

    assert task_runs.level is CheckLevel.FAIL
    assert "issue-7-spec.json" in task_runs.detail
    assert "attempt-000001.jsonl" in task_runs.detail


def test_doctor_warns_for_journal_only_orphan_evidence(tmp_path):
    (tmp_path / ".git").mkdir()
    lifecycle = TaskLifecycle(tmp_path / ".machinist/runs")
    lifecycle.run(9, Phase.SPEC, lambda claim: None)
    (lifecycle.runs_dir / "issue-9-spec.json").unlink()

    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )
    task_runs = next(check for check in report.checks if check.name == "Task Runs")

    assert task_runs.level is CheckLevel.WARN
    assert "journal-only evidence" in task_runs.detail
    assert "#9 spec attempt 1" in task_runs.detail


def test_doctor_passes_succeeded_current_run_while_surfacing_history(tmp_path):
    (tmp_path / ".git").mkdir()
    lifecycle = TaskLifecycle(tmp_path / ".machinist/runs")
    with pytest.raises(RuntimeError):
        lifecycle.run(
            10, Phase.EXECUTE, lambda claim: (_ for _ in ()).throw(RuntimeError("boom"))
        )
    lifecycle.retry(10, Phase.EXECUTE)
    lifecycle.run(10, Phase.EXECUTE, lambda claim: None)

    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )
    task_runs = next(check for check in report.checks if check.name == "Task Runs")

    assert task_runs.level is CheckLevel.PASS
    assert "2 recorded attempt(s)" in task_runs.detail
    assert "1 non-current history attempt(s)" in task_runs.detail


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
        if args[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "nameWithOwner": "other/project",
                        "defaultBranchRef": {"name": "trunk"},
                        "viewerPermission": "READ",
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


def test_doctor_redacts_origin_credentials_but_keeps_host_and_path(tmp_path):
    (tmp_path / ".git").mkdir()
    regular = _runner_for(tmp_path)
    credentialed = "https://build-user:super-secret-token@github.com/owner/project.git"

    def runner(args, **kwargs):
        if args == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, credentialed, "")
        return regular(args, **kwargs)

    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}" if name in {"git", "gh"} else None,
        runner=runner,
    )
    by_name = {check.name: check for check in report.checks}
    serialized = "\n".join(check.detail for check in report.checks)

    assert by_name["origin"].detail == "https://github.com/owner/project.git"
    assert by_name["repository identity"].detail == "derived owner/project from origin"
    assert "super-secret-token" not in serialized
    assert "build-user" not in serialized


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


def test_doctor_accepts_provider_neutral_managed_github_actions(tmp_path):
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

    assert spec_source.level is CheckLevel.PASS
    assert "compatible with codex" in spec_source.detail


def _doctor_update_check(status, **overrides):
    from machinist.updates import UpdateCheck, UpdateStatus

    fields = {
        "status": UpdateStatus(status),
        "installed": "0.2.0",
        "latest": "0.7.1",
        "upgrade_command": "uv tool upgrade agentmachinist",
    }
    fields.update(overrides)
    return UpdateCheck(**fields)


def _doctor_with_update_probe(tmp_path, probe):
    (tmp_path / ".git").mkdir()
    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/usr/bin/{name}" if name in {"git", "gh"} else None,
        runner=_runner_for(tmp_path),
        update_probe=probe,
    )
    return next(check for check in report.checks if check.name == "updates")


def test_doctor_warns_when_a_newer_release_is_published(tmp_path):
    check = _doctor_with_update_probe(
        tmp_path, lambda installed: _doctor_update_check("available")
    )

    assert check.level is CheckLevel.WARN
    assert "0.7.1" in check.detail
    assert "uv tool upgrade agentmachinist" in check.detail


def test_doctor_passes_when_the_installation_is_current(tmp_path):
    check = _doctor_with_update_probe(
        tmp_path,
        lambda installed: _doctor_update_check("current", latest="0.2.0"),
    )

    assert check.level is CheckLevel.PASS
    assert "latest release" in check.detail


def test_doctor_warns_but_never_fails_when_the_index_is_unreachable(tmp_path):
    (tmp_path / ".git").mkdir()
    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/usr/bin/{name}" if name in {"git", "gh"} else None,
        runner=_runner_for(tmp_path),
        update_probe=lambda installed: _doctor_update_check(
            "unknown", latest=None, error="URLError: offline"
        ),
    )

    check = next(item for item in report.checks if item.name == "updates")
    assert check.level is CheckLevel.WARN
    assert "offline" in check.detail
    assert CheckLevel.FAIL not in {
        item.level for item in report.checks if item.name == "updates"
    }


def test_doctor_survives_a_raising_update_probe(tmp_path):
    def probe(installed):
        raise RuntimeError("probe exploded")

    check = _doctor_with_update_probe(tmp_path, probe)

    assert check.level is CheckLevel.WARN
    assert "probe exploded" in check.detail


def test_doctor_reports_a_disabled_update_check_as_a_pass(tmp_path):
    # The autouse fixture sets MACHINIST_NO_UPDATE_CHECK, so the default probe
    # short-circuits before any request.
    (tmp_path / ".git").mkdir()
    report = run_doctor(
        tmp_path,
        MachinistConfig(),
        installed_version="0.2.0",
        which=lambda name: f"/usr/bin/{name}" if name in {"git", "gh"} else None,
        runner=_runner_for(tmp_path),
    )

    check = next(item for item in report.checks if item.name == "updates")
    assert check.level is CheckLevel.PASS
    assert "disabled" in check.detail


def test_doctor_fails_when_configured_harness_invocation_no_longer_parses(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig.model_validate(
        {"harness": {"name": "codex"}, "github": {"manage_workflows": False}}
    )
    regular = _runner_for(tmp_path)

    def runner(args, **kwargs):
        if args[:2] == ["codex", "--version"]:
            return subprocess.CompletedProcess(args, 0, "codex-cli 0.151.0\n", "")
        if args[:3] == ["codex", "login", "status"]:
            return subprocess.CompletedProcess(args, 0, "Logged in using ChatGPT\n", "")
        if args and args[0] == "codex" and "--help" in args:
            return subprocess.CompletedProcess(args, 2, "", "unexpected flag")
        return regular(args, **kwargs)

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=runner,
    )
    by_name = {check.name: check for check in report.checks}

    assert by_name["Spec Harness compatibility"].level is CheckLevel.FAIL
    assert by_name["Execute Harness compatibility"].level is CheckLevel.FAIL
    assert not report.ok


def test_doctor_fails_when_managed_actions_spec_secret_is_missing(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig.model_validate(
        {"github": {"spec_source": "github-actions"}}
    )
    regular = _runner_for(tmp_path)

    def runner(args, **kwargs):
        if args[:3] == ["gh", "secret", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:3] == ["gh", "api", "repos/owner/project"]:
            return subprocess.CompletedProcess(args, 0, "User\n", "")
        return regular(args, **kwargs)

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=runner,
    )
    credential = next(
        check for check in report.checks if check.name == "Actions Spec credential"
    )

    assert credential.level is CheckLevel.FAIL
    assert "gh secret set ANTHROPIC_API_KEY" in credential.detail


def test_doctor_verifies_managed_workflows_on_remote_default_branch(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig()
    sync_workflows(tmp_path, config, installed_version="0.2.0", check=False)
    regular = _runner_for(tmp_path)

    def runner(args, **kwargs):
        if args[:2] == ["gh", "api"] and "/contents/.github/workflows/" in args[2]:
            name = args[2].split("/workflows/", 1)[1].split("?", 1)[0]
            content = (tmp_path / ".github" / "workflows" / name).read_bytes()
            return subprocess.CompletedProcess(
                args, 0, base64.b64encode(content).decode() + "\n", ""
            )
        return regular(args, **kwargs)

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=runner,
    )
    remote = next(check for check in report.checks if check.name == "remote workflows")

    assert remote.level is CheckLevel.PASS
    assert "deployed on main" in remote.detail
    assert report.to_dict()["checks"]


def test_doctor_can_opt_in_to_executing_configured_gates(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig.model_validate(
        {
            "github": {"manage_workflows": False},
            "tests": {"command": "pytest -q"},
        }
    )

    def passing_gate(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "tests passed", "")

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
        run_gates=True,
        gate_runner=passing_gate,
    )
    execution = next(
        check for check in report.checks if check.name == "verification execution"
    )

    assert execution.level is CheckLevel.PASS
    assert "all 1" in execution.detail


def test_doctor_fails_when_the_sealed_task_template_is_missing(tmp_path):
    """Docs promise doctor covers the issue form; drift must surface as a FAIL."""
    (tmp_path / ".git").mkdir()
    config = MachinistConfig.model_validate({"github": {"manage_workflows": False}})

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )

    template = next(check for check in report.checks if check.name == "task template")
    assert template.level is CheckLevel.FAIL
    assert "machinist task template --write" in template.detail


def test_doctor_passes_when_the_sealed_task_template_matches(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig.model_validate({"github": {"manage_workflows": False}})
    sync_task_template(tmp_path, check=False)

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=lambda name: f"/bin/{name}",
        runner=_runner_for(tmp_path),
    )

    template = next(check for check in report.checks if check.name == "task template")
    assert template.level is CheckLevel.PASS


def test_every_canonical_doctor_check_name_has_a_fix_hint():
    """The docs promise an exact fix for any FAIL; no name may fall through."""
    missing = [name for name in DOCTOR_CHECK_NAMES if not fix_hint_for_check_name(name)]
    assert missing == []


@pytest.mark.parametrize(
    ("rendered", "canonical"),
    [
        ("claude-code version", "<harness> version"),
        ("codex authentication", "<harness> authentication"),
        ("Spec Harness compatibility", "<phase> Harness compatibility"),
        ("Execute Harness compatibility", "<phase> Harness compatibility"),
        ("workflows", "workflows"),
    ],
)
def test_canonical_check_name_folds_variable_prefixes(rendered, canonical):
    assert canonical_check_name(rendered) == canonical


def test_doctor_emits_only_canonical_check_names(tmp_path):
    """Guards the hint table against silent drift when checks are added."""
    (tmp_path / ".git").mkdir()
    observed: set[str] = set()
    for overrides in (
        {},
        {"github": {"manage_workflows": False}, "tests": {"command": "pytest -q"}},
        {"github": {"spec_source": "github-actions"}},
    ):
        config = MachinistConfig.model_validate(overrides)
        report = run_doctor(
            tmp_path,
            config,
            installed_version="0.2.0",
            which=lambda name: f"/bin/{name}",
            runner=_runner_for(tmp_path),
            run_gates=True,
            gate_runner=lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
        )
        observed.update(canonical_check_name(check.name) for check in report.checks)

    assert observed, "expected doctor to emit checks"
    assert observed <= set(DOCTOR_CHECK_NAMES), sorted(
        observed - set(DOCTOR_CHECK_NAMES)
    )
