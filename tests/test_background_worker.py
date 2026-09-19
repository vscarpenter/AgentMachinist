"""Real Git workflow contracts; Harness and forge transports remain injected."""

import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from machinist.background import BackgroundError, BackgroundWorker
from machinist.background_github import CIObservation, QueuedIssue
from machinist.config import MachinistConfig
from machinist.forge import PublishedChange
from machinist.local_workflow import LocalWorkflow
from machinist.publication import publish_task


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


class Harness:
    name = "fake"
    config = MachinistConfig().harness

    def __init__(self):
        self.calls = []
        self.fail = False

    def generate_spec(self, prompt, cwd):
        self.calls.append("spec")
        return "# Spec\nReturn two and update the regression test.\n"

    def implement(self, prompt, cwd):
        self.calls.append("execute")
        if self.fail:
            raise RuntimeError("simulated provider failure")
        (cwd / "feature.py").write_text("def answer(): return 2\n")
        (cwd / "tests/test_feature.py").write_text(
            "import unittest\nfrom feature import answer\nclass Test(unittest.TestCase):\n def test_answer(self): self.assertEqual(answer(), 2)\n"
        )
        return "Fixed answer and regression test."

    def review(self, prompt, cwd):
        self.calls.append("review")
        return json.dumps({"version": 1, "summary": "Matches the task", "findings": []})


class GitHub:
    repo = "owner/demo"
    repo_host = "github.com"

    def __init__(self):
        self.queue = [
            QueuedIssue(
                7,
                "Return two",
                "Return two and test it",
                "https://github.com/owner/demo/issues/7",
                "101",
                "owner",
                "2026-09-18T12:00:00Z",
            )
        ]
        self.ready = []
        self.ci = CIObservation("passed", "CI gate passed")
        self.open_count = 0

    def queued_tasks(self, label):
        return tuple(self.queue)

    def open_bot_pr_count(self, branch_prefix):
        return self.open_count

    def observe_ci(self, sha, required_checks):
        assert len(sha) == 40 and required_checks == ("CI gate",)
        return self.ci

    def request_ready(self, number, sha, **kwargs):
        self.ready.append((number, sha))


