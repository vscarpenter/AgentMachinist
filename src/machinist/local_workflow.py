"""Foreground local Task journey with explicit Approval and integration."""

from __future__ import annotations

import getpass
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from machinist.cancellation import CancellationStore
from machinist.config import MachinistConfig
from machinist.dispatch import TaskDispatcher
from machinist.lifecycle import LifecycleError, Phase, RunStatus, TaskLifecycle
from machinist.local_tasks import LocalTask, LocalTaskStore
from machinist.local_workspace import LocalWorkspace
from machinist.process import run_supervised
from machinist.publication import ready_candidate
from machinist.transitions import LocalTransitionDecision, classify_local_task


class LocalWorkflowError(Exception):
    """A local Task needs an explicit operator decision before proceeding."""


class LocalWorkflow:
    def __init__(
        self,
        config: MachinistConfig,
        *,
        repo_root: Path,
        harness_factory: Callable[[Phase, int], object] | None = None,
        store: LocalTaskStore | None = None,
        workspace: LocalWorkspace | None = None,
        test_runner: Callable[..., object] = run_supervised,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self._progress = progress
        self.repo_root = repo_root.resolve()
        self.store = store or LocalTaskStore(self.repo_root)
        self.workspace = workspace or LocalWorkspace(self.repo_root, config.workspace)
        if (
            self.store.repo_root != self.repo_root
            or self.workspace.repo_root != self.repo_root
        ):
            raise LocalWorkflowError(
                "local collaborators must belong to this repository"
            )
        runs = self.repo_root / ".machinist/runs/local"
        self.lifecycle = TaskLifecycle(runs, repo_root=self.repo_root)
        self.cancellation = CancellationStore(runs, repo_root=self.repo_root)
        self.dispatcher = TaskDispatcher(
            config,
            repo_root=self.repo_root,
            lifecycle=self.lifecycle,
            cancellation=self.cancellation,
            harness_factory=harness_factory,
            workspace_factory=lambda: self.workspace,
            test_runner=test_runner,
            progress=progress,
        )

    def start(
        self, title: str, body: str = "", *, source: dict[str, Any] | None = None
    ) -> LocalTask:
        """Record a local Task and produce its Spec; stop for exact-SHA Approval."""
        if not title.strip():
            raise LocalWorkflowError("a Task objective is required")
        if not any(gate.required for gate in self.config.resolved_verification_gates()):
            raise LocalWorkflowError(
                "configure a verification command before starting a local Task"
            )
        self.workspace.ensure_runtime_ignored()
        if self.workspace.has_changes(self.repo_root):
            raise LocalWorkflowError("start needs a clean committed checkout")
        base_branch = self.workspace.current_branch()
        if not base_branch:
            raise LocalWorkflowError("start needs a checked-out base branch")
        task = self.store.create(
            title.strip(),
            body.strip() or title.strip(),
            base_branch,
            self.workspace.resolve_commit(),
            self.config.workspace.branch_prefix,
            source=source,
        )
        if self._progress is not None:
            self._progress(
                f"Created {task.id}. Recover with machinist status {task.id}."
            )
        with self.store.claim(task.id) as task:
            return self.dispatcher.run_local_spec(task, store=self.store)

    def approve(
        self,
        task_id: str | int,
        *,
        expected_sha: str,
        actor: str | None = None,
    ) -> LocalTask:
        """Approve the displayed immutable Spec, then execute and review locally."""
        with self.store.claim(task_id) as task:
            spec = self.lifecycle.record(task.number, Phase.SPEC)
            if (
                task.spec_sha is None
                or expected_sha != task.spec_sha
                or spec is None
                or spec.status is not RunStatus.SUCCEEDED
                or spec.evidence.get("spec_sha") != task.spec_sha
            ):
                raise LocalWorkflowError(
                    "Spec changed or does not match successful Spec Evidence"
                )
            # Once complete, repeated Approval is an idempotent continuation.
            expected_branch = (
                task.candidate_sha if self._executed(task) else task.spec_sha
            )
            if self.workspace.branch_sha(task.branch) != expected_branch:
                raise LocalWorkflowError("Spec branch changed; refusing stale Approval")
            task = self.store.update(
                task,
                approval={
                    "repository": task.repository,
                    "task_id": task.id,
                    "spec_sha": task.spec_sha,
                    "actor": actor or getpass.getuser(),
                    "approved_at": datetime.now(UTC).isoformat(),
                },
            )
            return self._continue(task)

    def continue_task(self, task_id: str | int) -> LocalTask:
        with self.store.claim(task_id) as task:
            return self._continue(task)

    def _continue(self, task: LocalTask, *, resume: bool = False) -> LocalTask:
        spec = self.lifecycle.record(task.number, Phase.SPEC)
        if (
            task.spec_sha is None
            or spec is None
            or spec.status is not RunStatus.SUCCEEDED
            or spec.evidence.get("spec_sha") != task.spec_sha
        ):
            task = self.dispatcher.run_local_spec(task, store=self.store)
            if not self._approved(task):
                return task
        if not self._approved(task):
            raise LocalWorkflowError(
                f"Approval required: inspect the Spec, then run machinist approve "
                f"--task {task.id} --spec-sha {task.spec_sha}"
            )
        if not self._executed(task):
            task = self.dispatcher.run_local_execute(
                task, store=self.store, resume=resume
            )
        review = self.lifecycle.record(task.number, Phase.REVIEW)
        if (
            review is None
            or review.status is not RunStatus.SUCCEEDED
            or review.evidence.get("reviewed_sha") != task.candidate_sha
        ):
            task = self.dispatcher.run_local_review(task, store=self.store)
        ready_candidate(task, self.workspace, self.lifecycle)
        return task

    def amend(self, task_id: str | int, feedback: str) -> LocalTask:
        if not feedback.strip():
            raise LocalWorkflowError("amendment feedback must not be empty")
        with self.store.claim(task_id) as task:
            ready_candidate(task, self.workspace, self.lifecycle)
            if task.integration is not None:
                raise LocalWorkflowError(
                    "Task integration has started; create a new Task from the current base"
                )
            if task.publication and task.publication.get("stage") == "publishing":
                raise LocalWorkflowError(
                    "finish or reconcile pending publication before amendment"
                )
            task = self.store.update(
                task,
                feedback=feedback.strip(),
                spec_base_sha=task.candidate_sha,
                spec_sha=None,
                approval=None,
                review_report=None,
            )
            return self.dispatcher.run_local_spec(task, store=self.store, revise=True)

    def retry(
        self, task_id: str | int, *, phase: Phase, resume: bool = True
    ) -> LocalTask:
        with self.store.claim(task_id) as task:
            latest = self.lifecycle.latest(task.number)
            if latest is None or latest.phase is not phase:
                raise LifecycleError("retry must select the current failed Phase")
            self.lifecycle.retry(task.number, phase)
            self.cancellation.clear(task.number)
            if phase is Phase.SPEC:
                return self.dispatcher.run_local_spec(task, store=self.store)
            return self._continue(task, resume=resume)

    def integrate(self, task_id: str | int) -> LocalTask:
        with self.store.claim(task_id) as task:
            candidate = ready_candidate(task, self.workspace, self.lifecycle)
            intent = {
                "base_branch": task.base_branch,
                "base_sha": task.base_sha,
                "candidate_sha": candidate,
            }
            if task.integration is not None and any(
                task.integration.get(key) != value for key, value in intent.items()
            ):
                raise LocalWorkflowError("integration intent changed")
            task = self.store.update(task, integration=intent)
            observed = self.workspace.integrate(
                task.branch,
                base_branch=task.base_branch,
                expected_base_sha=task.base_sha,
                expected_candidate_sha=candidate,
            )
            task = self.store.update(
                task, integration={**intent, "observed_sha": observed}
            )
            return task

    def cancel(
        self,
        task_id: str | int,
        reason: str = "operator requested cancellation",
        *,
        clear: bool = False,
    ) -> object:
        # Cancellation must remain available while the foreground operation Claim is held.
        task = self.store.get(task_id)
        return (
            self.cancellation.clear(task.number)
            if clear
            else self.cancellation.request(task.number, reason)
        )

    def status(self, task_id: str | int) -> dict[str, Any]:
        task = self.store.get(task_id)
        records = {phase: self.lifecycle.record(task.number, phase) for phase in Phase}
        decision = classify_local_task(
            task, records=records, claim_held=self.lifecycle.claim_held(task.number)
        )
        expected_ref = task.candidate_sha if self._executed(task) else task.spec_sha
        if (
            decision.state
            in {"awaiting approval", "approved", "ready to integrate", "integrated"}
            and expected_ref
            and self.workspace.branch_sha(task.branch) != expected_ref
        ):
            decision = LocalTransitionDecision(
                "candidate changed", f"git log --oneline {task.branch}"
            )
        return {
            "id": task.id,
            "title": task.title,
            "state": decision.state,
            "next_action": decision.next_action,
            "spec_sha": task.spec_sha,
            "candidate_sha": task.candidate_sha,
            "spec": self.workspace.read_at_commit(
                task.spec_sha,
                f".machinist/specs/task-{task.number}-spec.md",
                max_bytes=self.config.limits.max_spec_chars * 4,
            )
            if task.spec_sha
            else None,
            "report": str(self.store.report_path(task)) if task.review_report else None,
            "publication": task.publication,
            "integration": task.integration,
        }

    def _executed(self, task: LocalTask) -> bool:
        record = self.lifecycle.record(task.number, Phase.EXECUTE)
        return bool(
            record
            and record.status is RunStatus.SUCCEEDED
            and record.evidence.get("approved_sha") == task.spec_sha
            and task.candidate_sha is not None
            and record.evidence.get("implementation_sha") == task.candidate_sha
        )

    @staticmethod
    def _approved(task: LocalTask) -> bool:
        return bool(
            task.spec_sha
            and task.approval
            and all(
                task.approval.get(key) == value
                for key, value in {
                    "repository": task.repository,
                    "task_id": task.id,
                    "spec_sha": task.spec_sha,
                }.items()
            )
        )
