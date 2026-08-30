"""Disposable local pipeline rehearsal with an explicit Harness opt-in."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from machinist.config import MachinistConfig
from machinist.phases.review import parse_review_report


class RehearsalError(Exception):
    """A disposable Harness rehearsal failed and was retained for diagnosis."""

    def __init__(self, message: str, workspace: Path):
        self.workspace = workspace
        super().__init__(f"{message}; rehearsal retained at {workspace}")


@dataclass(frozen=True)
class RehearsalResult:
    transitions: tuple[str, ...]
    harness_used: bool
    workspace: Path | None = None


def simulate_rehearsal(*, review_enabled: bool) -> RehearsalResult:
    """Return the no-network, no-model controller lifecycle simulation."""
    states = ["issue intake", "spec ready", "approval recorded", "execute verified"]
    if review_enabled:
        states.append("review complete")
    states.append("human merge pending")
    return RehearsalResult(tuple(states), harness_used=False)


def run_harness_rehearsal(
    config: MachinistConfig,
    *,
    harness_factory: Callable[[str], Any],
    temp_parent: Path | None = None,
) -> RehearsalResult:
    """Run enabled Harness phases in a disposable Git repository."""
    path = Path(
        tempfile.mkdtemp(
            prefix="agentmachinist-rehearsal-",
            dir=str(temp_parent) if temp_parent is not None else None,
        )
    )
    transitions = ["issue intake"]
    try:
        _initialize_repo(path)
        spec = _run_spec(path, harness_factory("spec"))
        transitions.extend(["spec ready", "approval recorded"])
        _run_execute(path, harness_factory("execute"), spec)
        transitions.append("execute verified")
        if config.review.enabled:
            _run_review(path, harness_factory("review"), spec)
            transitions.append("review complete")
        transitions.append("human merge pending")
    except Exception as exc:
        if isinstance(exc, RehearsalError):
            raise
        raise RehearsalError(str(exc), path) from exc
    shutil.rmtree(path)
    return RehearsalResult(tuple(transitions), harness_used=True)


def _initialize_repo(path: Path) -> None:
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("# AgentMachinist rehearsal\n")
    _git(path, "add", "README.md")
    _git(
        path,
        "-c",
        "user.name=AgentMachinist",
        "-c",
        "user.email=agentmachinist@users.noreply.github.com",
        "commit",
        "-q",
        "-m",
        "rehearsal baseline",
    )


def _run_spec(path: Path, harness) -> str:
    spec = harness.generate_spec(
        "Write a short implementation spec for creating rehearsal.txt with a "
        "single line saying 'implemented'. Return Markdown only.",
        path,
    )
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("Spec Harness returned no usable Markdown")
    target = path / ".machinist/specs/issue-1-spec.md"
    target.parent.mkdir(parents=True)
    target.write_text(spec)
    _git(path, "add", str(target.relative_to(path)))
    _git(
        path,
        "-c",
        "user.name=AgentMachinist",
        "-c",
        "user.email=agentmachinist@users.noreply.github.com",
        "commit",
        "-q",
        "-m",
        "rehearsal spec",
    )
    return spec


def _run_execute(path: Path, harness, spec: str) -> None:
    harness.implement(
        "Implement this disposable rehearsal spec. Do not commit.\n\n" + spec,
        path,
    )
    changed = _git(path, "status", "--porcelain=v1", "--untracked-files=all")
    if not changed.strip():
        raise ValueError("Execute Harness made no rehearsal changes")


def _run_review(path: Path, harness, spec: str) -> None:
    before = _git(path, "status", "--porcelain=v1", "--untracked-files=all")
    output = harness.review(
        "Review the disposable implementation read-only against this Spec. "
        "Return version-1 Review JSON only.\n\n" + spec,
        path,
    )
    after = _git(path, "status", "--porcelain=v1", "--untracked-files=all")
    if after != before:
        raise ValueError("Review Harness modified the rehearsal repository")
    parse_review_report(output)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic output").strip()
        raise RuntimeError(f"git {args[0]} failed: {detail}")
    return result.stdout
