"""Local lifecycle rehearsal never needs GitHub artifacts."""

import json
import subprocess

import pytest

from machinist.config import MachinistConfig
from machinist.rehearsal import (
    RehearsalError,
    run_harness_rehearsal,
    simulate_rehearsal,
)


class FakeHarness:
    name = "fake"
    config = MachinistConfig().harness

    def __init__(self, phase, calls, *, fail=False):
        self.phase = phase
        self.calls = calls
        self.fail = fail

    def generate_spec(self, prompt, cwd):
        self.calls.append(("spec", prompt, cwd))
        return "## Spec\nChange answer() in feature.py to return 2; update its regression test.\n"

    def implement(self, prompt, cwd):
        self.calls.append(("execute", prompt, cwd))
        if self.fail:
            raise RuntimeError("harness unavailable")
        (cwd / "feature.py").write_text("def answer():\n    return 2\n")
        (cwd / "tests/test_feature.py").write_text(
            "import unittest\nfrom feature import answer\n"
            "class TestFeature(unittest.TestCase):\n"
            "    def test_answer(self): self.assertEqual(answer(), 2)\n"
        )
        return "implemented"

    def review(self, prompt, cwd):
        self.calls.append(("review", prompt, cwd))
        return json.dumps({"version": 1, "summary": "looks good", "findings": []})


def test_simulation_runs_full_production_lifecycle_and_cleans_workspace(tmp_path):
    result = simulate_rehearsal(review_enabled=True, temp_parent=tmp_path)

    assert result.harness_used is False
    assert result.workspace is None
    assert result.transitions[-2:] == ("review complete", "local integration complete")
    assert result.task_id == "T1"
    assert result.spec_sha and result.candidate_sha != result.spec_sha
    assert result.integrated_sha == result.candidate_sha
    assert list(tmp_path.iterdir()) == []


def test_harness_rehearsal_uses_disposable_repo_and_all_enabled_phases(tmp_path):
    calls = []
    config = MachinistConfig.model_validate({"review": {"enabled": True}})

    result = run_harness_rehearsal(
        config,
        harness_factory=lambda phase: FakeHarness(phase, calls),
        temp_parent=tmp_path,
    )

    assert [call[0] for call in calls] == ["spec", "execute", "review"]
    assert result.harness_used is True
    assert result.workspace is None
    assert list(tmp_path.iterdir()) == []


def test_failed_harness_rehearsal_retains_disposable_repo_for_diagnosis(tmp_path):
    calls = []

    with pytest.raises(RehearsalError, match="harness unavailable") as raised:
        run_harness_rehearsal(
            MachinistConfig(),
            harness_factory=lambda phase: FakeHarness(
                phase, calls, fail=phase == "execute"
            ),
            temp_parent=tmp_path,
        )

    assert raised.value.workspace.is_dir()
    assert (raised.value.workspace / "repository/.git").is_dir()


def test_rehearsal_calls_production_engine_and_keeps_no_origin(monkeypatch, tmp_path):
    from machinist.local_workflow import LocalWorkflow

    calls = []
    original = LocalWorkflow.integrate

    def observe(self, task_id):
        for phase in ("spec", "execute", "review"):
            from machinist.lifecycle import Phase, RunStatus

            assert self.lifecycle.record(1, Phase(phase)).status is RunStatus.SUCCEEDED
        assert self.store.read_report(task_id)
        assert (
            subprocess.run(
                ["git", "remote"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            == ""
        )
        calls.append(task_id)
        return original(self, task_id)

    monkeypatch.setattr(LocalWorkflow, "integrate", observe)
    simulate_rehearsal(review_enabled=True, temp_parent=tmp_path)
    assert calls == ["T1"]


def test_wrong_implementation_fails_real_gate_and_retains_phase_evidence(tmp_path):
    class WrongHarness(FakeHarness):
        def implement(self, prompt, cwd):
            super().implement(prompt, cwd)
            (cwd / "feature.py").write_text("def answer():\n    return 99\n")
            return "Claims success"

    with pytest.raises(RehearsalError, match="verif|gate|test") as raised:
        run_harness_rehearsal(
            MachinistConfig.model_validate({"review": {"enabled": True}}),
            harness_factory=lambda phase: WrongHarness(phase, []),
            temp_parent=tmp_path,
        )
    root = raised.value.workspace
    evidence = list((root / "repository/.machinist/runs/local").rglob("*.json"))
    assert evidence
    assert any('"failed"' in path.read_text() for path in evidence)


def test_fake_rehearsal_propagates_production_gate_failure(monkeypatch, tmp_path):
    import machinist.rehearsal as rehearsal

    original = rehearsal._FakeHarness.implement

    def broken(self, prompt, cwd):
        original(self, prompt, cwd)
        (cwd / "feature.py").write_text("def answer():\n    return -1\n")
        return "implemented"

    monkeypatch.setattr(rehearsal._FakeHarness, "implement", broken)
    with pytest.raises(RehearsalError):
        simulate_rehearsal(review_enabled=True, temp_parent=tmp_path)


def test_rehearsal_does_not_execute_repository_specific_gate_or_notification(tmp_path):
    external = tmp_path / "must-not-run"
    config = MachinistConfig.model_validate(
        {
            "tests": {"command": f"touch {external}"},
            "notifications": {
                "backend": "command",
                "command": {"argv": ["touch", str(external)]},
            },
            "telemetry": {"otlp_endpoint": "https://must-not-call.invalid/telemetry"},
            "review": {"enabled": True},
        }
    )
    result = run_harness_rehearsal(
        config,
        harness_factory=lambda phase: FakeHarness(phase, []),
        temp_parent=tmp_path,
    )
    assert result.integrated_sha
    assert not external.exists()


def test_legacy_review_setting_cannot_disable_guided_local_review(tmp_path):
    result = simulate_rehearsal(review_enabled=False, temp_parent=tmp_path)
    assert "review complete" in result.transitions
    assert result.integrated_sha == result.candidate_sha


def test_fixture_git_ignores_ambient_repository_redirection(monkeypatch, tmp_path):
    from machinist.rehearsal import _initialize_repo

    repository = tmp_path / "fixture"
    repository.mkdir()
    foreign = tmp_path / "caller"
    foreign.mkdir()
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "ambient-branch")

    _initialize_repo(repository)

    assert (repository / ".git/HEAD").read_text().strip() == "ref: refs/heads/main"
    assert list(foreign.iterdir()) == []
