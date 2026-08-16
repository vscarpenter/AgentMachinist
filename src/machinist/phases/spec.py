"""Phase 1: GitHub issue → implementation spec → draft PR.

The harness runs in read-only print mode inside an isolated workspace:
it can explore the codebase for context, but machinist itself writes the
spec file, commits, pushes, and opens the draft PR.
"""

from __future__ import annotations

from importlib.resources import files
from string import Template

from machinist.config import MachinistConfig
from machinist.github import DraftPR, Issue

_APPROVED_LABEL_COLOR = "0e8a16"
_SPEC_PROMPT = files("machinist") / "templates" / "spec-prompt.md"


class SpecPhaseError(Exception):
    """Phase 1 could not produce a usable spec."""


def render_spec_prompt(issue: Issue) -> str:
    template = Template(_SPEC_PROMPT.read_text())
    return template.safe_substitute(
        number=issue.number,
        title=issue.title,
        body=issue.body or "(no description provided)",
    )


def run_spec_phase(issue_number: int, config: MachinistConfig, *, github, harness, workspace) -> DraftPR:
    issue = github.get_issue(issue_number)
    base = github.default_branch()
    branch = f"{config.workspace.branch_prefix}issue-{issue.number}"

    path = workspace.provision(f"issue-{issue.number}", branch, f"origin/{base}")
    try:
        spec_text = harness.generate_spec(render_spec_prompt(issue), cwd=path)
        if not spec_text.strip():
            raise SpecPhaseError(f"{harness.name} returned an empty spec for issue #{issue.number}")

        spec_file = path / ".machinist" / "specs" / f"issue-{issue.number}-spec.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(spec_text)

        workspace.commit_all(path, f"docs(spec): add implementation spec for issue #{issue.number}")
        workspace.push(path, branch)

        github.ensure_label(
            config.github.labels.approved,
            color=_APPROVED_LABEL_COLOR,
            description="Machinist: spec approved for implementation",
        )
        pr = github.create_draft_pr(
            branch=branch,
            base=base,
            title=f"Spec: {issue.title} (#{issue.number})",
            body=_pr_body(issue, config),
        )
    except Exception:
        workspace.cleanup(path, success=False)
        raise
    workspace.cleanup(path, success=True)
    return pr


def _pr_body(issue: Issue, config: MachinistConfig) -> str:
    approved = config.github.labels.approved
    return (
        f"AgentMachinist generated an implementation spec for #{issue.number}:\n"
        f"`.machinist/specs/issue-{issue.number}-spec.md` (see Files changed).\n"
        "\n"
        f"**To approve:** apply the `{approved}` label, or comment `/machinist-execute`.\n"
        "Once approved, the machinist daemon implements the spec on this branch,\n"
        "runs the test gate, and marks this PR ready for review.\n"
        "\n"
        f"Closes #{issue.number}"
    )
