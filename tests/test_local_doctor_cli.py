"""The optional local doctor preserves the existing command's public contract."""

import json

from click.testing import CliRunner

from machinist.cli import main
from machinist.doctor import CheckLevel, DoctorCheck, DoctorReport


def test_local_doctor_uses_local_readiness_without_loading_github_config(
    monkeypatch, tmp_path
):
    calls = []

    def local_readiness(root, *, run_gates):
        calls.append((root, run_gates))
        return DoctorReport((DoctorCheck(CheckLevel.PASS, "working tree", "clean"),))

    def forbidden(*args, **kwargs):
        raise AssertionError("local doctor must not load or diagnose GitHub setup")

    monkeypatch.setattr(
        "machinist.cli.run_local_doctor", local_readiness, raising=False
    )
    monkeypatch.setattr("machinist.cli.load_config", forbidden)
    monkeypatch.setattr("machinist.cli.run_doctor", forbidden)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["doctor", "--local", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "ok": True,
        "checks": [{"level": "PASS", "name": "working tree", "detail": "clean"}],
    }
    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    assert calls[0][1] is False


def test_local_doctor_runs_verification_only_when_explicit(monkeypatch):
    requested = []

    def local_readiness(root, *, run_gates):
        requested.append(run_gates)
        return DoctorReport(())

    monkeypatch.setattr(
        "machinist.cli.run_local_doctor", local_readiness, raising=False
    )
    result = CliRunner().invoke(main, ["doctor", "--local", "--run-gates"])

    assert result.exit_code == 0, result.output
    assert requested == [True]


def test_local_doctor_failure_is_actionable_without_github_setup_hints(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.run_local_doctor",
        lambda *args, **kwargs: DoctorReport(
            (
                DoctorCheck(
                    CheckLevel.FAIL, "local configuration", "missing required gate"
                ),
            )
        ),
        raising=False,
    )
    result = CliRunner().invoke(main, ["doctor", "--local"])

    assert result.exit_code == 1, result.output
    assert "missing required gate" in result.output
    assert "fix (local configuration)" in result.output
    assert "onboard" not in result.output
    assert "gh auth" not in result.output


def test_local_doctor_failure_json_has_no_human_remediation_suffix(monkeypatch):
    monkeypatch.setattr(
        "machinist.cli.run_local_doctor",
        lambda *args, **kwargs: DoctorReport(
            (DoctorCheck(CheckLevel.FAIL, "working tree", "uncommitted changes"),)
        ),
        raising=False,
    )
    result = CliRunner().invoke(main, ["doctor", "--local", "--json"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["ok"] is False
    assert "→ fix" not in result.output
