"""Transactional setup-branch delivery for guided first-run adoption."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from machinist.github import DraftPR

_SETUP_BRANCH = "chore/agentmachinist-setup"
_ALLOWED_EXACT = frozenset(
    {
        "machinist.yaml",
        ".gitignore",
        ".machinist/specs/.gitkeep",
        ".github/ISSUE_TEMPLATE/agentmachinist-task.yml",
    }
)


class OnboardingError(Exception):
    """Guided setup could not produce a bounded draft pull request."""


@dataclass(frozen=True)
class SetupPRResult:
    branch: str
    base: str
    commit_sha: str
    pr: DraftPR


def deliver_setup_pr(
    repo_root: Path,
    *,
    github,
    initialize: Callable[[], None],
    runner: Callable[..., Any] = subprocess.run,
    branch: str = _SETUP_BRANCH,
) -> SetupPRResult:
    """Initialize on a clean setup branch, push it, and open a draft PR."""
    root = Path(repo_root).resolve()
    _require_clean(root, runner)
    base = github.default_branch()
    current = _git(root, runner, "branch", "--show-current").strip()
    if current != base:
        raise OnboardingError(
            f"setup PR must start from default branch '{base}', not '{current}'"
        )
    _require_new_branch(root, runner, branch)
    _git(root, runner, "switch", "-c", branch)
    initialize()
    changed = _setup_changes(root, runner)
    if not changed:
        raise OnboardingError("setup generated no repository changes")
    unmanaged = [path for path in changed if not _allowed_setup_path(path)]
    if unmanaged:
        raise OnboardingError(
            "setup changed paths outside the setup allowlist: "
            + ", ".join(unmanaged)
            + "; changes remain visible on the setup branch"
        )
    _git(root, runner, "add", "--", *changed)
    _git(
        root,
        runner,
        "-c",
        "user.name=AgentMachinist",
        "-c",
        "user.email=agentmachinist@users.noreply.github.com",
        "commit",
        "-m",
        "chore: adopt AgentMachinist",
    )
    commit_sha = _git(root, runner, "rev-parse", "HEAD").strip()
    _git(root, runner, "push", "--set-upstream", "origin", branch)
    pr = github.create_draft_pr(
        branch=branch,
        base=base,
        title="Adopt AgentMachinist",
        body=_setup_pr_body(changed, commit_sha),
    )
    return SetupPRResult(branch=branch, base=base, commit_sha=commit_sha, pr=pr)


def _require_clean(root: Path, runner: Callable[..., Any]) -> None:
    status = _git(root, runner, "status", "--porcelain=v1", "--untracked-files=all")
    if status.strip():
        paths = [line[3:] for line in status.splitlines() if len(line) > 3]
        detail = ", ".join(paths[:10]) or "uncommitted changes"
        raise OnboardingError(
            f"setup PR requires a clean worktree; commit or stash: {detail}"
        )


def _require_new_branch(root: Path, runner: Callable[..., Any], branch: str) -> None:
    result = _run_git(root, runner, "show-ref", "--verify", f"refs/heads/{branch}")
    if result.returncode == 0:
        raise OnboardingError(
            f"setup branch '{branch}' already exists; inspect or rename it first"
        )
    if result.returncode not in {1, 128}:
        raise OnboardingError(_git_failure(result, "show-ref"))


def _setup_changes(root: Path, runner: Callable[..., Any]) -> tuple[str, ...]:
    output = _git(
        root,
        runner,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    records = [record for record in output.split("\0") if record]
    paths = tuple(sorted({record[3:] for record in records if len(record) > 3}))
    if len(paths) != len(records):
        raise OnboardingError("setup produced a rename or unreadable Git status")
    return paths


def _allowed_setup_path(value: str) -> bool:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        return False
    if value in _ALLOWED_EXACT:
        return True
    return value.startswith(".github/workflows/machinist-") and value.endswith(".yml")


def _setup_pr_body(changed: tuple[str, ...], sha: str) -> str:
    listing = "\n".join(f"- `{path}`" for path in changed)
    return (
        "## AgentMachinist setup\n\n"
        "This draft contains generated adoption files for review. It does not "
        "merge itself or change branch protection.\n\n"
        f"Commit: `{sha}`\n\n### Generated files\n\n{listing}\n\n"
        "### Next\n\nRun `machinist doctor --run-gates`, review the generated "
        "configuration and workflows, then use the repository's normal human "
        "review and merge process."
    )


def _git(root: Path, runner: Callable[..., Any], *args: str) -> str:
    result = _run_git(root, runner, *args)
    if result.returncode != 0:
        raise OnboardingError(_git_failure(result, args[0]))
    return result.stdout or ""


def _run_git(root: Path, runner: Callable[..., Any], *args: str):
    try:
        return runner(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OnboardingError(f"could not run git {args[0]}: {exc}") from exc


def _git_failure(result, command: str) -> str:
    detail = (result.stderr or result.stdout or "no diagnostic output").strip()
    return f"git {command} failed: {detail}"
