"""Phase 1: GitHub issue → implementation spec → draft PR.

The harness runs in read-only print mode inside an isolated workspace:
it can explore the codebase for context, but machinist itself writes the
spec file, commits, pushes, and opens the draft PR.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from string import Template
from uuid import uuid4

from machinist.config import MachinistConfig
from machinist.github import DraftPR, Issue

_APPROVED_LABEL_COLOR = "0e8a16"
_SPEC_PROMPT = files("machinist") / "templates" / "spec-prompt.md"
_MAX_ISSUE_TITLE_CHARS = 500


class SpecPhaseError(Exception):
    """Phase 1 could not produce a usable spec."""


def render_spec_prompt(issue: Issue, instructions: str = "") -> str:
    template = Template(_SPEC_PROMPT.read_text())
    prompt = template.safe_substitute(
        number=issue.number,
        title=issue.title,
        body=issue.body or "(no description provided)",
    )
    if instructions:
        prompt += (
            "\n\n## Repository instructions\n\n"
            "--- BEGIN REPOSITORY INSTRUCTIONS ---\n"
            f"{instructions}\n"
            "--- END REPOSITORY INSTRUCTIONS ---\n"
        )
    return prompt


def run_spec_phase(
    issue_number: int,
    config: MachinistConfig,
    *,
    github,
    harness,
    workspace,
    claim=None,
    revise: bool = False,
    attempt: int | None = None,
) -> DraftPR:
    issue = github.get_issue(issue_number)
    _validate_issue(issue, config)
    base = github.default_branch()
    branch = f"{config.workspace.branch_prefix}issue-{issue.number}"
    existing_pr = github.pr_for_branch(branch) if revise else None
    if revise and existing_pr is None:
        raise SpecPhaseError(
            f"no existing Spec PR for issue #{issue.number}; run 'machinist spec {issue.number}' first"
        )
    if existing_pr is not None and existing_pr.state == "MERGED":
        raise SpecPhaseError(
            f"PR #{existing_pr.number} is already merged; its Spec cannot be revised"
        )

    provision_args = (f"issue-{issue.number}", branch, f"origin/{base}")
    path = (
        workspace.provision(*provision_args)
        if attempt is None
        else workspace.provision(*provision_args, attempt=attempt)
    )
    try:
        if claim is not None:
            claim.checkpoint(workspace_path=str(path))
        spec_text = _generate_spec(issue, config, harness, workspace, path)
        if workspace.has_changes(path):
            raise SpecPhaseError(
                f"{harness.name} changed repository files during read-only spec generation"
            )

        spec_file = path / ".machinist" / "specs" / f"issue-{issue.number}-spec.md"
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(spec_text)

        action = "revise" if revise else "add"
        workspace.commit_all(
            path,
            f"docs(spec): {action} implementation spec for issue #{issue.number}",
        )
        spec_sha = workspace.head_sha(path)
        if claim is not None:
            claim.checkpoint(spec_sha=spec_sha, push_intended_sha=spec_sha)
        workspace.push(
            path,
            branch,
            expected_sha=existing_pr.head_sha if existing_pr is not None else None,
        )
        if claim is not None:
            claim.checkpoint(push_observed_sha=spec_sha)

        github.ensure_label(
            config.github.labels.approved,
            color=_APPROVED_LABEL_COLOR,
            description="Machinist: spec approved for implementation",
        )
        title = f"Spec: {issue.title} (#{issue.number})"
        body = _pr_body(issue, config)
        if existing_pr is None:
            pr = github.create_draft_pr(
                branch=branch,
                base=base,
                title=title,
                body=body,
            )
        else:
            if config.github.labels.approved in existing_pr.labels:
                github.remove_pr_label(
                    existing_pr.number, config.github.labels.approved
                )
            if existing_pr.state == "CLOSED":
                github.reopen_pr(existing_pr.number)
            if not existing_pr.is_draft and hasattr(github, "mark_draft"):
                github.mark_draft(existing_pr.number)
            github.update_pr(existing_pr.number, title=title, body=body)
            pr = DraftPR(number=existing_pr.number, url=existing_pr.url)
    except Exception:
        workspace.cleanup(path, success=False)
        raise
    workspace.cleanup(path, success=True)
    return pr


def preview_spec_phase(
    issue_number: int,
    config: MachinistConfig,
    *,
    github,
    harness,
    workspace,
) -> str:
    """Generate a Spec preview without writing, committing, pushing, or opening a PR."""
    issue = github.get_issue(issue_number)
    _validate_issue(issue, config)
    base = github.default_branch()
    branch = f"{config.workspace.branch_prefix}issue-{issue.number}"
    preview_task = f"preview-issue-{issue.number}-{uuid4().hex[:12]}"
    path = workspace.provision(preview_task, branch, f"origin/{base}")
    try:
        spec_text = _generate_spec(issue, config, harness, workspace, path)
        if workspace.has_changes(path):
            raise SpecPhaseError(
                f"{harness.name} changed repository files during read-only spec preview"
            )
    except Exception:
        workspace.cleanup(path, success=False)
        raise
    workspace.cleanup(path, success=True)
    return spec_text


def _validate_issue(issue: Issue, config: MachinistConfig) -> None:
    if len(issue.title) > _MAX_ISSUE_TITLE_CHARS:
        raise SpecPhaseError(
            f"issue #{issue.number} title is too large ({len(issue.title)} characters; "
            f"maximum {_MAX_ISSUE_TITLE_CHARS})"
        )
    if len(issue.body) > config.limits.max_issue_body_chars:
        raise SpecPhaseError(
            f"issue #{issue.number} body is too large ({len(issue.body)} characters; "
            f"maximum {config.limits.max_issue_body_chars})"
        )


def _generate_spec(
    issue: Issue, config: MachinistConfig, harness, workspace, path
) -> str:
    repo_root = getattr(workspace, "repo_root", Path.cwd())
    instructions = config.resolve_instructions("spec", repo_root)
    spec_text = harness.generate_spec(
        render_spec_prompt(issue, instructions),
        cwd=path,
    )
    if not spec_text.strip():
        raise SpecPhaseError(
            f"{harness.name} returned an empty spec for issue #{issue.number}"
        )
    if len(spec_text) > config.limits.max_spec_chars:
        raise SpecPhaseError(
            f"{harness.name} spec is too large ({len(spec_text)} characters; "
            f"maximum {config.limits.max_spec_chars})"
        )
    return spec_text


def _pr_body(issue: Issue, config: MachinistConfig) -> str:
    approved = config.github.labels.approved
    return (
        f"AgentMachinist generated an implementation spec for #{issue.number}:\n"
        f"`.machinist/specs/issue-{issue.number}-spec.md` (see Files changed).\n"
        "\n"
        f"**To approve:** apply the `{approved}` label, or comment `/machinist-execute`.\n"
        "(GitHub's review **Approve button** is *not* the mechanism — GitHub blocks it\n"
        "on your own PRs, and machinist only watches the label.)\n"
        "Once approved, the machinist daemon implements the spec on this branch,\n"
        "runs the test gate, and marks this PR ready for review.\n"
        "Please leave this PR as a draft — machinist flips it to ready itself\n"
        "when the implementation lands; marking it ready early pauses the daemon.\n"
        "\n"
        f"Closes #{issue.number}"
    )
