"""Exercise production local Phases in a disposable, real Git repository."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from machinist.config import MachinistConfig
from machinist.lifecycle import Phase, RunStatus
from machinist.process import credential_reduced_environment
from machinist.workspace import Workspace

if TYPE_CHECKING:
    from machinist.local_tasks import LocalTask
    from machinist.local_workflow import LocalWorkflow

_TASK_TITLE = "Return two from answer() and update its regression test"
_TASK_BODY = """## Objective
In feature.py, change answer() to return the integer 2 instead of 1.

## Acceptance criteria
- answer() returns 2.
- tests/test_feature.py asserts that answer() returns 2.
- The standard-library unittest suite passes.

## Constraints
Keep the existing regression test. Do not add dependencies or network calls.
"""


class RehearsalError(Exception):
    """A disposable rehearsal failed and was retained for diagnosis."""

    def __init__(self, message: str, workspace: Path):
        self.workspace = workspace
        super().__init__(f"{message}; rehearsal retained at {workspace}")


@dataclass(frozen=True)
class RehearsalResult:
    transitions: tuple[str, ...]
    harness_used: bool
    workspace: Path | None = None
    task_id: str | None = None
    spec_sha: str | None = None
    candidate_sha: str | None = None
    integrated_sha: str | None = None
    stopped_at: str | None = None


@dataclass(frozen=True)
class RehearsalCheckpoint:
    """Exact saved artifacts available while the human decision callback waits."""

    stage: Literal["spec", "acceptance"]
    prompt: str
    workspace: Path
    repository: Path
    task_id: str
    spec_sha: str
    spec: str
    spec_path: Path
    harness_used: bool
    candidate_sha: str | None = None
    diff: str = ""
    diff_path: Path | None = None
    candidate_path: Path | None = None
    verification_report: dict[str, Any] | None = None
    review_report: dict[str, Any] | None = None
    report_path: Path | None = None


def run_local_rehearsal(
    *,
    guided: bool = False,
    confirm: Callable[[RehearsalCheckpoint], bool] | None = None,
    temp_parent: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> RehearsalResult:
    """Exercise the packaged fixture for free, optionally waiting for two decisions.

    ``confirm`` receives the exact Spec before Approval, then the verified and
    reviewed candidate before disposable integration. The caller renders those
    artifacts and waits for the operator's decision. Declining retains them and
    returns ``stopped_at``; no provider or forge is needed in either mode.
    """
    return _run_rehearsal(
        MachinistConfig(),
        harness_factory=lambda phase: _FakeHarness(),
        harness_used=False,
        temp_parent=temp_parent,
        progress=progress,
        guided=guided,
        confirm=confirm,
    )


def simulate_rehearsal(
    *,
    review_enabled: bool,
    temp_parent: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> RehearsalResult:
    """Run the real controller with a deterministic fake Harness, without a model.

    The historical name is retained for callers; this is no longer a static list
    of successful states. Git, Claims, Approval, verification, Review and local
    integration all follow the same path as a real foreground Task.
    ``review_enabled`` is accepted for compatibility; guided local Review is
    always required, independently of the legacy GitHub Review setting.
    """
    config = MachinistConfig.model_validate({"review": {"enabled": review_enabled}})
    return _run_rehearsal(
        config,
        harness_factory=lambda phase: _FakeHarness(),
        harness_used=False,
        temp_parent=temp_parent,
        progress=progress,
    )


def run_harness_rehearsal(
    config: MachinistConfig,
    *,
    harness_factory: Callable[[str], Any],
    temp_parent: Path | None = None,
    progress: Callable[[str], None] | None = None,
    guided: bool = False,
    confirm: Callable[[RehearsalCheckpoint], bool] | None = None,
) -> RehearsalResult:
    """Explicit opt-in: exercise configured Harnesses through the same local path.

    The disposable application has its own meaningful verification command;
    unrelated repository gates, instruction files, notifications and telemetry
    are deliberately not replayed inside the fixture.
    """
    return _run_rehearsal(
        config,
        harness_factory=harness_factory,
        harness_used=True,
        temp_parent=temp_parent,
        progress=progress,
        guided=guided,
        confirm=confirm,
    )


def _run_rehearsal(
    config: MachinistConfig,
    *,
    harness_factory: Callable[[str], Any],
    harness_used: bool,
    temp_parent: Path | None,
    progress: Callable[[str], None] | None = None,
    guided: bool = False,
    confirm: Callable[[RehearsalCheckpoint], bool] | None = None,
) -> RehearsalResult:
    from machinist.local_workflow import LocalWorkflow

    if guided and confirm is None:
        raise ValueError("guided rehearsal requires a confirmation callback")
    path = Path(
        tempfile.mkdtemp(
            prefix="agentmachinist-rehearsal-",
            dir=str(temp_parent) if temp_parent is not None else None,
        )
    ).resolve()
    repository = path / "repository"
    repository.mkdir()
    transitions: list[str] = []

    def reached(*names: str) -> None:
        for name in names:
            transitions.append(name)
            if progress is not None:
                progress(name)

    reached("local Task intake")
    try:
        _initialize_repo(repository)
        command = shlex.join(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        )
        _check_baseline(repository)
        local_config = MachinistConfig.model_validate(
            {
                "harness": config.harness.model_dump(),
                "workspace": {
                    "root": str(path / "workshops"),
                    "strategy": config.workspace.strategy,
                },
                "tests": {"command": command},
                "review": {"enabled": True},
                "notifications": {"backend": "disabled"},
            }
        )
        workflow = LocalWorkflow(
            local_config,
            repo_root=repository,
            harness_factory=lambda phase, number: harness_factory(phase.value),
        )
        task = workflow.start(_TASK_TITLE, _TASK_BODY)
        if not task.spec_sha:
            raise ValueError("production Spec did not record its exact commit")
        spec_sha = task.spec_sha
        reached("spec ready")
        if guided:
            checkpoint = _inspection_checkpoint(
                "spec", workflow, task, path, harness_used=harness_used
            )
            assert confirm is not None
            if not confirm(checkpoint):
                return RehearsalResult(
                    tuple(transitions),
                    harness_used=harness_used,
                    workspace=path,
                    task_id=task.id,
                    spec_sha=spec_sha,
                    stopped_at=checkpoint.stage,
                )
        task = workflow.approve(
            task.id, expected_sha=spec_sha, actor="rehearsal operator"
        )
        for phase in (Phase.SPEC, Phase.EXECUTE, Phase.REVIEW):
            record = workflow.lifecycle.record(task.number, phase)
            if record is None or record.status is not RunStatus.SUCCEEDED:
                raise ValueError(f"production {phase.value} did not record success")
        if not task.candidate_sha or task.candidate_sha == spec_sha:
            raise ValueError("production Execute did not deliver an implementation")
        if not task.approval:
            raise ValueError("production workflow did not record human Approval")
        reached("approval recorded", "execute verified")
        if (
            not task.review_report
            or task.review_report.get("reviewed_sha") != task.candidate_sha
        ):
            raise ValueError("production Review did not cover the candidate")
        reached("review complete")
        if not workflow.store.read_report(task.id):
            raise ValueError("production workflow did not save a local report")
        if guided:
            checkpoint = _inspection_checkpoint(
                "acceptance", workflow, task, path, harness_used=harness_used
            )
            assert confirm is not None
            if not confirm(checkpoint):
                return RehearsalResult(
                    tuple(transitions),
                    harness_used=harness_used,
                    workspace=path,
                    task_id=task.id,
                    spec_sha=spec_sha,
                    candidate_sha=task.candidate_sha,
                    stopped_at=checkpoint.stage,
                )
        # Automatic rehearsal authorizes the disposable integration up front;
        # guided rehearsal waits for the separate acceptance decision above.
        integrated = workflow.integrate(task.id)
        observed = _git(repository, "rev-parse", "HEAD").strip()
        if (
            not integrated.integration
            or integrated.integration.get("observed_sha") != task.candidate_sha
            or observed != task.candidate_sha
            or _git(repository, "status", "--porcelain").strip()
        ):
            raise ValueError(
                "local integration did not reach the exact clean candidate"
            )
        if _git(repository, "remote").strip():
            raise ValueError("rehearsal unexpectedly acquired a remote")
        reached("local integration complete")
        result = RehearsalResult(
            tuple(transitions),
            harness_used=harness_used,
            task_id=task.id,
            spec_sha=spec_sha,
            candidate_sha=task.candidate_sha,
            integrated_sha=observed,
        )
    except Exception as exc:
        raise RehearsalError(str(exc), path) from exc
    shutil.rmtree(path)
    return result


def _inspection_checkpoint(
    stage: Literal["spec", "acceptance"],
    workflow: LocalWorkflow,
    task: LocalTask,
    path: Path,
    *,
    harness_used: bool,
) -> RehearsalCheckpoint:
    assert task.spec_sha is not None
    spec = workflow.workspace.read_at_commit(
        task.spec_sha,
        f".machinist/specs/task-{task.number}-spec.md",
        max_bytes=workflow.config.limits.max_spec_chars * 4,
    )
    inspection = path / "inspection"
    inspection.mkdir(exist_ok=True)
    spec_file = inspection / "spec.md"
    spec_file.write_text(spec)
    checkpoint = RehearsalCheckpoint(
        stage=stage,
        prompt="Approve this exact Spec to implement the disposable Task?",
        workspace=path,
        repository=workflow.repo_root,
        task_id=task.id,
        spec_sha=task.spec_sha,
        spec=spec,
        spec_path=spec_file,
        harness_used=harness_used,
    )
    if stage == "spec":
        return checkpoint
    assert task.candidate_sha is not None
    execute = workflow.lifecycle.record(task.number, Phase.EXECUTE)
    assert execute is not None
    verification_report = execute.evidence.get("verification_report")
    if (
        not isinstance(verification_report, dict)
        or verification_report.get("success") is not True
    ):
        raise ValueError("production Execute did not save successful Verification")
    diff = _git(
        workflow.repo_root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        task.spec_sha,
        task.candidate_sha,
        "--",
    )
    diff_file = inspection / "candidate.diff"
    diff_file.write_text(diff)
    candidate = inspection / "candidate"
    for relative in ("feature.py", "tests/test_feature.py"):
        saved = candidate / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(
            workflow.workspace.read_at_commit(
                task.candidate_sha, relative, max_bytes=1024 * 1024
            )
        )
    return replace(
        checkpoint,
        prompt="Accept this reviewed change into the disposable repository?",
        candidate_sha=task.candidate_sha,
        diff=diff,
        diff_path=diff_file,
        candidate_path=candidate,
        verification_report=verification_report,
        review_report=task.review_report,
        report_path=workflow.store.report_path(task),
    )


class _FakeHarness:
    """Only model behavior is simulated; all controller work remains real."""

    name = "rehearsal-fake"
    config = MachinistConfig().harness

    def generate_spec(self, prompt: str, cwd: Path) -> str:
        return (
            "# Rehearsal Spec\n\nChange answer() in feature.py to return 2. "
            "Update tests/test_feature.py so its existing test asserts 2. "
            "Run the configured unittest gate. Do not add dependencies.\n"
        )

    def implement(self, prompt: str, cwd: Path) -> str:
        _write_feature(cwd, 2)
        return "Changed answer() and its regression test to 2."

    def review(self, prompt: str, cwd: Path) -> str:
        if "return 2" not in (cwd / "feature.py").read_text():
            raise ValueError("rehearsal candidate does not implement the Spec")
        return json.dumps(
            {
                "version": 1,
                "summary": "Candidate matches the rehearsal Spec",
                "findings": [],
            }
        )


def _initialize_repo(path: Path) -> None:
    _git(path, "-c", "init.templateDir=", "init", "-q", "-b", "main")
    for name, value in (
        ("user.name", "AgentMachinist rehearsal"),
        ("user.email", "agentmachinist@localhost"),
        ("commit.gpgsign", "false"),
        ("maintenance.auto", "false"),
        ("core.hooksPath", str(path / ".git/hooks")),
    ):
        _git(path, "config", name, value)
    (path / ".gitignore").write_text("__pycache__/\n/.machinist/runs/\n")
    (path / "README.md").write_text("# Disposable AgentMachinist rehearsal\n")
    (path / "tests").mkdir()
    _write_feature(path, 1)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "rehearsal baseline")


def _write_feature(path: Path, answer: int) -> None:
    (path / "feature.py").write_text(f"def answer():\n    return {answer}\n")
    (path / "tests/test_feature.py").write_text(
        "import unittest\nfrom feature import answer\n\n"
        "class TestFeature(unittest.TestCase):\n"
        f"    def test_answer(self): self.assertEqual(answer(), {answer})\n"
    )


def _check_baseline(path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=30,
        env=credential_reduced_environment(),
    )
    if result.returncode != 0:
        raise ValueError("rehearsal baseline test failed: " + result.stderr.strip())


def _git(path: Path, *args: str) -> str:
    # Fixture setup has the same Git environment and hook protection as Phases.
    workspace = Workspace(path, MachinistConfig().workspace)
    return workspace._git(path, *args)
