"""Real local Git contracts for claimed Spec, Execute and Review Phases."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from machinist.config import MachinistConfig
from machinist.lifecycle import Phase, TaskLifecycle
from machinist.local_tasks import LocalTaskStore
from machinist.local_workspace import LocalWorkspace
from machinist.phases.local import (
    LocalPhaseError,
    run_local_execute,
    run_local_review,
    run_local_spec,
)


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=True
    ).stdout.strip()


class Harness:
    name = "fake"

    def __init__(self):
        self.calls = []
        self.spec_effect = None
        self.execute_effect = None
        self.review_effect = None
        self.findings = []

    def generate_spec(self, prompt, cwd):
        self.calls.append("spec")
        if self.spec_effect:
            self.spec_effect(cwd)
        return "# Spec\n\nReturn two from answer and test that behavior.\n"

    def implement(self, prompt, cwd):
        self.calls.append("execute")
        (cwd / "feature.py").write_text("def answer():\n    return 2\n")
        (cwd / "tests/test_feature.py").write_text(
            "import unittest\nfrom feature import answer\n"
            "class Contract(unittest.TestCase):\n"
            "    def test_answer(self): self.assertEqual(answer(), 2)\n"
        )
        if self.execute_effect:
            self.execute_effect(cwd)
        return "Implementation complete."

    def review(self, prompt, cwd):
        self.calls.append("review")
        if self.review_effect:
            self.review_effect(cwd)
        return json.dumps(
            {"version": 1, "summary": "Review complete", "findings": self.findings}
        )


@pytest.fixture
def local(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text("__pycache__/\n/.machinist/runs/\n")
    (root / "feature.py").write_text("def answer():\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests/test_feature.py").write_text(
        "import unittest\nfrom feature import answer\n"
        "class Contract(unittest.TestCase):\n"
        "    def test_answer(self): self.assertEqual(answer(), 1)\n"
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    config = MachinistConfig.model_validate(
        {
            "workspace": {"root": str(tmp_path / "workshops")},
            "tests": {"command": f"{sys.executable} -m unittest discover -s tests"},
            "review": {"enabled": True},
        }
    )
    store = LocalTaskStore(root)
    task = store.create(
        "Improve the answer",
        "Return two and add a regression test.",
        "main",
        git(root, "rev-parse", "HEAD"),
        "agent/",
    )
    lifecycle = TaskLifecycle(root / ".machinist/runs/local", repo_root=root)
    env = SimpleNamespace(
        root=root,
        config=config,
        store=store,
        task=task,
        lifecycle=lifecycle,
        harness=Harness(),
    )

    def run(phase, **kwargs):
        func = {
            Phase.SPEC: run_local_spec,
            Phase.EXECUTE: run_local_execute,
            Phase.REVIEW: run_local_review,
        }[phase]
        task = store.get(env.task.id)
        with store.claim(task.id):
            env.task = lifecycle.run(
                task.number,
                phase,
                lambda claim: func(
                    task,
                    env.config,
                    store=store,
                    workspace=LocalWorkspace(root, env.config.workspace),
                    harness=env.harness,
                    claim=claim,
                    cancel_check=kwargs.pop("cancel_check", None),
                    **kwargs,
                ),
            )
        return env.task

    def approve():
        task = store.get(env.task.id)
        env.task = store.update(
            task,
            approval={
                "repository": task.repository,
                "task_id": task.id,
                "spec_sha": task.spec_sha,
                "actor": "developer",
                "approved_at": datetime.now(UTC).isoformat(),
            },
        )
        return env.task

    env.run = run
    env.approve = approve
    return env


def test_local_pipeline_retains_exact_commits_and_review_evidence_without_origin(local):
    base = git(local.root, "rev-parse", "HEAD")
    task = local.run(Phase.SPEC)
    assert task.spec_sha == git(local.root, "rev-parse", task.branch)
    assert git(
        local.root, "show", f"{task.spec_sha}:.machinist/specs/task-1-spec.md"
    ).startswith("# Spec")
    assert task.candidate_sha is None
    local.approve()
    task = local.run(Phase.EXECUTE)
    assert task.candidate_sha != task.spec_sha
    task = local.run(Phase.REVIEW)
    assert task.review_report["reviewed_sha"] == task.candidate_sha
    assert task.review_report["completed"] is True
    assert local.store.read_report(task.id)
    assert local.harness.calls == ["spec", "execute", "review"]
    assert git(local.root, "rev-parse", "HEAD") == base
    assert git(local.root, "remote") == ""
    assert git(local.root, "status", "--porcelain") == ""


def test_baseline_gate_failure_stops_before_spec_harness(local):
    local.config.tests.command = f"{sys.executable} -c 'raise SystemExit(7)'"
    with pytest.raises(Exception, match="verification|baseline"):
        local.run(Phase.SPEC)
    assert local.harness.calls == []
    evidence = local.lifecycle.record(local.task.number, Phase.SPEC).evidence
    assert evidence["baseline_report"]["success"] is False


def _enable_repair(local):
    payload = local.config.model_dump(mode="json")
    payload["verification"]["repair"] = {"max_attempts": 1, "timeout_minutes": 10}
    local.config = MachinistConfig.model_validate(payload)


def _fail_initial_implementation(local):
    def effect(path):
        if local.harness.calls.count("execute") == 1:
            (path / "feature.py").write_text("def answer():\n    return 0\n")

    local.harness.execute_effect = effect


def test_local_repair_verifies_and_reviews_exact_repaired_candidate(local):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()
    _fail_initial_implementation(local)
    task = local.run(Phase.EXECUTE)
    assert local.harness.calls == ["spec", "execute", "execute"]
    evidence = local.lifecycle.record(task.number, Phase.EXECUTE).evidence
    assert evidence["verification_report"]["success"] is True
    assert evidence["repair"]
    assert git(local.root, "show", f"{task.candidate_sha}:feature.py").endswith(
        "return 2"
    )
    task = local.run(Phase.REVIEW)
    assert task.review_report["reviewed_sha"] == task.candidate_sha
    assert task.approval["spec_sha"] == task.spec_sha


def test_local_repair_exhaustion_never_delivers_or_replenishes_on_resume(local):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()
    local.harness.execute_effect = lambda path: (path / "feature.py").write_text(
        "def answer():\n    return 0\n"
    )
    with pytest.raises(Exception, match="verification"):
        local.run(Phase.EXECUTE)
    assert local.harness.calls == ["spec", "execute", "execute"]
    assert local.store.get(local.task.id).candidate_sha is None
    local.lifecycle.retry(local.task.number, Phase.EXECUTE)
    with pytest.raises(Exception, match="verification|repair"):
        local.run(Phase.EXECUTE, resume=True)
    assert local.harness.calls == ["spec", "execute", "execute"]
    assert local.store.get(local.task.id).candidate_sha is None


@pytest.mark.parametrize("violation", ["commit", "delete_test"])
def test_local_repair_rechecks_custody_and_change_limits(local, violation):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()

    def effect(path):
        if local.harness.calls.count("execute") == 1:
            (path / "feature.py").write_text("def answer():\n    return 0\n")
        elif violation == "commit":
            git(path, "add", ".")
            git(path, "commit", "-qm", "unauthorized repair")
        else:
            (path / "tests/test_feature.py").unlink()

    local.harness.execute_effect = effect
    with pytest.raises(Exception, match="HEAD|head|custody|deleted test"):
        local.run(Phase.EXECUTE)
    assert local.harness.calls == ["spec", "execute", "execute"]
    assert local.store.get(local.task.id).candidate_sha is None


def test_local_interrupted_repair_cannot_replay_paid_work_on_resume(local):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()

    def effect(path):
        (path / "feature.py").write_text("def answer():\n    return 0\n")
        if local.harness.calls.count("execute") == 2:
            raise RuntimeError("repair process interrupted")

    local.harness.execute_effect = effect
    with pytest.raises(RuntimeError, match="repair process interrupted"):
        local.run(Phase.EXECUTE)
    assert local.harness.calls == ["spec", "execute", "execute"]
    local.lifecycle.retry(local.task.number, Phase.EXECUTE)
    with pytest.raises(Exception, match="repair|fresh retry"):
        local.run(Phase.EXECUTE, resume=True)
    assert local.harness.calls == ["spec", "execute", "execute"]
    assert local.store.get(local.task.id).candidate_sha is None


def test_local_repaired_commit_recovery_does_not_repeat_harness_or_gates(
    local, monkeypatch
):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()
    _fail_initial_implementation(local)
    original = local.store.update

    def fail_delivery(task, **changes):
        if changes.get("candidate_sha"):
            raise RuntimeError("interrupted before repaired Task delivery")
        return original(task, **changes)

    monkeypatch.setattr(local.store, "update", fail_delivery)
    with pytest.raises(RuntimeError, match="Task delivery"):
        local.run(Phase.EXECUTE)
    assert local.harness.calls == ["spec", "execute", "execute"]
    monkeypatch.setattr(local.store, "update", original)
    local.lifecycle.retry(local.task.number, Phase.EXECUTE)

    def no_gates(*args, **kwargs):
        pytest.fail("successful repaired Verification repeated after commit")

    task = local.run(Phase.EXECUTE, resume=True, test_runner=no_gates)
    assert task.candidate_sha
    assert local.harness.calls == ["spec", "execute", "execute"]


def test_local_repair_reruns_all_gates_and_keeps_distinct_logs(local):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()
    payload = local.config.model_dump(mode="json")
    payload["tests"]["command"] = None
    payload["verification"]["gates"] = [
        {"name": "one", "command": "check-one"},
        {"name": "two", "command": "check-two"},
    ]
    local.config = MachinistConfig.model_validate(payload)
    commands = []

    def gates(argv, **kwargs):
        commands.append(argv)
        kwargs["stdout_log"].write_text("check output")
        return subprocess.CompletedProcess(
            argv, 1 if len(commands) == 1 else 0, stdout="check output", stderr=""
        )

    local.run(Phase.EXECUTE, test_runner=gates)
    assert commands == ["check-one", "check-two", "check-one", "check-two"]
    evidence = local.lifecycle.record(local.task.number, Phase.EXECUTE).evidence
    repair = evidence["repair"]
    initial_log = repair["initial_verification_report"]["gates"][0]["stdout_log"]
    final_log = evidence["verification_report"]["gates"][0]["stdout_log"]
    assert initial_log != final_log
    from pathlib import Path

    assert Path(initial_log).read_text() == "check output"
    assert Path(final_log).read_text() == "check output"


@pytest.mark.parametrize("failure", ["missing_command", "mutation"])
def test_local_control_failures_never_start_repair(local, failure):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()
    payload = local.config.model_dump(mode="json")
    payload["tests"]["command"] = None
    payload["verification"]["gates"] = [{"name": "check", "command": "check"}]
    local.config = MachinistConfig.model_validate(payload)

    def gate(argv, **kwargs):
        if failure == "mutation":
            (kwargs["cwd"] / "feature.py").write_text("unauthorized mutation")
        return subprocess.CompletedProcess(
            argv, 127 if failure == "missing_command" else 1, stdout="", stderr="failed"
        )

    with pytest.raises(Exception, match="verification"):
        local.run(Phase.EXECUTE, test_runner=gate)
    assert local.harness.calls == ["spec", "execute"]
    assert local.store.get(local.task.id).candidate_sha is None


def test_local_cancellation_during_repair_never_delivers(local):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()
    cancelled = False

    def effect(path):
        nonlocal cancelled
        if local.harness.calls.count("execute") == 1:
            (path / "feature.py").write_text("def answer():\n    return 0\n")
        else:
            cancelled = True

    local.harness.execute_effect = effect
    with pytest.raises(Exception, match="cancel"):
        local.run(Phase.EXECUTE, cancel_check=lambda: cancelled)
    assert local.harness.calls == ["spec", "execute", "execute"]
    assert local.store.get(local.task.id).candidate_sha is None


@pytest.mark.parametrize("malformed", [{}, [], ""])
def test_local_resume_rejects_malformed_repair_even_when_disabled(local, malformed):
    local.run(Phase.SPEC)
    local.approve()

    def failed_gate(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")

    with pytest.raises(Exception, match="verification"):
        local.run(Phase.EXECUTE, test_runner=failed_gate)
    path = local.lifecycle._path(local.task.number, Phase.EXECUTE)
    payload = json.loads(path.read_text())
    payload["evidence"]["repair"] = malformed
    path.write_text(json.dumps(payload))
    local.lifecycle.retry(local.task.number, Phase.EXECUTE)
    with pytest.raises(Exception, match="repair.*budget"):
        local.run(Phase.EXECUTE, resume=True)
    assert local.harness.calls == ["spec", "execute"]
    assert local.store.get(local.task.id).candidate_sha is None


def test_local_resume_keeps_instruction_provenance_when_no_new_harness_runs(local):
    local.run(Phase.SPEC)
    local.approve()
    payload = local.config.model_dump(mode="json")
    payload["instructions"]["execute"] = {"paths": ["feature.py"]}
    local.config = MachinistConfig.model_validate(payload)

    def failed_gate(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")

    with pytest.raises(Exception, match="verification"):
        local.run(Phase.EXECUTE, test_runner=failed_gate)
    consumed = local.lifecycle.record(local.task.number, Phase.EXECUTE).evidence[
        "instructions_sha256"
    ]
    local.lifecycle.retry(local.task.number, Phase.EXECUTE)
    local.run(Phase.EXECUTE, resume=True)
    assert local.harness.calls == ["spec", "execute"]
    assert (
        local.lifecycle.record(local.task.number, Phase.EXECUTE).evidence[
            "instructions_sha256"
        ]
        == consumed
    )


def test_local_resumed_repair_refuses_changed_instructions_before_paid_work(local):
    _enable_repair(local)
    local.run(Phase.SPEC)
    local.approve()
    payload = local.config.model_dump(mode="json")
    payload["instructions"]["execute"] = {"paths": ["feature.py"]}
    local.config = MachinistConfig.model_validate(payload)

    def interrupted_gate(command, **kwargs):
        raise RuntimeError("gate process vanished")

    with pytest.raises(RuntimeError, match="gate process vanished"):
        local.run(Phase.EXECUTE, test_runner=interrupted_gate)
    consumed = local.lifecycle.record(local.task.number, Phase.EXECUTE).evidence[
        "instructions_sha256"
    ]
    local.lifecycle.retry(local.task.number, Phase.EXECUTE)

    def failed_gate(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")

    with pytest.raises(LocalPhaseError, match="instructions.*--fresh"):
        local.run(Phase.EXECUTE, resume=True, test_runner=failed_gate)
    assert local.harness.calls == ["spec", "execute"]
    assert local.store.get(local.task.id).candidate_sha is None
    assert (
        local.lifecycle.record(local.task.number, Phase.EXECUTE).evidence[
            "instructions_sha256"
        ]
        == consumed
    )


def test_spec_read_only_contract_catches_harness_edits(local):
    local.harness.spec_effect = lambda path: (path / "feature.py").write_text(
        "unauthorized"
    )
    with pytest.raises(Exception, match="read-only|modified"):
        local.run(Phase.SPEC)
    assert local.store.get(local.task.id).spec_sha is None
    assert local.lifecycle.record(local.task.number, Phase.SPEC).evidence[
        "workspace_path"
    ]


@pytest.mark.parametrize(
    "change", ["missing", "wrong_sha", "wrong_task", "wrong_repository"]
)
def test_approval_is_exactly_bound_before_execute(local, change):
    task = local.run(Phase.SPEC)
    local.approve()
    task = local.store.get(task.id)
    approval = dict(task.approval)
    if change == "missing":
        approval = None
    elif change == "wrong_sha":
        approval["spec_sha"] = "a" * 40
    elif change == "wrong_task":
        approval["task_id"] = "T2"
    else:
        approval["repository"] = "/foreign"
    local.task = local.store.update(task, approval=approval)
    with pytest.raises(LocalPhaseError, match="Approval"):
        local.run(Phase.EXECUTE)
    assert local.harness.calls == ["spec"]


def test_execute_guard_rejects_harness_commit(local):
    local.run(Phase.SPEC)
    local.approve()

    def commit(path):
        git(path, "add", ".")
        git(path, "commit", "-qm", "unowned")

    local.harness.execute_effect = commit
    with pytest.raises(Exception, match="head|HEAD|custody"):
        local.run(Phase.EXECUTE)
    assert local.store.get(local.task.id).candidate_sha is None


def test_execute_reuses_existing_change_limits_for_deleted_tests(local):
    local.run(Phase.SPEC)
    local.approve()
    local.harness.execute_effect = lambda path: (
        path / "tests/test_feature.py"
    ).unlink()
    with pytest.raises(Exception, match="deleted test"):
        local.run(Phase.EXECUTE)
    assert local.store.get(local.task.id).candidate_sha is None


def test_execute_recovery_after_ref_delivery_does_not_rerun_paid_work_or_gates(
    local, monkeypatch
):
    local.run(Phase.SPEC)
    local.approve()
    original = local.store.update

    def fail_delivery(task, **changes):
        if changes.get("candidate_sha"):
            raise RuntimeError("interrupted before Task delivery")
        return original(task, **changes)

    monkeypatch.setattr(local.store, "update", fail_delivery)
    with pytest.raises(RuntimeError, match="Task delivery"):
        local.run(Phase.EXECUTE)
    monkeypatch.setattr(local.store, "update", original)
    record = local.lifecycle.record(local.task.number, Phase.EXECUTE)
    assert record.evidence["implementation_sha"]
    local.lifecycle.retry(local.task.number, Phase.EXECUTE)

    def no_gates(*args, **kwargs):
        pytest.fail("completed verification repeated")

    local.run(Phase.EXECUTE, resume=True, test_runner=no_gates)
    assert local.harness.calls == ["spec", "execute"]


def test_spec_recovery_after_ref_delivery_does_not_repeat_model(local, monkeypatch):
    original = local.store.update

    def fail_delivery(task, **changes):
        if changes.get("spec_sha"):
            raise RuntimeError("interrupted before Spec delivery")
        return original(task, **changes)

    monkeypatch.setattr(local.store, "update", fail_delivery)
    with pytest.raises(RuntimeError, match="Spec delivery"):
        local.run(Phase.SPEC)
    monkeypatch.setattr(local.store, "update", original)
    local.lifecycle.retry(local.task.number, Phase.SPEC)
    task = local.run(Phase.SPEC)
    assert task.spec_sha
    assert local.harness.calls == ["spec"]


def test_review_recovery_does_not_repeat_model_after_task_report_failure(
    local, monkeypatch
):
    local.run(Phase.SPEC)
    local.approve()
    local.run(Phase.EXECUTE)
    original = local.store.save_report

    def fail_report(*args):
        raise RuntimeError("report delivery interrupted")

    monkeypatch.setattr(local.store, "save_report", fail_report)
    with pytest.raises(RuntimeError, match="report delivery"):
        local.run(Phase.REVIEW)
    monkeypatch.setattr(local.store, "save_report", original)
    local.lifecycle.retry(local.task.number, Phase.REVIEW)
    local.run(Phase.REVIEW)
    assert local.harness.calls == ["spec", "execute", "review"]
    from pathlib import Path

    assert not Path(
        local.lifecycle.record(local.task.number, Phase.REVIEW).evidence[
            "workspace_path"
        ]
    ).exists()


def test_review_read_only_guard_rejects_working_tree_edits(local):
    local.run(Phase.SPEC)
    local.approve()
    local.run(Phase.EXECUTE)
    local.harness.review_effect = lambda path: (path / "feature.py").write_text(
        "changed"
    )
    with pytest.raises(Exception, match="read-only|modified"):
        local.run(Phase.REVIEW)
    assert local.store.get(local.task.id).review_report is None


def test_review_findings_remain_visible_advisory_evidence(local):
    local.run(Phase.SPEC)
    local.approve()
    local.run(Phase.EXECUTE)
    local.harness.findings = [
        {
            "severity": "high",
            "confidence": "high",
            "file": "feature.py",
            "line": 2,
            "requirement": "example",
            "message": "Needs human attention",
            "remediation": "Inspect it",
        }
    ]
    task = local.run(Phase.REVIEW)
    assert task.review_report["findings"][0]["severity"] == "high"
    report = local.store.read_report(task.id)
    assert (
        "Needs human attention" in report and "passed independent review" not in report
    )


@pytest.mark.parametrize("phase", [Phase.SPEC, Phase.EXECUTE])
def test_commit_checkpoint_gap_recovers_exact_tree_without_paid_work(
    local, monkeypatch, phase
):
    if phase is Phase.EXECUTE:
        local.run(Phase.SPEC)
        local.approve()
    original = LocalWorkspace.commit_all

    def interrupt_after_commit(workspace, path, message):
        original(workspace, path, message)
        raise RuntimeError("interrupted after Git commit")

    monkeypatch.setattr(LocalWorkspace, "commit_all", interrupt_after_commit)
    with pytest.raises(RuntimeError, match="after Git commit"):
        local.run(phase)
    checkpoint = local.lifecycle.record(local.task.number, phase).evidence
    assert checkpoint["local_commit_intent"]["tree_sha"]
    assert not checkpoint.get(
        "spec_sha" if phase is Phase.SPEC else "implementation_sha"
    )
    calls = local.harness.calls[:]
    producer = checkpoint["harness"]
    monkeypatch.setattr(LocalWorkspace, "commit_all", original)
    local.harness.name = "newly-configured-harness"
    local.lifecycle.retry(local.task.number, phase)

    def no_gates(*_args, **_kwargs):
        pytest.fail("completed Gates must not repeat after commit")

    local.run(phase, test_runner=no_gates)
    assert local.harness.calls == calls
    assert (
        local.lifecycle.record(local.task.number, phase).evidence["harness"] == producer
    )


def test_local_deliveries_preserve_publication_identity_for_next_lease(local):
    publication = {"stage": "published", "number": 77, "head_sha": local.task.base_sha}
    local.task = local.store.update(local.task, publication=publication)
    task = local.run(Phase.SPEC)
    assert task.publication == publication
    local.approve()
    task = local.run(Phase.EXECUTE)
    assert task.publication == publication


def test_prepared_commit_recovery_rejects_changed_bytes(local, monkeypatch):
    local.run(Phase.SPEC)
    local.approve()
    original = LocalWorkspace.commit_all

    def before_commit(_workspace, _path, _message):
        raise RuntimeError("interrupted before Git commit")

    monkeypatch.setattr(LocalWorkspace, "commit_all", before_commit)
    with pytest.raises(RuntimeError, match="before Git commit"):
        local.run(Phase.EXECUTE)
    from pathlib import Path

    evidence = local.lifecycle.record(local.task.number, Phase.EXECUTE).evidence
    (Path(evidence["workspace_path"]) / "feature.py").write_text(
        "changed after verification"
    )
    monkeypatch.setattr(LocalWorkspace, "commit_all", original)
    local.lifecycle.retry(local.task.number, Phase.EXECUTE)
    with pytest.raises(LocalPhaseError, match="bytes changed"):
        local.run(Phase.EXECUTE, resume=True)
    assert local.harness.calls == ["spec", "execute"]


@pytest.mark.parametrize("advisory_only", [False, True])
def test_removed_required_gates_after_spec_block_execute_before_harness(
    local, advisory_only
):
    local.run(Phase.SPEC)
    local.approve()
    payload = local.config.model_dump(mode="json")
    payload["tests"]["command"] = None
    if advisory_only:
        payload["verification"]["gates"] = [
            {
                "name": "advice",
                "command": f"{sys.executable} -c 'pass'",
                "required": False,
            }
        ]
    local.config = MachinistConfig.model_validate(payload)
    with pytest.raises(
        LocalPhaseError, match="required.*[Vv]erification|[Vv]erification.*required"
    ):
        local.run(Phase.EXECUTE)
    assert local.harness.calls == ["spec"]
    assert local.store.get(local.task.id).candidate_sha is None
