"""Exercise production local Phases in a disposable, real Git repository."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from machinist.config import MachinistConfig
from machinist.lifecycle import Phase, RunStatus
from machinist.process import credential_reduced_environment
from machinist.workspace import Workspace

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


def simulate_rehearsal(
    *, review_enabled: bool, temp_parent: Path | None = None
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
    )


def run_harness_rehearsal(
    config: MachinistConfig,
    *,
    harness_factory: Callable[[str], Any],
    temp_parent: Path | None = None,
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
    )


def _run_rehearsal(
    config: MachinistConfig,
    *,
    harness_factory: Callable[[str], Any],
    harness_used: bool,
    temp_parent: Path | None,
) -> RehearsalResult:
    from machinist.local_workflow import LocalWorkflow

    path = Path(
        tempfile.mkdtemp(
            prefix="agentmachinist-rehearsal-",
            dir=str(temp_parent) if temp_parent is not None else None,
        )
    ).resolve()
    repository = path / "repository"
    repository.mkdir()
    transitions = ["local Task intake"]
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
        transitions.append("spec ready")
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
        transitions.extend(["approval recorded", "execute verified"])
        if (
            not task.review_report
            or task.review_report.get("reviewed_sha") != task.candidate_sha
        ):
            raise ValueError("production Review did not cover the candidate")
        transitions.append("review complete")
        if not workflow.store.read_report(task.id):
            raise ValueError("production workflow did not save a local report")
        # This invocation authorizes integration of the disposable fixture.
        # Real Tasks still wait for the explicit human integration command.
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
        transitions.append("local integration complete")
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
