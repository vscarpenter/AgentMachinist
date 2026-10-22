from __future__ import annotations

import subprocess

import pytest
from test_local_workflow import (
    git,
    local,  # noqa: F401
)

from machinist.lifecycle import LifecycleError, Phase, RunStatus
from machinist.local_workflow import LocalWorkflow, LocalWorkflowError
from machinist.publication import PublicationError, ready_candidate
from machinist.workspace import WorkspaceError


@pytest.fixture
def local_revision(local):  # noqa: F811
    root, workflow, harness = local
    git(root, "config", "maintenance.auto", "false")
    git(root, "config", "core.autocrlf", "false")
    # Bind custody after configuring the fixture's Git metadata.
    workflow = LocalWorkflow(
        workflow.config,
        repo_root=root,
        harness_factory=lambda phase, number: harness,
    )
    return root, workflow, harness


def test_revision_comparison_uses_saved_commits_and_is_read_only(local_revision):
    root, workflow, harness = local_revision
    task = workflow.start("Improve the answer")
    first = task.spec_sha
    harness.value = 3
    task = workflow.revise(task.id, "Return three instead")
    before = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }

    comparison = workflow.status(task.id)["spec_revision"]

    assert comparison["previous_sha"] == first
    assert comparison["current_sha"] == task.spec_sha
    assert "-Return 2 from answer" in comparison["text"]
    assert "+Return 3 from answer" in comparison["text"]
    assert comparison["unchanged"] is False
    assert comparison["error"] is None
    assert before == {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert harness.calls == ["spec", "spec"]


def test_identical_revision_comparison_still_names_new_approval(local_revision):
    _root, workflow, _harness = local_revision
    task = workflow.start("Improve the answer")
    first = task.spec_sha
    task = workflow.revise(task.id, "Clarify the same behavior")

    status = workflow.status(task.id)

    assert status["spec_revision"]["unchanged"] is True
    assert status["spec_revision"]["text"] == ""
    assert task.spec_sha != first
    assert task.spec_sha in status["next_action"]


def test_revision_comparison_detects_committed_line_ending_only_changes(
    local_revision, monkeypatch
):
    root, workflow, harness = local_revision
    task = workflow.start("Improve the answer")
    first = task.spec_sha
    path = ".machinist/specs/task-1-spec.md"
    previous = subprocess.run(
        ["git", "cat-file", "blob", f"{first}:{path}"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout

    def crlf_spec(prompt, cwd):
        harness.calls.append("spec")
        return previous.decode("utf-8").replace("\n", "\r\n")

    monkeypatch.setattr(harness, "generate_spec", crlf_spec)
    task = workflow.revise(task.id, "Use CRLF line endings for the same Spec")
    current = subprocess.run(
        ["git", "cat-file", "blob", f"{task.spec_sha}:{path}"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    assert b"\r" not in previous
    assert current == previous.replace(b"\n", b"\r\n")
    # A fresh CLI status read has no active Phase cancellation callback, unlike
    # the workflow instance that just generated the Spec.
    workflow = LocalWorkflow(
        workflow.config,
        repo_root=root,
        harness_factory=lambda phase, number: harness,
    )
    before = {
        str(file.relative_to(root)): file.read_bytes()
        for file in root.rglob("*")
        if file.is_file()
    }

    status = workflow.status(task.id)
    comparison = status["spec_revision"]

    assert comparison["unchanged"] is False
    assert comparison["line_endings_only"] is True
    assert (
        "Spec text differs only by line endings; "
        "this revision still requires fresh Approval."
    ) in comparison["text"]
    assert "\n-" not in comparison["text"]
    assert comparison["error"] is None
    assert first in comparison["command"]
    assert task.spec_sha in comparison["command"]
    assert workflow.workspace.read_at_commit(
        task.spec_sha, path, max_bytes=len(current)
    ) == current.decode("utf-8")
    assert task.spec_sha in status["next_action"]
    assert harness.calls == ["spec", "spec"]
    assert before == {
        str(file.relative_to(root)): file.read_bytes()
        for file in root.rglob("*")
        if file.is_file()
    }


def test_revision_comparison_uses_latest_pair_after_multiple_revisions(local_revision):
    _root, workflow, harness = local_revision
    task = workflow.start("Improve the answer")
    harness.value = 3
    task = workflow.revise(task.id, "Return three")
    second = task.spec_sha
    harness.value = 4
    task = workflow.revise(task.id, "Return four")

    comparison = workflow.status(task.id)["spec_revision"]

    assert comparison["previous_sha"] == second
    assert "-Return 3 from answer" in comparison["text"]
    assert "+Return 4 from answer" in comparison["text"]


def test_revision_comparison_failure_does_not_hide_saved_plan(
    local_revision, monkeypatch
):
    _root, workflow, harness = local_revision
    task = workflow.start("Improve the answer")
    first = task.spec_sha
    harness.value = 3
    task = workflow.revise(task.id, "Return three")
    read = workflow.workspace.read_at_commit

    def missing_previous(sha, *args, **kwargs):
        if sha == first:
            raise WorkspaceError("missing old blob API_KEY=sample-secret\x1b[31m")
        return read(sha, *args, **kwargs)

    monkeypatch.setattr(workflow.workspace, "read_at_commit", missing_previous)
    status = workflow.status(task.id)

    assert status["state"] == "awaiting approval"
    assert "Return 3" in status["spec"]
    assert "comparison unavailable" in status["spec_revision"]["error"]
    assert "sample-secret" not in status["spec_revision"]["error"]
    assert "\x1b" not in status["spec_revision"]["error"]
    assert first in status["spec_revision"]["command"]
    assert task.spec_sha in status["next_action"]


def test_revision_comparison_is_bounded_sanitized_and_compares_raw_text(
    local_revision, monkeypatch
):
    _root, workflow, harness = local_revision
    task = workflow.start("Improve the answer")
    first = task.spec_sha
    harness.value = 3
    task = workflow.revise(task.id, "Return three")
    monkeypatch.setattr("machinist.local_workflow._MAX_REVISION_CHARS", 500)

    def synthetic_text(sha, *args, **kwargs):
        secret = "old-secret" if sha == first else "new-secret"
        return "API_KEY=" + secret + "\x1b[31m\n" + "many details\n" * 200

    monkeypatch.setattr(workflow.workspace, "read_at_commit", synthetic_text)
    comparison = workflow.status(task.id)["spec_revision"]

    assert comparison["unchanged"] is False
    assert "old-secret" not in comparison["text"]
    assert "new-secret" not in comparison["text"]
    assert "\x1b" not in comparison["text"]
    assert "[REDACTED]" in comparison["text"]
    assert "--no-ext-diff --no-textconv --no-color" in comparison["command"]
    assert comparison["command"].endswith("-- .machinist/specs/task-1-spec.md")

    monkeypatch.setattr(
        workflow.workspace,
        "read_at_commit",
        lambda sha, *args, **kwargs: (
            ("old details\n" if sha == first else "new details\n") * 200
        ),
    )
    large = workflow.status(task.id)["spec_revision"]
    assert len(large["text"]) <= 500
    assert large["truncated"] is True

    monkeypatch.setattr(
        workflow.workspace,
        "read_at_commit",
        lambda sha, *args, **kwargs: "old\n" if sha == first else "new",
    )
    no_newline = workflow.status(task.id)["spec_revision"]["text"]
    assert "-old\n+new\n\\ No newline at end of file" in no_newline


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
    assert status["spec_revision"]["previous_sha"] == original_sha
    assert status["spec_revision"]["current_sha"] == recovered.spec_sha
    assert status["spec_revision"]["unchanged"] is same_content


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
