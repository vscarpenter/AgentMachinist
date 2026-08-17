"""Phase 3: approved spec → implementation → PR ready for review.

Runs only against a PR carrying the approval label. The harness gets
edit permissions here (unlike Phase 1), but still no git access:
machinist owns the commit, the push, and the draft→ready flip.
"""

from __future__ import annotations

import subprocess
from importlib.resources import files
from string import Template

from machinist.config import MachinistConfig
from machinist.github import PullRequest

_IMPLEMENT_PROMPT = files("machinist") / "templates" / "implement-prompt.md"


class ExecutePhaseError(Exception):
    """Phase 3 refused to run or failed to produce a shippable change."""


def render_implement_prompt(issue_number: int, spec_text: str) -> str:
    template = Template(_IMPLEMENT_PROMPT.read_text())
    return template.safe_substitute(number=issue_number, spec=spec_text)


def run_execute_phase(
    issue_number: int,
    config: MachinistConfig,
    *,
    github,
    harness,
    workspace,
    test_runner=subprocess.run,
) -> PullRequest:
    branch = f"{config.workspace.branch_prefix}issue-{issue_number}"
    pr = next(
        (p for p in github.open_machinist_prs(config.workspace.branch_prefix) if p.branch == branch),
        None,
    )
    if pr is None:
        raise ExecutePhaseError(
            f"no open PR for branch '{branch}'; run 'machinist spec {issue_number}' first"
        )
    approved = config.github.labels.approved
    if approved not in pr.labels:
        raise ExecutePhaseError(
            f"PR #{pr.number} is not approved; apply the '{approved}' label "
            "(or comment /machinist-execute on it) first"
        )

    base = github.default_branch()
    path = workspace.provision(f"issue-{issue_number}", branch, f"origin/{base}")
    try:
        spec_file = path / ".machinist" / "specs" / f"issue-{issue_number}-spec.md"
        if not spec_file.exists():
            raise ExecutePhaseError(
                f"spec file .machinist/specs/{spec_file.name} not found on branch '{branch}'"
            )

        harness.implement(render_implement_prompt(issue_number, spec_file.read_text()), cwd=path)
        if not workspace.has_changes(path):
            raise ExecutePhaseError(f"{harness.name} made no changes for issue #{issue_number}")

        if config.tests.command:
            result = test_runner(
                config.tests.command,
                shell=True,
                cwd=path,
                capture_output=True,
                text=True,
                timeout=config.harness.timeout_minutes * 60,
            )
            if result.returncode != 0:
                output = (result.stdout + result.stderr).strip()
                raise ExecutePhaseError(
                    f"test gate '{config.tests.command}' failed (workspace kept at {path}):\n"
                    f"{output[-2000:]}"
                )

        workspace.commit_all(path, f"feat(agent): implement issue #{issue_number} per approved spec")
        workspace.push(path, branch)
        github.mark_ready(pr.number)
    except Exception:
        workspace.cleanup(path, success=False)
        raise
    workspace.cleanup(path, success=True)
    return pr
