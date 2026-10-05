"""Local onboarding commands preserve explicit decisions and workflow routing."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from machinist.cli import main
from machinist.config import MachinistConfig
from machinist.doctor import CheckLevel, DoctorCheck, DoctorReport
from machinist.local_config_cli import LOCAL_CONFIG_CHANGE_NOTICE
from machinist.rehearsal import RehearsalCheckpoint, RehearsalResult

SPEC_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40


def _forbidden(*args, **kwargs):
    raise AssertionError("this local command must not route to GitHub automation")


@pytest.fixture
def revision_workflow(monkeypatch):
    calls = []

    class Workflow:
        def revise(self, task_id, feedback):
            calls.append((task_id, feedback))
            return SimpleNamespace(id=task_id)

        def status(self, task_id):
            return {
                "id": task_id,
                "title": "Clarify the first Task",
                "state": "awaiting approval",
                "spec_sha": SPEC_SHA,
                "spec": "# Revised Spec\nKeep the exact feedback.\n",
                "next_action": (
                    f"machinist approve --task {task_id} --spec-sha {SPEC_SHA}"
                ),
            }

    monkeypatch.setattr("machinist.local_cli._existing_workflow", lambda: Workflow())
    monkeypatch.setattr("machinist.cli.load_config", _forbidden)
    return calls


def test_revise_delegates_exact_feedback_and_shows_new_approval(revision_workflow):
    feedback = "  Explain the failure clearly.\nKeep the existing test.  "
    result = CliRunner().invoke(main, ["revise", "T1", "--feedback", feedback])

    assert result.exit_code == 0, result.output
    assert revision_workflow == [("T1", feedback)]
    assert "Revised Spec" in result.output
    assert f"approve --task T1 --spec-sha {SPEC_SHA}" in result.output


@pytest.mark.parametrize("task_id", ["1", "T0", "T01", "t1", "T1/../../T2"])
def test_revise_rejects_invalid_task_ids_before_workflow(revision_workflow, task_id):
    result = CliRunner().invoke(
        main, ["revise", task_id, "--feedback", "Clarify acceptance"]
    )

    assert result.exit_code != 0
    assert "T1" in result.output
    assert revision_workflow == []


@pytest.mark.parametrize("arguments", [[], ["--feedback", ""], ["--feedback", "  \n"]])
def test_revise_requires_nonempty_feedback_before_workflow(
    revision_workflow, arguments
):
    result = CliRunner().invoke(main, ["revise", "T1", *arguments])

    assert result.exit_code != 0
    assert "feedback" in result.output.lower()
    assert revision_workflow == []


def test_inspect_local_task_resolves_repository_root_without_github(
    monkeypatch, tmp_path
):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True)
    nested = repository / "source" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    calls = []
    payload = {"task": {"id": "T1"}, "repository": str(repository)}

    def render(root, task_id, *, as_json):
        calls.append((root, task_id, as_json))
        return json.dumps(payload)

    monkeypatch.setattr("machinist.cli.render_local_inspection", render)
    monkeypatch.setattr("machinist.cli.load_config", _forbidden)
    monkeypatch.setattr("machinist.cli._bound_github_client", _forbidden)

    result = CliRunner().invoke(main, ["inspect", "T1", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [(repository, "T1", True)]
    assert json.loads(result.output) == payload


def test_inspect_legacy_issue_keeps_integer_report_routing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = []
    payload = {"task": 42, "history": []}

    def report(lifecycle, *, issue, remote_sources):
        calls.append(issue)
        return SimpleNamespace(to_dict=lambda: payload)

    monkeypatch.setattr("machinist.cli.load_config", lambda: MachinistConfig())
    monkeypatch.setattr("machinist.cli.build_run_report", report)
    monkeypatch.setattr(
        "machinist.local_inspection.render_local_inspection", _forbidden
    )
    monkeypatch.setattr("machinist.cli._bound_github_client", _forbidden)

    result = CliRunner().invoke(main, ["inspect", "42", "--offline", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [42]
    assert json.loads(result.output) == payload


@pytest.mark.parametrize("local_arguments", [[], ["--local"]])
def test_fresh_workshop_doctor_implies_local_readiness(
    monkeypatch, tmp_path, local_arguments
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "machinist.yaml").write_text("version: 1\n")
    calls = []

    def local_readiness(root, *, run_gates, fresh_workshop):
        calls.append((root, run_gates, fresh_workshop))
        return DoctorReport(())

    monkeypatch.setattr("machinist.cli.run_local_doctor", local_readiness)
    monkeypatch.setattr("machinist.cli.load_config", _forbidden)
    monkeypatch.setattr("machinist.cli.run_doctor", _forbidden)

    result = CliRunner().invoke(
        main, ["doctor", *local_arguments, "--fresh-workshop", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, True, True)]
    assert json.loads(result.output) == {"ok": True, "checks": []}


@pytest.fixture
def local_configuration(monkeypatch, tmp_path):
    path = tmp_path / ".machinist/runs/local/config.yaml"
    calls = []
    payload = {
        "configuration": {
            "workflow": "local",
            "source": "saved",
            "path": str(path),
            "changes_apply": LOCAL_CONFIG_CHANGE_NOTICE,
        },
        "tests": {"command": "python -m unittest"},
    }
    monkeypatch.chdir(tmp_path)
    (tmp_path / "machinist.yaml").write_text("version: 1\n")
    monkeypatch.setattr("machinist.cli.find_repository_root", lambda cwd: tmp_path)

    def show(root):
        calls.append(("show", root))
        return payload

    def update(key, value, root):
        calls.append(("set", key, value, root))
        return MachinistConfig()

    monkeypatch.setattr("machinist.cli.show_local_effective", show, raising=False)
    monkeypatch.setattr("machinist.cli.set_local_value", update, raising=False)
    monkeypatch.setattr(
        "machinist.cli.local_config_path", lambda root: path, raising=False
    )
    monkeypatch.setattr("machinist.cli.show_effective", _forbidden)
    monkeypatch.setattr("machinist.cli.set_config_value", _forbidden)
    return SimpleNamespace(root=tmp_path, path=path, payload=payload, calls=calls)


def test_config_show_local_selects_saved_workflow_and_explains_changes(
    local_configuration,
):
    result = CliRunner().invoke(main, ["config", "show", "--local", "--json"])

    assert result.exit_code == 0, result.output
    assert local_configuration.calls == [("show", local_configuration.root)]
    assert json.loads(result.output) == local_configuration.payload


def test_config_set_local_uses_validated_local_api_and_prints_change_notice(
    local_configuration,
):
    result = CliRunner().invoke(
        main, ["config", "set", "tests.command", "uv run pytest", "--local"]
    )

    assert result.exit_code == 0, result.output
    assert local_configuration.calls == [
        ("set", "tests.command", "uv run pytest", local_configuration.root)
    ]
    assert str(local_configuration.path) in result.output
    assert LOCAL_CONFIG_CHANGE_NOTICE in result.output


@pytest.mark.parametrize("arguments", [["show"], ["set", "tests.command", "pytest"]])
def test_local_config_selector_rejects_an_explicit_path(local_configuration, arguments):
    result = CliRunner().invoke(
        main, ["config", *arguments, "--local", "--path", "machinist.yaml"]
    )

    assert result.exit_code != 0
    assert "--local" in result.output and "--path" in result.output
    assert local_configuration.calls == []


def _checkpoint(tmp_path: Path, stage: str, *, harness_used: bool = False):
    return RehearsalCheckpoint(
        stage=stage,
        prompt=(
            "Approve this exact Spec to implement the disposable Task?"
            if stage == "spec"
            else "Accept this reviewed change into the disposable repository?"
        ),
        workspace=tmp_path,
        repository=tmp_path / "repository",
        task_id="T1",
        spec_sha=SPEC_SHA,
        spec="# Rehearsal Spec\nReturn two and keep the regression test.\n",
        spec_path=tmp_path / "inspection/spec.md",
        harness_used=harness_used,
        candidate_sha=CANDIDATE_SHA if stage == "acceptance" else None,
        diff="-return 1\n+return 2\n" if stage == "acceptance" else "",
        diff_path=tmp_path / "inspection/candidate.diff",
        candidate_path=tmp_path / "inspection/candidate",
        verification_report={"success": True, "gates": []},
        review_report={
            "completed": True,
            "reviewed_sha": CANDIDATE_SHA,
            "summary": "The candidate matches the exact Spec",
            "findings": [],
        },
        report_path=tmp_path / "inspection/report.json",
    )


def test_guided_rehearsal_renders_evidence_and_waits_at_both_decisions(
    monkeypatch, tmp_path
):
    decisions = []

    def run(*, guided, confirm, progress):
        assert guided is True
        assert callable(confirm)
        progress("spec ready")
        for stage in ("spec", "acceptance"):
            decisions.append((stage, confirm(_checkpoint(tmp_path, stage))))
        return RehearsalResult(
            ("spec ready", "local integration complete"),
            harness_used=False,
            task_id="T1",
            spec_sha=SPEC_SHA,
            candidate_sha=CANDIDATE_SHA,
            integrated_sha=CANDIDATE_SHA,
        )

    monkeypatch.setattr("machinist.cli.run_local_rehearsal", run, raising=False)
    monkeypatch.setattr("machinist.cli.simulate_rehearsal", _forbidden)
    monkeypatch.setattr("machinist.cli.load_config", _forbidden)

    result = CliRunner().invoke(main, ["rehearse", "--guided"], input="y\ny\n")

    assert result.exit_code == 0, result.output
    assert decisions == [("spec", True), ("acceptance", True)]
    assert SPEC_SHA in result.output
    assert "Return two and keep the regression test" in result.output
    assert CANDIDATE_SHA in result.output
    assert "+return 2" in result.output
    assert "The candidate matches the exact Spec" in result.output
    assert "Rehearsal passed" in result.output
    assert "no model or API usage" in result.output


def test_declining_guided_rehearsal_keeps_artifacts_without_claiming_success(
    monkeypatch, tmp_path
):
    decisions = []

    def run(*, guided, confirm, progress):
        decisions.append(confirm(_checkpoint(tmp_path, "spec")))
        return RehearsalResult(
            ("spec ready",),
            harness_used=False,
            workspace=tmp_path,
            task_id="T1",
            spec_sha=SPEC_SHA,
            stopped_at="spec",
        )

    monkeypatch.setattr("machinist.cli.run_local_rehearsal", run, raising=False)
    monkeypatch.setattr("machinist.cli.simulate_rehearsal", _forbidden)

    result = CliRunner().invoke(main, ["rehearse", "--guided"], input="n\n")

    assert result.exit_code == 0, result.output
    assert decisions == [False]
    assert "Rehearsal passed" not in result.output
    assert "paused at spec" in result.output.lower()
    assert str(tmp_path) in result.output
    assert "Next: machinist start" not in result.output


def test_guided_acceptance_shows_real_review_findings(monkeypatch, tmp_path):
    checkpoint = _checkpoint(tmp_path, "acceptance")
    checkpoint = replace(
        checkpoint,
        review_report={
            "summary": "Read this finding before accepting",
            "findings": [
                {
                    "severity": "high",
                    "confidence": "high",
                    "file": "feature.py",
                    "line": 2,
                    "requirement": "Preserve valid input",
                    "message": "The change rejects valid input.",
                    "remediation": "Keep the existing valid-input behavior.",
                }
            ],
        },
    )

    def run(*, guided, confirm, progress):
        assert not confirm(checkpoint)
        return RehearsalResult(
            (), harness_used=False, workspace=tmp_path, stopped_at="acceptance"
        )

    monkeypatch.setattr("machinist.cli.run_local_rehearsal", run)
    result = CliRunner().invoke(main, ["rehearse", "--guided"], input="n\n")
    assert result.exit_code == 0, result.output
    for text in (
        "high",
        "feature.py:2",
        "The change rejects valid input.",
        "Keep the existing valid-input behavior.",
    ):
        assert text in result.output
    assert "Review finding" not in result.output


def test_fresh_readiness_failure_recommends_same_mode(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "machinist.cli.run_local_doctor",
        lambda *a, **kw: DoctorReport(
            (
                DoctorCheck(CheckLevel.PASS, "fresh Workshop", "provisioned"),
                DoctorCheck(
                    CheckLevel.FAIL,
                    "verification execution",
                    "dependency missing in fresh clone",
                ),
            )
        ),
    )
    result = CliRunner().invoke(main, ["doctor", "--local", "--fresh-workshop"])
    assert result.exit_code == 1
    assert "machinist doctor --local --fresh-workshop" in result.output
    assert "machinist doctor --local --run-gates" not in result.output


def test_guided_harness_rehearsal_labels_provider_usage_before_running(
    monkeypatch, tmp_path
):
    config = MachinistConfig()
    calls = []

    def run(received, *, harness_factory, guided, confirm, progress):
        calls.append((received, guided, callable(harness_factory)))
        click.echo("Configured Harness invocation begins")
        assert confirm(_checkpoint(tmp_path, "spec", harness_used=True))
        return RehearsalResult(("local integration complete",), harness_used=True)

    monkeypatch.setattr("machinist.cli.has_local_configuration", lambda: True)
    monkeypatch.setattr("machinist.cli.find_repository_root", lambda cwd: tmp_path)
    monkeypatch.setattr("machinist.cli.load_local_config", lambda root: config)
    monkeypatch.setattr("machinist.cli.run_harness_rehearsal", run)
    monkeypatch.setattr("machinist.cli.load_config", _forbidden)

    result = CliRunner().invoke(
        main, ["rehearse", "--guided", "--harness"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    assert calls == [(config, True, True)]
    before_harness = result.output.split("Configured Harness invocation begins")[0]
    assert "quota" in before_harness.lower() or "usage" in before_harness.lower()
    assert "no model or API usage" not in result.output


def test_top_level_help_puts_the_local_journey_before_github_automation():
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Local Tasks" in result.output
    assert "GitHub automation" in result.output
    local = result.output.index("Local Tasks")
    github = result.output.index("GitHub automation", local)
    assert local < github
    local_help = result.output[local:github]
    for command in ("start", "revise", "approve", "continue", "integrate"):
        assert any(
            line.lstrip().startswith(command + " ") for line in local_help.splitlines()
        )
