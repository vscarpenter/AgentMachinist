"""Read-only installation diagnostics."""

import subprocess

from machinist.config import MachinistConfig
from machinist.doctor import CheckLevel, run_doctor


def test_doctor_accumulates_pass_warn_and_fail_without_writing(tmp_path):
    (tmp_path / ".git").mkdir()
    config = MachinistConfig()

    def which(name):
        return f"/usr/bin/{name}" if name in {"git", "gh"} else None

    def runner(args, **kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 1, "", "not logged in")
        return subprocess.CompletedProcess(args, 0, "", "")

    report = run_doctor(
        tmp_path,
        config,
        installed_version="0.2.0",
        which=which,
        runner=runner,
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
