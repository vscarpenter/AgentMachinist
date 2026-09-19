from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from machinist.config import MachinistConfig
from machinist.lifecycle import LifecycleError, Phase, RunStatus
from machinist.local_workflow import LocalWorkflow, LocalWorkflowError


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


class Harness:
    name = "fake"
    config = MachinistConfig().harness

    def __init__(self):
        self.calls = []
        self.value = 2
        self.fail = False
        self.commit = False

    def generate_spec(self, prompt, cwd):
        self.calls.append("spec")
        return f"# Spec\n\nReturn {self.value} from answer and test it.\n"

    def implement(self, prompt, cwd):
        self.calls.append("execute")
        (cwd / "feature.py").write_text(f"def answer():\n    return {self.value}\n")
        (cwd / "tests/test_feature.py").write_text(
            "import unittest\nfrom feature import answer\n"
            "class TestFeature(unittest.TestCase):\n"
            f"    def test_answer(self): self.assertEqual(answer(), {self.value})\n"
        )
        if self.commit:
            git(cwd, "add", ".")
            git(cwd, "commit", "-m", "unowned commit")
        if self.fail:
            raise RuntimeError("Harness interrupted after edits")
        return "Updated answer and tests."

    def review(self, prompt, cwd):
        self.calls.append("review")
        return json.dumps({"version": 1, "summary": "Matches the Spec", "findings": []})