@pytest.fixture
def pilot(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "remote", "add", "origin", "https://github.com/owner/demo.git")
    (root / ".gitignore").write_text("__pycache__/\n/.machinist/runs/\n")
    (root / "feature.py").write_text("def answer(): return 1\n")
    (root / "tests").mkdir()
    (root / "tests/test_feature.py").write_text(
        "import unittest\nfrom feature import answer\nclass Test(unittest.TestCase):\n def test_answer(self): self.assertEqual(answer(), 1)\n"
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    config = MachinistConfig.model_validate(
        {
            "workspace": {"root": str(tmp_path / "workshops")},
            "tests": {"command": f"{sys.executable} -B -m unittest discover -s tests"},
            "background": {
                "enabled": True,
                "image": "test:pilot",
                "required_checks": ["CI gate"],
            },
        }
    )
    harness, github, published, messages = Harness(), GitHub(), [], []
    clock = [1000.0]

    def workflow_factory(effective):
        return LocalWorkflow(
            effective,
            repo_root=root,
            harness_factory=lambda phase, number: harness,
            delegated_execution=True,
        )

    def publisher(task_id, *, store, workspace, lifecycle, forge, draft, cancel_check):
        assert draft and not cancel_check()
        task = store.get(task_id)
        assert task.approval is None and task.delegation
        published.append(task.candidate_sha)
        return store.update(
            task,
            publication={
                "stage": "published",
                "change_number": 11,
                "url": "https://github.com/owner/demo/pull/11",
            },
        )

    def factory(**kwargs):
        options = dict(
            github=github,
            forge=SimpleNamespace(
                host="github.com",
                repository="owner/demo",
                default_branch=lambda: "main",
                find_change=lambda branch: None,
            ),
            workflow_factory=workflow_factory,
            publisher=publisher,
            base_resolver=lambda workspace, branch: workspace.resolve_commit(),
            clock=lambda: clock[0],
            notify=messages.append,
        )
        options.update(kwargs)
        return BackgroundWorker(config, repo_root=root, **options)

    return SimpleNamespace(
        root=root,
        config=config,
        harness=harness,
        github=github,
        published=published,
        messages=messages,
        clock=clock,
        worker=factory,
    )


def test_issue_to_pr_without_manual_approval_and_duplicate_safe_restart(pilot):
    worker = pilot.worker()
    worker.run_once()
    assert pilot.harness.calls == ["spec", "execute", "review"]
    assert len(pilot.published) == 1
    assert len(pilot.github.ready) == 1
    assert worker.status()[0]["status"] == "ready"
    pilot.worker().run_once()
    assert len(pilot.published) == 1
    assert len(pilot.harness.calls) == 3
    assert len(pilot.messages) == 1
    assert git(pilot.root, "show", "HEAD:feature.py") == "def answer(): return 1"


def test_failed_run_requires_explicit_retry(pilot):
    worker = pilot.worker()
    pilot.harness.fail = True
    worker.run_once()
    pilot.harness.fail = False
    pilot.worker().run_once()
    assert pilot.harness.calls == ["spec", "execute"]
    assert worker.status()[0]["status"] == "failed"
    worker.retry("T1")
    worker.run_once()
    assert pilot.harness.calls == ["spec", "execute", "execute", "review"]
    assert len(pilot.published) == 1


def test_backlog_limit_prevents_intake(pilot):
    pilot.github.open_count = 2
    worker = pilot.worker()
    worker.run_once()
    assert worker.status() == []
    assert pilot.harness.calls == []


def test_ci_failure_preserves_draft_and_reports_once(pilot):
    pilot.github.ci = CIObservation("failed", "CI gate failed")
    worker = pilot.worker()
    worker.run_once()
    worker.run_once()
    assert worker.status()[0]["status"] == "needs_attention"
    assert pilot.github.ready == []
    assert len(pilot.published) == 1 and len(pilot.messages) == 1


def test_deadline_persists_while_waiting_for_ci(pilot):
    pilot.github.ci = CIObservation("pending", "CI gate pending")
    worker = pilot.worker()
    worker.run_once()
    assert worker.status()[0]["status"] == "awaiting_ci"
    pilot.clock[0] += 1801
    pilot.github.ci = CIObservation("passed", "CI gate passed")
    pilot.worker().run_once()
    assert worker.status()[0]["status"] == "needs_attention"
    assert pilot.github.ready == []


def test_cancel_before_readiness_never_marks_ready(pilot):
    pilot.github.ci = CIObservation("pending", "CI gate pending")
    worker = pilot.worker()
    worker.run_once()
    worker.cancel("T1")
    pilot.github.ci = CIObservation("passed", "CI gate passed")
    worker.run_once()
    assert worker.status()[0]["status"] == "cancelled"
    assert not pilot.github.ready


def test_corrupt_state_fails_closed(pilot):
    worker = pilot.worker()
    worker.run_once()
    (pilot.root / ".machinist/runs/background/state.json").write_text('{"version": 9}')
    with pytest.raises(BackgroundError):
        worker.run_once()


def test_failure_before_task_allocation_can_be_explicitly_retried(pilot):
    def unavailable(workspace, branch):
        raise OSError("temporary fetch failure")

    worker = pilot.worker(base_resolver=unavailable)
    worker.run_once()
    assert worker.status()[0]["status"] == "failed"
    assert worker.status()[0]["task_id"] is None
    assert not pilot.harness.calls
    worker.retry(issue_number=7)
    assert worker.status()[0]["status"] == "accepted"
    assert not pilot.harness.calls


def test_worker_claim_is_exclusive(pilot):
    worker = pilot.worker()
    with worker.state.claim():
        with pytest.raises(BackgroundError, match="another background worker"):
            with pilot.worker().state.claim():
                pytest.fail("duplicate worker acquired claim")


def test_persisted_policy_change_blocks_before_paid_work(pilot):
    def unavailable(workspace, branch):
        raise OSError("temporary fetch failure")

    worker = pilot.worker(base_resolver=unavailable)
    worker.run_once()
    worker.retry(issue_number=7)
    pilot.config.background.timeout_minutes = 31
    pilot.worker().run_once()
    assert worker.status()[0]["status"] == "failed"
    assert "policy changed" in worker.status()[0]["message"]
    assert not pilot.harness.calls


def test_retry_does_not_bypass_pr_backlog_limit(pilot):
    def unavailable(workspace, branch):
        raise OSError("temporary fetch failure")

    worker = pilot.worker(base_resolver=unavailable)
    worker.run_once()
    worker.retry(issue_number=7)
    pilot.github.open_count = 2
    pilot.worker().run_once()
    assert worker.status()[0]["status"] == "accepted"
    assert "Deferred" in worker.status()[0]["message"]
    assert not pilot.harness.calls


def test_complete_flow_uses_real_git_publication_and_recovers_interruption(pilot):
    remote = pilot.root.parent / "remote.git"
    git(pilot.root.parent, "init", "--bare", str(remote))
    git(pilot.root, "push", str(remote), "main")

    class Forge:
        provider, host, repository = "github", "github.com", "owner/demo"
        change = None
        created = 0

        def default_branch(self):
            return "main"

        def find_change(self, branch):
            return self.change

        def get_change(self, number):
            return self.change

        def create_change(self, *, branch, base, title, body, draft, cancel_check=None):
            assert cancel_check is None or not cancel_check()
            assert "delegated" in body.lower() and "human-approved" not in body
            self.created += 1
            head = git(remote, "rev-parse", f"refs/heads/{branch}")
            self.change = PublishedChange(
                self.provider,
                self.host,
                self.repository,
                11,
                "https://github.com/owner/demo/pull/11",
                branch,
                base,
                head,
                "OPEN",
                draft,
            )
            return self.change

        def update_change(self, number, *, title, body, draft, cancel_check=None):
            assert cancel_check is None or not cancel_check()
            self.change = replace(self.change, is_draft=draft)
            return self.change

    original_factory = pilot.worker()._workflow_factory

    def workflow_factory(effective):
        workflow = original_factory(effective)

        # Only the transport URL is injected. Controller publication still
        # performs real leased pushes, exact remote reads and durable records.
        def network(*args):
            return git(
                pilot.root,
                *(
                    str(remote) if arg == "https://github.com/owner/demo.git" else arg
                    for arg in args
                ),
            )

        workflow.workspace._network_git = network
        return workflow

    forge = Forge()
    interrupted = [False]

    def publication_with_crash(*args, **kwargs):
        task = publish_task(*args, **kwargs)
        if not interrupted[0]:
            interrupted[0] = True
            raise SystemExit("simulated controller exit after publication")
        return task

    worker = pilot.worker(
        forge=forge, workflow_factory=workflow_factory, publisher=publication_with_crash
    )
    with pytest.raises(SystemExit):
        worker.run_once()
    assert forge.created == 1
    assert pilot.harness.calls == ["spec", "execute", "review"]
    worker.run_once()
    assert worker.status()[0]["status"] == "ready"
    assert forge.created == 1
    assert pilot.harness.calls == ["spec", "execute", "review"]
    assert git(remote, "rev-parse", forge.change.branch) == pilot.github.ready[0][1]


def test_container_deadline_uses_persisted_active_repair_budget(pilot, monkeypatch):
    from test_repair import Scenario

    from machinist.lifecycle import Phase, TaskLifecycle

    scenario = Scenario()
    monkeypatch.setattr("machinist.lifecycle._now", lambda: scenario.clock.isoformat())
    worker = pilot.worker()
    outer_deadline = scenario.clock.timestamp() + 1800
    worker._active = {"task_id": "T1", "deadline_at": outer_deadline}
    lifecycle = TaskLifecycle(
        pilot.root / ".machinist/runs/local", repo_root=pilot.root
    )
    observed = []

    def execute(claim):
        original = scenario.checkpoint

        def checkpoint(values):
            original(values)
            claim.checkpoint(**values)

        scenario.checkpoint = checkpoint

        def implement(prompt, cancel_check):
            observed.append(worker._deadline())
            return scenario.implement(prompt, cancel_check)

        scenario.run(implement=implement)

    lifecycle.run(1, Phase.EXECUTE, execute)
    assert observed == [outer_deadline - 1740]
    assert worker._deadline() == outer_deadline


def test_fresh_execute_null_repair_keeps_outer_deadline(pilot):
    from machinist.lifecycle import Phase, TaskLifecycle

    worker = pilot.worker()
    worker._active = {"task_id": "T1", "deadline_at": 2800.0}
    lifecycle = TaskLifecycle(
        pilot.root / ".machinist/runs/local", repo_root=pilot.root
    )

    def execute(claim):
        claim.checkpoint(repair=None)
        assert worker._deadline() == 2800.0

    lifecycle.run(1, Phase.EXECUTE, execute)
    assert worker._deadline() == 2800.0


def test_fresh_retry_does_not_inherit_interrupted_repair_deadline(pilot, monkeypatch):
    from datetime import timedelta

    from test_repair import Scenario

    from machinist.lifecycle import Phase, TaskLifecycle

    scenario = Scenario()
    monkeypatch.setattr("machinist.lifecycle._now", lambda: scenario.clock.isoformat())
    worker = pilot.worker(clock=lambda: scenario.clock.timestamp())
    worker._active = {"task_id": "T1", "deadline_at": scenario.clock.timestamp() + 1800}
    lifecycle = TaskLifecycle(
        pilot.root / ".machinist/runs/local", repo_root=pilot.root
    )

    def interrupted_execute(claim):
        original = scenario.checkpoint

        def checkpoint(values):
            original(values)
            claim.checkpoint(**values)

        scenario.checkpoint = checkpoint

        def crash(prompt, cancel_check):
            assert worker._deadline() == scenario.clock.timestamp() + 60
            raise KeyboardInterrupt("controller interrupted during repair")

        scenario.run(implement=crash)

    with pytest.raises(KeyboardInterrupt):
        lifecycle.run(1, Phase.EXECUTE, interrupted_execute)
    scenario.clock += timedelta(minutes=2)
    lifecycle.retry(1, Phase.EXECUTE)
    outer_deadline = scenario.clock.timestamp() + 1800
    worker._active["deadline_at"] = outer_deadline
    assert worker._deadline() == outer_deadline

    def fresh_execute(claim):
        assert claim.attempt == 2
        assert not worker._stop_requested()
        claim.checkpoint(repair=None)
        assert worker._deadline() == outer_deadline

    lifecycle.run(1, Phase.EXECUTE, fresh_execute)
