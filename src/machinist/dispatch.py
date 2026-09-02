"""Construction and dispatch of claimed Phase Task Runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TypeVar

from machinist.cancellation import CancellationStore
from machinist.config import MachinistConfig
from machinist.github import DraftPR, GitHubClient, PullRequest
from machinist.harness import get_harness
from machinist.lifecycle import (
    LifecycleError,
    Phase,
    RunRecord,
    RunStatus,
    TaskClaim,
    TaskLifecycle,
)
from machinist.phases.execute import run_execute_phase
from machinist.phases.review import run_review_phase
from machinist.phases.spec import run_spec_phase
from machinist.process import run_supervised
from machinist.repository_custody import bind_repository
from machinist.workspace import Workspace

_Result = TypeVar("_Result")
SpecRunner = Callable[..., DraftPR]
ExecuteRunner = Callable[..., PullRequest]
ReviewRunner = Callable[..., PullRequest]


class Lifecycle(Protocol):
    """Task Run persistence capability required by dispatch."""

    def run(
        self,
        issue: int,
        phase: Phase,
        action: Callable[[TaskClaim], _Result],
        *,
        repeat_succeeded: bool = False,
    ) -> _Result: ...

    def record(self, issue: int, phase: Phase) -> RunRecord | None: ...


class Cancellation(Protocol):
    """Durable cancellation capability required by dispatch."""

    def check(self, issue: int) -> Callable[[], bool]: ...


class TaskDispatcher:
    """Own dependency construction and lifecycle entry for every Phase run."""

    def __init__(
        self,
        config: MachinistConfig,
        *,
        repo_root: Path,
        lifecycle: Lifecycle | None = None,
        cancellation: Cancellation | None = None,
        github: object | None = None,
        github_factory: Callable[[], object] | None = None,
        harness_factory: Callable[[Phase, int], object] | None = None,
        workspace_factory: Callable[[], object] | None = None,
        progress: Callable[[str], None] | None = None,
        spec_runner: SpecRunner = run_spec_phase,
        execute_runner: ExecuteRunner = run_execute_phase,
        review_runner: ReviewRunner = run_review_phase,
        test_runner: Callable[..., object] = run_supervised,
    ) -> None:
        self.config = config
        self.repo_root = repo_root.resolve()
        self.runs_dir = self.repo_root / ".machinist/runs"
        self.lifecycle = (
            lifecycle
            if lifecycle is not None
            else TaskLifecycle(self.runs_dir, repo_root=self.repo_root)
        )
        self.cancellation = (
            cancellation
            if cancellation is not None
            else CancellationStore(self.runs_dir, repo_root=self.repo_root)
        )
        self._github = github
        self._github_factory = github_factory
        self._harness_factory = harness_factory
        self._workspace_factory = workspace_factory
        self._progress = progress
        self._spec_runner = spec_runner
        self._execute_runner = execute_runner
        self._review_runner = review_runner
        self._test_runner = test_runner

    def run_spec(self, issue: int, *, revise: bool | None = None) -> DraftPR:
        """Enter one claimed Spec Task Run."""
        github = self._github_client()

        def invoke(claim: TaskClaim) -> DraftPR:
            options: dict[str, object] = {}
            if revise is not None:
                options["revise"] = revise
            return self._spec_runner(
                issue,
                self.config,
                github=github,
                harness=self._harness(Phase.SPEC, issue),
                workspace=self._workspace(),
                claim=claim,
                attempt=self._fresh_attempt(claim),
                cancel_check=self.cancellation.check(issue),
                **options,
            )

        return self.lifecycle.run(
            issue,
            Phase.SPEC,
            invoke,
            repeat_succeeded=revise is True,
        )

    def run_execute(
        self,
        issue: int,
        *,
        force: bool | None = None,
        recovery: str = "fresh",
        feedback: str | None = None,
    ) -> PullRequest:
        """Enter one claimed Execute Task Run, including amendment runs."""
        github = self._github_client()

        def invoke(claim: TaskClaim) -> PullRequest:
            options: dict[str, object] = {}
            if force is not None:
                options["force"] = force
            if feedback is not None:
                options["feedback"] = feedback
            return self._execute_runner(
                issue,
                self.config,
                github=github,
                harness=self._harness(Phase.EXECUTE, issue),
                workspace=self._workspace(),
                test_runner=self._test_runner,
                claim=claim,
                recovery=recovery,
                cancel_check=self.cancellation.check(issue),
                **options,
            )

        return self.lifecycle.run(
            issue,
            Phase.EXECUTE,
            invoke,
            repeat_succeeded=force is True,
        )

    def run_review(self, issue: int) -> PullRequest:
        """Enter Review only for the exact successful Execute Evidence."""
        execute = self.lifecycle.record(issue, Phase.EXECUTE)
        if execute is None or execute.status is not RunStatus.SUCCEEDED:
            raise LifecycleError(
                f"issue #{issue} has no successful Execute Task Run to review"
            )
        github = self._github_client()
        return self.lifecycle.run(
            issue,
            Phase.REVIEW,
            lambda claim: self._review_runner(
                issue,
                self.config,
                github=github,
                harness=self._harness(Phase.REVIEW, issue),
                workspace=self._workspace(),
                execute_evidence=dict(execute.evidence),
                claim=claim,
                cancel_check=self.cancellation.check(issue),
            ),
        )

    def run_phase(
        self,
        issue: int,
        phase: Phase,
        *,
        recovery: str = "fresh",
    ) -> DraftPR | PullRequest:
        """Dispatch the Phase selected by an explicit retry-now transition."""
        if phase is Phase.SPEC:
            return self.run_spec(issue)
        if phase is Phase.EXECUTE:
            return self.run_execute(issue, recovery=recovery)
        return self.run_review(issue)

    def _github_client(self) -> object:
        if self._github is not None:
            return self._github
        if self._github_factory is not None:
            return self._github_factory()
        workspace = Workspace(repo_root=self.repo_root, config=self.config.workspace)
        github = GitHubClient()
        bind_repository(self.config, github, workspace)
        return github

    def _harness(self, phase: Phase, issue: int) -> object:
        if self._harness_factory is not None:
            return self._harness_factory(phase, issue)
        harness = get_harness(self.config.harness_for(phase.value))
        if self._progress is not None:
            harness.on_progress = self._progress
        harness.cancel_check = self.cancellation.check(issue)
        return harness

    def _workspace(self) -> object:
        if self._workspace_factory is not None:
            return self._workspace_factory()
        return Workspace(repo_root=self.repo_root, config=self.config.workspace)

    @staticmethod
    def _fresh_attempt(claim: TaskClaim) -> int | None:
        """Keep the first-run path stable and isolate every later attempt."""
        return claim.attempt if claim.attempt > 1 else None