@pytest.fixture
def local(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text("__pycache__/\n/.machinist/runs/\n")
    (root / "feature.py").write_text("def answer():\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests/test_feature.py").write_text(
        "import unittest\nfrom feature import answer\n"
        "class TestFeature(unittest.TestCase):\n"
        "    def test_answer(self): self.assertEqual(answer(), 1)\n"
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    config = MachinistConfig.model_validate(
        {
            "workspace": {"root": str(tmp_path / "workshops")},
            "tests": {"command": f"{sys.executable} -m unittest discover -s tests"},
            "review": {"enabled": True},
        }
    )
    harness = Harness()
    workflow = LocalWorkflow(
        config,
        repo_root=root,
        harness_factory=lambda phase, number: harness,
        delegated_execution=True,
    )
    return root, workflow, harness


def test_no_origin_task_runs_to_review_and_explicit_integration(local):
    root, workflow, harness = local
    base = git(root, "rev-parse", "HEAD")
    task = workflow.start("Improve the answer with its regression test")
    assert task.id == "T1" and task.spec_sha
    assert workflow.status(task.id)["state"] == "awaiting approval"
    assert git(root, "rev-parse", "HEAD") == base
    task = workflow.approve(task.id, expected_sha=task.spec_sha, actor="developer")
    assert harness.calls == ["spec", "execute", "review"]
    assert task.candidate_sha != task.spec_sha
    assert task.review_report["reviewed_sha"] == task.candidate_sha
    assert workflow.status(task.id)["state"] == "ready to integrate"
    assert git(root, "rev-parse", "HEAD") == base
    result = workflow.integrate(task.id)
    assert result.integration["observed_sha"] == result.candidate_sha
    assert git(root, "rev-parse", "HEAD") == result.candidate_sha
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "remote") == ""
    assert workflow.store.read_report(task.id)


def test_unapproved_and_stale_spec_never_execute(local):
    root, workflow, harness = local
    task = workflow.start("Improve the answer with its regression test")
    with pytest.raises(LocalWorkflowError, match="Approval"):
        workflow.continue_task(task.id)
    with pytest.raises(LocalWorkflowError, match="Spec.*changed|Spec.*match"):
        workflow.approve(task.id, expected_sha="f" * 40)
    assert harness.calls == ["spec"]


def test_amendment_requires_fresh_approval_and_second_review(local):
    root, workflow, harness = local
    task = workflow.start("Improve the answer with its regression test")
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    old = task.candidate_sha
    harness.value = 3
    task = workflow.amend(task.id, "Return three instead and adjust the test")
    assert task.approval is None and task.spec_sha
    with pytest.raises(LocalWorkflowError, match="Approval"):
        workflow.continue_task(task.id)
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    assert task.candidate_sha != old
    assert harness.calls == ["spec", "execute", "review", "spec", "execute", "review"]
    assert len(workflow.lifecycle.history(task.number, Phase.REVIEW)) == 2


def test_failed_execute_needs_explicit_retry_and_resume_preserves_edits(local):
    root, workflow, harness = local
    task = workflow.start("Improve the answer with its regression test")
    harness.fail = True
    with pytest.raises(RuntimeError, match="interrupted"):
        workflow.approve(task.id, expected_sha=task.spec_sha)
    with pytest.raises(LifecycleError, match="retry"):
        workflow.continue_task(task.id)
    harness.fail = False
    task = workflow.retry(task.id, phase=Phase.EXECUTE, resume=True)
    assert task.review_report["completed"]
    assert (
        workflow.lifecycle.record(task.number, Phase.EXECUTE).status
        is RunStatus.SUCCEEDED
    )


def test_duplicate_continuation_does_not_repeat_paid_work(local):
    root, workflow, harness = local
    task = workflow.start("Improve the answer with its regression test")
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    workflow.continue_task(task.id)
    assert harness.calls == ["spec", "execute", "review"]


def test_harness_commit_is_detected_and_no_candidate_delivered(local):
    root, workflow, harness = local
    task = workflow.start("Improve the answer with its regression test")
    harness.commit = True
    with pytest.raises(Exception, match="head|HEAD|custody"):
        workflow.approve(task.id, expected_sha=task.spec_sha)
    assert workflow.store.get(task.id).candidate_sha is None


def test_base_drift_and_dirty_checkout_block_integration(local):
    root, workflow, harness = local
    task = workflow.start("Improve the answer with its regression test")
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    (root / "feature.py").write_text("# unrelated edits\n")
    with pytest.raises(Exception, match="clean"):
        workflow.integrate(task.id)
    git(root, "add", "feature.py")
    git(root, "commit", "-m", "concurrent human change")
    with pytest.raises(Exception, match="base branch changed|base.*changed"):
        workflow.integrate(task.id)


def test_pending_amendment_recovers_before_new_spec_claim(local, monkeypatch):
    root, workflow, harness = local
    task = workflow.start("Improve the answer with its regression test")
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    original = workflow.dispatcher.run_local_spec

    def interrupted(*args, **kwargs):
        raise RuntimeError("crash after amendment intent")

    monkeypatch.setattr(workflow.dispatcher, "run_local_spec", interrupted)
    with pytest.raises(RuntimeError, match="amendment intent"):
        workflow.amend(task.id, "Return three and update its test")
    monkeypatch.setattr(workflow.dispatcher, "run_local_spec", original)
    harness.value = 3
    recovered = workflow.continue_task(task.id)
    assert recovered.spec_sha and recovered.spec_sha != task.spec_sha
    assert recovered.approval is None
    assert harness.calls == ["spec", "execute", "review", "spec"]


def test_completed_candidate_drift_is_not_reported_as_success(local):
    root, workflow, harness = local
    task = workflow.start("Improve the answer with its regression test")
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    git(root, "update-ref", f"refs/heads/{task.branch}", task.base_sha)
    assert workflow.status(task.id)["state"] == "candidate changed"
    with pytest.raises(Exception, match="candidate branch changed"):
        workflow.continue_task(task.id)
    assert harness.calls == ["spec", "execute", "review"]


def test_no_verification_command_is_rejected_before_task_or_model(local):
    root, workflow, harness = local
    unverified = LocalWorkflow(
        workflow.config.model_copy(update={"tests": MachinistConfig().tests}),
        repo_root=root,
        harness_factory=lambda phase, number: harness,
    )
    with pytest.raises(LocalWorkflowError, match="verification command"):
        unverified.start("Improve the answer with its regression test")
    assert harness.calls == []
    assert unverified.store.list() == ()


def test_published_task_amendment_preserves_remote_lease_and_change_identity(local):
    from dataclasses import replace

    from machinist.forge import PublishedChange
    from machinist.publication import publish_task

    root, workflow, harness = local
    pushes = []

    class PublicationWorkspace:
        repo_root = root
        remote = None

        def origin_url(self):
            return "git@gitlab.com:team/project.git"

        def bind_publication_auth(self, provider, *, origin_url):
            assert provider == "gitlab" and origin_url == self.origin_url()

        def branch_sha(self, branch):
            return workflow.workspace.branch_sha(branch)

        def remote_sha(self, branch, *, origin_url):
            assert origin_url == self.origin_url()
            return self.remote

        def push_candidate(
            self, branch, *, expected_candidate_sha, expected_remote_sha, origin_url
        ):
            assert origin_url == self.origin_url()
            assert expected_remote_sha == self.remote
            assert self.branch_sha(branch) == expected_candidate_sha
            pushes.append((expected_remote_sha, expected_candidate_sha))
            self.remote = expected_candidate_sha
            return self.remote

    transport = PublicationWorkspace()

    class Forge:
        provider = "gitlab"
        host = "gitlab.com"
        repository = "team/project"
        change = None
        creates = 0

        def find_change(self, branch):
            if self.change is not None:
                self.change = replace(self.change, head_sha=transport.remote)
            return self.change

        def create_change(self, *, branch, base, title, body, draft):
            self.creates += 1
            self.change = PublishedChange(
                self.provider,
                self.host,
                self.repository,
                17,
                "https://gitlab.com/team/project/-/merge_requests/17",
                branch,
                base,
                transport.remote,
                "OPEN",
                draft,
            )
            return self.change

        def update_change(self, number, *, title, body, draft):
            assert number == self.change.number
            self.change = replace(self.change, is_draft=draft)
            return self.change

        def get_change(self, number):
            assert number == self.change.number
            return self.change

    forge = Forge()

    def publish(task):
        return publish_task(
            task.id,
            store=workflow.store,
            workspace=transport,
            forge=forge,
            lifecycle=workflow.lifecycle,
        )

    task = workflow.start("Improve the answer with its regression test")
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    task = publish(task)
    original = task.candidate_sha
    publication = dict(task.publication)
    harness.value = 3
    task = workflow.amend(task.id, "Return three instead and adjust the test")
    assert task.publication == publication
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    assert task.publication == publication
    task = publish(task)

    assert task.candidate_sha != original
    assert pushes == [(None, original), (original, task.candidate_sha)]
    assert forge.creates == 1
    assert task.publication["change_number"] == 17
    assert task.publication["published_sha"] == task.candidate_sha
    assert harness.calls == ["spec", "execute", "review", "spec", "execute", "review"]


def delegate_task(root, workflow):
    from machinist.authorization import make_delegation

    task = workflow.store.create(
        "Improve the answer with its regression test",
        "Return two and test it",
        "main",
        git(root, "rev-parse", "HEAD"),
        workflow.config.workspace.branch_prefix,
        source={
            "provider": "github",
            "repository": "team/project",
            "issue_number": 7,
            "event_id": "42",
            "actor": "vinny",
        },
    )
    return workflow.store.update(
        task,
        delegation=make_delegation(
            task, workflow.config, actor="vinny", source_event="42"
        ),
    )


def test_delegated_task_runs_through_review_without_human_approval(local):
    root, workflow, harness = local
    task = delegate_task(root, workflow)
    original_implement, original_review = harness.implement, harness.review
    prompts = []

    def implement(prompt, cwd):
        prompts.append(prompt)
        return original_implement(prompt, cwd)

    def review(prompt, cwd):
        prompts.append(prompt)
        return original_review(prompt, cwd)

    harness.implement, harness.review = implement, review
    task = workflow.continue_task(task.id)
    assert all("reviewed and approved by a human" not in prompt for prompt in prompts)
    assert "## The internal spec" in prompts[0]
    assert "## Internal Spec" in prompts[1]
    assert harness.calls == ["spec", "execute", "review"]
    assert task.approval is None
    assert task.delegation["spec_sha"] == task.spec_sha
    assert task.review_report["reviewed_sha"] == task.candidate_sha
    evidence = workflow.lifecycle.record(task.number, Phase.EXECUTE).evidence
    assert evidence["authorization"]["kind"] == "delegation"
    assert evidence["authorization"]["spec_sha"] == task.spec_sha
    assert workflow.status(task.id)["state"] == "ready to publish"
    workflow.continue_task(task.id)
    assert harness.calls == ["spec", "execute", "review"]
    assert git(root, "rev-parse", "HEAD") == task.base_sha


def test_delegated_policy_change_stops_before_spec_harness(local):
    root, workflow, harness = local
    task = delegate_task(root, workflow)
    workflow.config.limits.max_changed_files += 1
    with pytest.raises(LocalWorkflowError, match="configuration"):
        workflow.continue_task(task.id)
    with pytest.raises(LocalWorkflowError, match="configuration"):
        workflow.retry(task.id, phase=Phase.SPEC)
    assert harness.calls == []


def test_delegated_task_rejects_manual_approval_amendment_and_integration(local):
    root, workflow, harness = local
    task = delegate_task(root, workflow)
    for operation in (
        lambda: workflow.approve(task.id, expected_sha="a" * 40),
        lambda: workflow.amend(task.id, "Expand the task"),
        lambda: workflow.integrate(task.id),
    ):
        with pytest.raises(LocalWorkflowError, match="delegated"):
            operation()
    assert harness.calls == []


def test_delegated_failed_spec_retry_continues_through_review(local):
    root, workflow, harness = local
    task = delegate_task(root, workflow)
    original = harness.generate_spec
    harness.generate_spec = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("interrupted")
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        workflow.continue_task(task.id)
    with pytest.raises(LifecycleError, match="retry"):
        workflow.continue_task(task.id)
    harness.generate_spec = original
    task = workflow.retry(task.id, phase=Phase.SPEC)
    assert task.review_report["completed"]
    assert task.approval is None


def test_generic_continuation_cannot_run_a_delegated_task_on_host(local):
    root, workflow, harness = local
    task = delegate_task(root, workflow)
    foreground = LocalWorkflow(
        workflow.config, repo_root=root, harness_factory=lambda phase, number: harness
    )
    with pytest.raises(LocalWorkflowError, match="background runtime"):
        foreground.continue_task(task.id)
    with pytest.raises(LocalWorkflowError, match="background runtime"):
        foreground.retry(task.id, phase=Phase.SPEC)
    assert workflow.lifecycle.record(task.number, Phase.SPEC) is None
    assert harness.calls == []
