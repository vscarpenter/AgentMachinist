from __future__ import annotations

import pytest
from test_local_workflow import (
    git,
    local,  # noqa: F401
)

from machinist.lifecycle import LifecycleError, Phase, RunStatus
from machinist.local_workflow import LocalWorkflow, LocalWorkflowError
from machinist.publication import PublicationError, ready_candidate


@pytest.fixture
def local_revision(local):  # noqa: F811
    root, workflow, harness = local
    git(root, "config", "maintenance.auto", "false")
    # Bind custody after configuring the fixture's Git metadata.
    workflow = LocalWorkflow(
        workflow.config,
        repo_root=root,
        harness_factory=lambda phase, number: harness,
    )
    return root, workflow, harness


@pytest.mark.parametrize("same_content", [False, True], ids=["changed", "identical"])
@pytest.mark.parametrize("stage", ["prepared", "committed", "retained", "delivered"])
def test_initial_revision_recovers_exact_commit_without_repeating_paid_work(
    local_revision, monkeypatch, stage, same_content
):
    root, workflow, harness = local_revision
    task = workflow.start("Improve the answer with its regression test")
    original_sha = task.spec_sha
    feedback = "Preserve the contract and clarify its acceptance criteria"
    if not same_content:
        harness.value = 3
    gate_calls = []
    original_runner = workflow.dispatcher._test_runner

    def count_gate(*args, **kwargs):
        gate_calls.append(args[0])
        return original_runner(*args, **kwargs)

    monkeypatch.setattr(workflow.dispatcher, "_test_runner", count_gate)
    with monkeypatch.context() as interrupted:
        if stage in {"prepared", "committed"}:
            original_commit = workflow.workspace.commit_all

            def interrupt_commit(path, message, **kwargs):
                if stage == "committed":
                    original_commit(path, message, **kwargs)
                raise RuntimeError(f"revision interrupted after {stage}")

            interrupted.setattr(workflow.workspace, "commit_all", interrupt_commit)
        elif stage == "retained":
            original_retain = workflow.workspace.retain_candidate

            def interrupt_retain(*args, **kwargs):
                original_retain(*args, **kwargs)
                raise RuntimeError("revision interrupted after retained")

            interrupted.setattr(
                workflow.workspace, "retain_candidate", interrupt_retain
            )
        else:
            original_update = workflow.store.update

            def interrupt_delivery(current, **changes):
                delivered = original_update(current, **changes)
                if changes.get("spec_sha") is not None:
                    raise RuntimeError("revision interrupted after delivered")
                return delivered

            interrupted.setattr(workflow.store, "update", interrupt_delivery)

        with pytest.raises(RuntimeError, match=f"after {stage}"):
            workflow.revise(task.id, feedback)

    pending = workflow.store.get(task.id)
    failed = workflow.lifecycle.record(task.number, Phase.SPEC)
    assert failed.status is RunStatus.FAILED
    assert failed.evidence["baseline_report"]["success"] is True
    assert failed.evidence["local_input_sha"] == original_sha
    assert failed.evidence["local_feedback"] == feedback
    assert pending.spec_base_sha == original_sha
    assert pending.approval is None and pending.candidate_sha is None
    assert pending.integration is None and pending.publication is None
    if stage == "delivered":
        assert pending.spec_sha == workflow.workspace.branch_sha(task.branch)
        assert pending.spec_sha != original_sha
    else:
        assert pending.spec_sha is None
    with pytest.raises(LocalWorkflowError, match="Spec"):
        workflow.approve(task.id, expected_sha=original_sha)
    with pytest.raises(LifecycleError, match="retry"):
        workflow.continue_task(task.id)
    with pytest.raises(PublicationError):
        ready_candidate(pending, workflow.workspace, workflow.lifecycle)

    def no_repeated_gate(*args, **kwargs):
        pytest.fail(
            "committed or prepared Spec recovery repeated baseline Verification"
        )

    monkeypatch.setattr(workflow.dispatcher, "_test_runner", no_repeated_gate)
    recovered = workflow.retry(task.id, phase=Phase.SPEC)

    assert recovered.spec_sha != original_sha
    assert recovered.spec_base_sha == original_sha
    assert recovered.feedback == feedback
    assert recovered.approval is None and recovered.candidate_sha is None
    assert recovered.base_sha == task.base_sha
    assert recovered.base_branch == task.base_branch
    assert workflow.workspace.branch_sha(task.branch) == recovered.spec_sha
    assert git(root, "rev-parse", f"{recovered.spec_sha}^") == original_sha
    assert git(root, "rev-parse", "HEAD") == task.base_sha
    assert harness.calls == ["spec", "spec"]
    assert len(gate_calls) == 1
    if same_content:
        assert git(root, "rev-parse", f"{recovered.spec_sha}^{{tree}}") == git(
            root, "rev-parse", f"{original_sha}^{{tree}}"
        )
    history = workflow.lifecycle.history(task.number, Phase.SPEC)
    assert [record.status for record in history] == [
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.SUCCEEDED,
    ]
    assert history[-1].evidence["spec_sha"] == recovered.spec_sha
    assert workflow.lifecycle.record(task.number, Phase.EXECUTE) is None
    assert workflow.lifecycle.record(task.number, Phase.REVIEW) is None
    status = workflow.status(task.id)
    assert status["state"] == "awaiting approval"
    assert status["next_action"].endswith(f"--spec-sha {recovered.spec_sha}")


