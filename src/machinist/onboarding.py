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
    pr: DraftPR | None


def deliver_setup_pr(
    repo_root: Path,
    *,
    github,
    initialize: Callable[[], None],
    validate: Callable[[], None] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    branch: str = _SETUP_BRANCH,
) -> SetupPRResult:
    """Initialize or resume bounded setup changes and reuse their draft PR."""
    root = Path(repo_root).resolve()
    base = github.default_branch()
    current = _git(root, runner, "branch", "--show-current").strip()
    if current not in {base, branch}:
        raise OnboardingError(
            f"setup PR must start from default branch '{base}' or resume '{branch}', "
            f"not '{current}'; switch branches and rerun 'machinist onboard --setup-pr'"
        )
    if current == base:
        _require_clean(root, runner)
        if _branch_exists(root, runner, branch):
            _require_setup_history(root, runner, base, branch)
            _git(root, runner, "switch", branch)
        else:
            _git(root, runner, "switch", "-c", branch)
    else:
        _require_setup_history(root, runner, base, branch)
        _require_allowed_changes(_setup_changes(root, runner))

    existing_pr = github.pr_for_branch(branch)
    if existing_pr is not None and (
        existing_pr.state != "OPEN"
        or not existing_pr.is_draft
        or existing_pr.branch != branch
        or existing_pr.base != base
    ):
        raise OnboardingError(
            f"setup PR #{existing_pr.number} is no longer an open draft targeting "
            f"'{base}'; inspect it before changing its branch"
        )
    initialize()
    changed = _setup_changes(root, runner)
    _require_allowed_changes(changed)
    if validate is not None:
        validate()
    if changed:
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
    delivered = _require_setup_history(root, runner, base, branch)
    if not delivered and existing_pr is None:
        if current == base:
            _git(root, runner, "switch", base)
        return SetupPRResult(branch=base, base=base, commit_sha=commit_sha, pr=None)
    _git(root, runner, "push", "--set-upstream", "origin", branch)
    pr = (
        DraftPR(number=existing_pr.number, url=existing_pr.url)
        if existing_pr is not None
        else github.create_draft_pr(
            branch=branch,
            base=base,
            title="Adopt AgentMachinist",
            body=_setup_pr_body(delivered, commit_sha),
        )
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


def _branch_exists(root: Path, runner: Callable[..., Any], branch: str) -> bool:
    result = _run_git(root, runner, "show-ref", "--verify", f"refs/heads/{branch}")
    if result.returncode == 0:
        return True
    if result.returncode not in {1, 128}:
        raise OnboardingError(_git_failure(result, "show-ref"))
    return False


def _require_setup_history(
    root: Path, runner: Callable[..., Any], base: str, branch: str
) -> tuple[str, ...]:
    # A final diff cannot reveal unrelated content added and then reverted.
    # Publishing a branch publishes those commits too, so check every one.
    commits = _git(root, runner, "rev-list", f"{base}..{branch}", "--").splitlines()
    for commit in commits:
        committed_paths = tuple(
            path
            for path in _git(
                root,
                runner,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "--root",
                "-r",
                "-m",
                "-z",
                commit,
                "--",
            ).split("\0")
            if path
        )
        _require_allowed_changes(committed_paths)
    paths = tuple(
        path
        for path in _git(
            root, runner, "diff", "--name-only", "-z", f"{base}...{branch}", "--"
        ).split("\0")
        if path
    )
    _require_allowed_changes(paths)
    return paths


def _require_allowed_changes(paths: tuple[str, ...]) -> None:
    unmanaged = [path for path in paths if not _allowed_setup_path(path)]
    if unmanaged:
        raise OnboardingError(
            "setup changed paths outside the setup allowlist: "
            + ", ".join(unmanaged)
            + "; preserve this work separately before rerunning 'machinist onboard --setup-pr'"
        )


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
        "### Next\n\nReview the generated configuration and workflows, then use "
        "the repository's normal human review and merge process. After this PR "
        "is merged, run `machinist doctor --run-gates` to verify deployed "
        "workflows and execution readiness."
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
