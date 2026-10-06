"""Guided rehearsal waits for inspection and acceptance of real local Evidence."""

import json
from pathlib import Path

import pytest

from machinist import rehearsal
from machinist.config import MachinistConfig
from machinist.lifecycle import Phase, RunStatus, TaskLifecycle
from machinist.local_tasks import LocalTaskStore


def _local_state(checkpoint):
    store = LocalTaskStore(checkpoint.repository)
    lifecycle = TaskLifecycle(
        checkpoint.repository / ".machinist/runs/local",
        repo_root=checkpoint.repository,
    )
    return store.get(checkpoint.task_id), lifecycle


def test_guided_rehearsal_pauses_at_exact_spec_and_verified_candidate(tmp_path):
    checkpoints = []

    def inspect(checkpoint):
        checkpoints.append(checkpoint)
        task, lifecycle = _local_state(checkpoint)
        assert checkpoint.workspace.is_dir()
        assert checkpoint.spec_path.read_text() == checkpoint.spec
        assert checkpoint.spec == rehearsal._git(
            checkpoint.repository,
            "show",
            f"{checkpoint.spec_sha}:.machinist/specs/task-1-spec.md",
        )
        assert rehearsal._git(checkpoint.repository, "remote").strip() == ""
        assert task.integration is None
        assert (
            rehearsal._git(checkpoint.repository, "rev-parse", "HEAD").strip()
            == task.base_sha
        )
        if checkpoint.stage == "spec":
            assert task.approval is None
            assert lifecycle.record(1, Phase.SPEC).status is RunStatus.SUCCEEDED
            assert lifecycle.record(1, Phase.EXECUTE) is None
            assert lifecycle.record(1, Phase.REVIEW) is None
            assert checkpoint.candidate_sha is None
            assert "exact Spec" in checkpoint.prompt
        else:
            assert checkpoint.stage == "acceptance"
            assert task.approval["spec_sha"] == checkpoint.spec_sha
            assert checkpoint.candidate_sha == task.candidate_sha
            assert checkpoint.candidate_sha != checkpoint.spec_sha
            assert checkpoint.diff_path.read_text() == checkpoint.diff
            assert "-    return 1" in checkpoint.diff
            assert "+    return 2" in checkpoint.diff
            assert "tests/test_feature.py" in checkpoint.diff
            assert checkpoint.verification_report["success"] is True
            assert checkpoint.review_report["reviewed_sha"] == checkpoint.candidate_sha
            assert checkpoint.review_report["completed"] is True
            assert checkpoint.report_path.is_file()
            assert (
                checkpoint.candidate_path.joinpath("feature.py").read_text()
                == "def answer():\n    return 2\n"
            )
            assert "disposable" in checkpoint.prompt
        assert checkpoint.harness_used is False
        return True

    result = rehearsal.run_local_rehearsal(
        guided=True, confirm=inspect, temp_parent=tmp_path
    )

    assert [checkpoint.stage for checkpoint in checkpoints] == ["spec", "acceptance"]
    assert result.stopped_at is None
    assert result.workspace is None
    assert result.integrated_sha == result.candidate_sha
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("declined_stage", ["spec", "acceptance"])
def test_declining_retains_inspectable_artifacts_without_integration(
    tmp_path, declined_stage
):
    seen = []

    def decide(checkpoint):
        seen.append(checkpoint)
        return checkpoint.stage != declined_stage

    result = rehearsal.run_local_rehearsal(
        guided=True, confirm=decide, temp_parent=tmp_path
    )

    assert result.workspace.is_dir()
    assert result.stopped_at == declined_stage
    assert result.integrated_sha is None
    assert "local integration complete" not in result.transitions
    checkpoint = seen[-1]
    task, lifecycle = _local_state(checkpoint)
    assert task.integration is None
    assert checkpoint.spec_path.read_text() == checkpoint.spec
    assert (
        rehearsal._git(checkpoint.repository, "rev-parse", "HEAD").strip()
        == task.base_sha
    )
    if declined_stage == "spec":
        assert result.candidate_sha is None
        assert task.approval is None
        assert lifecycle.record(1, Phase.EXECUTE) is None
        assert lifecycle.record(1, Phase.REVIEW) is None
    else:
        assert result.candidate_sha == task.candidate_sha
        assert checkpoint.diff_path.is_file()
        assert checkpoint.candidate_path.is_dir()
        assert lifecycle.record(1, Phase.REVIEW).status is RunStatus.SUCCEEDED


def test_guided_rehearsal_requires_a_decision_callback_before_setup(tmp_path):
    with pytest.raises(ValueError, match="confirmation callback"):
        rehearsal.run_local_rehearsal(guided=True, temp_parent=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_approval_rechecks_exact_spec_after_human_inspection(tmp_path):
    def change_spec_branch(checkpoint):
        task, _ = _local_state(checkpoint)
        rehearsal._git(
            checkpoint.repository,
            "update-ref",
            f"refs/heads/{task.branch}",
            task.base_sha,
        )
        return True

    with pytest.raises(rehearsal.RehearsalError, match="stale Approval") as raised:
        rehearsal.run_local_rehearsal(
            guided=True, confirm=change_spec_branch, temp_parent=tmp_path
        )

    repository = raised.value.workspace / "repository"
    task = LocalTaskStore(repository).get("T1")
    assert task.approval is None
    assert task.candidate_sha is None


def test_free_rehearsal_needs_no_source_examples_provider_or_forge(
    monkeypatch, tmp_path
):
    def external_call(*args, **kwargs):
        raise AssertionError("free rehearsal must not invoke an external integration")

    for target in (
        "machinist.harness.get_harness",
        "machinist.github.GitHubClient",
        "machinist.gitlab.GitLabClient",
        "machinist.notify.notify_event",
        "machinist.telemetry.export_otlp",
    ):
        monkeypatch.setattr(target, external_call)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    parent = tmp_path / "disposable"
    parent.mkdir()
    monkeypatch.chdir(unrelated)

    result = rehearsal.run_local_rehearsal(
        guided=True, confirm=lambda checkpoint: True, temp_parent=parent
    )

    assert result.harness_used is False
    assert result.integrated_sha == result.candidate_sha
    assert list(parent.iterdir()) == []
    assert list(unrelated.iterdir()) == []


def test_noninteractive_local_rehearsal_preserves_automatic_behavior(tmp_path):
    result = rehearsal.run_local_rehearsal(temp_parent=tmp_path)

    assert result.stopped_at is None
    assert result.harness_used is False
    assert result.integrated_sha == result.candidate_sha
    assert result.transitions[-1] == "local integration complete"
    assert list(tmp_path.iterdir()) == []


def test_live_harness_guidance_identifies_actual_provider_mode(tmp_path):
    phases = []
    checkpoints = []

    class ConfiguredHarness(rehearsal._FakeHarness):
        def review(self, prompt: str, cwd: Path) -> str:
            report = json.loads(super().review(prompt, cwd))
            report["summary"] = "Configured Harness reviewed this candidate"
            return json.dumps(report)

    def factory(phase):
        phases.append(phase)
        return ConfiguredHarness()

    result = rehearsal.run_harness_rehearsal(
        MachinistConfig(),
        harness_factory=factory,
        guided=True,
        confirm=lambda checkpoint: checkpoints.append(checkpoint) or True,
        temp_parent=tmp_path,
    )

    assert phases == ["spec", "execute", "review"]
    assert all(checkpoint.harness_used for checkpoint in checkpoints)
    assert (
        checkpoints[-1].review_report["summary"]
        == "Configured Harness reviewed this candidate"
    )
    assert result.harness_used is True