def test_identical_initial_revision_gets_new_sha_and_requires_new_approval(
    local_revision,
):
    root, workflow, harness = local_revision
    task = workflow.start("Improve the answer with its regression test")

    revised = workflow.revise(task.id, "Keep the same implementation contract")

    assert revised.spec_sha != task.spec_sha
    assert git(root, "rev-parse", f"{revised.spec_sha}^") == task.spec_sha
    assert git(root, "rev-parse", f"{revised.spec_sha}^{{tree}}") == git(
        root, "rev-parse", f"{task.spec_sha}^{{tree}}"
    )
    assert revised.approval is None and revised.candidate_sha is None
    with pytest.raises(LocalWorkflowError, match="Spec"):
        workflow.approve(task.id, expected_sha=task.spec_sha)
    assert harness.calls == ["spec", "spec"]
    delivered = workflow.approve(task.id, expected_sha=revised.spec_sha)
    assert delivered.review_report["reviewed_sha"] == delivered.candidate_sha
    assert harness.calls == ["spec", "spec", "execute", "review"]


@pytest.mark.parametrize("phase", [Phase.EXECUTE, Phase.REVIEW])
def test_initial_revision_rejects_orphaned_implementation_history(
    local_revision, phase
):
    root, workflow, harness = local_revision
    task = workflow.start("Improve the answer with its regression test")
    workflow.lifecycle.run(task.number, phase, lambda claim: None)
    projection = workflow.lifecycle.runs_dir / f"issue-{task.number}-{phase.value}.json"
    projection.unlink()
    assert workflow.lifecycle.record(task.number, phase) is None
    assert workflow.lifecycle.history(task.number, phase)

    with pytest.raises(LocalWorkflowError, match="implementation has started"):
        workflow.revise(task.id, "Return three instead")

    assert workflow.store.get(task.id) == task
    assert workflow.workspace.branch_sha(task.branch) == task.spec_sha
    assert harness.calls == ["spec"]


def test_initial_revision_rejects_empty_review_metadata_without_mutation(
    local_revision,
):
    root, workflow, harness = local_revision
    task = workflow.start("Improve the answer with its regression test")
    task = workflow.store.update(task, review_report={})

    with pytest.raises(LocalWorkflowError, match="Review already exists"):
        workflow.revise(task.id, "Return three instead")

    assert workflow.store.get(task.id) == task
    assert workflow.workspace.branch_sha(task.branch) == task.spec_sha
    assert harness.calls == ["spec"]
