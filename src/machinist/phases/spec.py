"""Phase 1: GitHub issue → implementation spec → draft PR.

The harness runs in read-only print mode inside an isolated workspace:
it can explore the codebase for context, but machinist itself writes the
spec file, commits, pushes, and opens the draft PR.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from string import Template
from uuid import uuid4

from machinist.config import MachinistConfig
from machinist.evidence import EvidenceError, TaskEvidence
from machinist.github import (
    APPROVED_LABEL_COLOR,
    APPROVED_LABEL_DESCRIPTION,
    DraftPR,
    Issue,
    PullRequest,
)
from machinist.harness import harness_evidence
from machinist.managed_paths import ManagedPathError, write_managed_text
from machinist.phases.progress import bind_harness_progress, report_progress
from machinist.phases.workshop_cleanup import finish_workshop_cleanup
from machinist.repository_custody import (
    PullRequestExpectation,
    RepositoryCustodyError,
    bind_repository,
    validate_pr_base,
    verify_branch_pr,
    verify_pull_request,
)

_SPEC_PROMPT = files("machinist") / "templates" / "spec-prompt.md"
_MAX_ISSUE_TITLE_CHARS = 500


class SpecPhaseError(Exception):
    """Phase 1 could not produce a usable spec."""


class SpecPhaseCancelled(SpecPhaseError):
    """Phase 1 stopped at a controller-owned cancellation boundary."""

    cancelled = True


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
    claim,
    revise: bool = False,
    attempt: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> PullRequest:
    report_progress(claim, "read Task", f"fetching GitHub issue #{issue_number}")
    repository_identity = _bind_repository(config, github, workspace)
    issue = github.get_issue(issue_number)
    _validate_issue(issue, config)
    _raise_if_cancelled(cancel_check, issue.number)
    previous = TaskEvidence.load(claim.previous_evidence)
    try:
        base = previous.pr_base() or github.default_branch()
    except EvidenceError as exc:
        raise SpecPhaseError("prior Spec checkpoint has an invalid PR base") from exc
    _require_pr_base(base, source="GitHub default branch")
    claim.checkpoint(
        pr_base=base,
        harness=harness_evidence(harness, profile="spec"),
    )
    branch = f"{config.workspace.branch_prefix}issue-{issue.number}"
    # Always inspect the exact branch. GitHub permits only one open PR for a
    # head branch, and blindly calling create again makes a post-delivery crash
    # impossible to recover from. Conversely, merely finding a same-named PR
    # is not authority to mutate it: recovery must prove custody below.
    existing_pr = github.pr_for_branch(branch)
    if revise and existing_pr is None:
        raise SpecPhaseError(
            f"no existing Spec PR for issue #{issue.number}; run 'machinist spec {issue.number}' first"
        )
    if existing_pr is not None and existing_pr.state == "MERGED":
        raise SpecPhaseError(
            f"PR #{existing_pr.number} is already merged; its Spec cannot be revised"
        )

    provision_args = (f"issue-{issue.number}", branch, f"origin/{base}")
    report_progress(claim, "provision Workshop", branch)
    path = workspace.provision(*provision_args, attempt=attempt)
    try:
        claim.checkpoint(workspace_path=str(path))
        remote_before = workspace.remote_sha(path, branch)
        delivery_pr = _select_delivery_pr(
            existing_pr,
            branch=branch,
            expected_base=base,
            revise=revise,
            previous=previous,
            remote_sha=remote_before,
            expected_repository=repository_identity,
        )
        recovery_sha = previous.spec_delivery_sha()
        delivery_only = (
            not revise and recovery_sha is not None and remote_before == recovery_sha
        )
        if delivery_only:
            assert recovery_sha is not None  # established by delivery_only
            report_progress(claim, "reconcile Spec delivery", recovery_sha[:12])
            # The prior attempt already published the exact checkpointed Spec.
            # Regenerating deterministic text would leave no changes to commit,
            # so reconcile only the missing/incomplete GitHub delivery.
            spec_sha = recovery_sha
            local_sha = workspace.head_sha(path)
            if local_sha != spec_sha:
                raise SpecPhaseError(
                    f"recovery workspace head {local_sha[:12]} does not match "
                    f"checkpointed Spec {spec_sha[:12]}"
                )
            observed_sha = workspace.remote_sha(path, branch)
            if observed_sha != spec_sha:
                raise SpecPhaseError(
                    f"checkpointed Spec {spec_sha[:12]} is no longer remote branch "
                    f"{branch!r} ({(observed_sha or 'missing')[:12]})"
                )
            claim.checkpoint(
                spec_sha=spec_sha,
                push_intended_sha=spec_sha,
                push_observed_sha=observed_sha,
            )
        else:
            instructions = config.resolve_instructions("spec", path)
            claim.checkpoint(**config.instructions.evidence("spec", instructions))
            _raise_if_cancelled(cancel_check, issue.number)
            report_progress(claim, "generate Spec", harness.name)
            bind_harness_progress(harness, claim, stage="generate Spec")
            spec_text = _generate_spec(
                issue,
                config,
                harness,
                workspace,
                path,
                instructions,
                cancel_check=cancel_check,
            )

            spec_relative = (
                Path(".machinist") / "specs" / f"issue-{issue.number}-spec.md"
            )
            try:
                write_managed_text(path, spec_relative, spec_text)
            except ManagedPathError as exc:
                raise SpecPhaseError(f"cannot safely write Spec: {exc}") from exc

            action = "revise" if revise else "add"
            _raise_if_cancelled(cancel_check, issue.number)
            report_progress(claim, "commit Spec", str(spec_relative))
            workspace.commit_all(
                path,
                f"docs(spec): {action} implementation spec for issue #{issue.number}",
            )
            spec_sha = workspace.head_sha(path)
            claim.checkpoint(spec_sha=spec_sha, push_intended_sha=spec_sha)
            _raise_if_cancelled(cancel_check, issue.number)
            report_progress(claim, "push Spec", branch)
            workspace.push(
                path,
                branch,
                expected_sha=remote_before,
            )
            observed_sha = workspace.remote_sha(path, branch)
            if observed_sha != spec_sha:
                raise SpecPhaseError(
                    f"pushed Spec {spec_sha[:12]}, but remote branch {branch!r} "
                    f"resolved to {(observed_sha or 'missing')[:12]}"
                )
            claim.checkpoint(push_observed_sha=observed_sha)

        _raise_if_cancelled(cancel_check, issue.number)
        report_progress(claim, "deliver draft PR", branch)
        github.ensure_label(
            config.github.labels.approved,
            color=APPROVED_LABEL_COLOR,
            description=APPROVED_LABEL_DESCRIPTION,
        )
        title = f"Spec: {issue.title} (#{issue.number})"
        body = _pr_body(issue, config, spec_sha)
        _raise_if_cancelled(cancel_check, issue.number)
        if delivery_pr is None:
            pr = github.create_draft_pr(
                branch=branch,
                base=base,
                title=title,
                body=body,
            )
        else:
            if config.github.labels.approved in delivery_pr.labels:
                _raise_if_cancelled(cancel_check, issue.number)
                github.remove_pr_label(
                    delivery_pr.number, config.github.labels.approved
                )
            if delivery_pr.state == "CLOSED":
                _raise_if_cancelled(cancel_check, issue.number)
                github.reopen_pr(delivery_pr.number)
            if not delivery_pr.is_draft:
                _raise_if_cancelled(cancel_check, issue.number)
                github.mark_draft(delivery_pr.number)
            _raise_if_cancelled(cancel_check, issue.number)
            github.update_pr(delivery_pr.number, title=title, body=body)
            pr = DraftPR(number=delivery_pr.number, url=delivery_pr.url)
        # Persist delivery identity before the verification read so a
        # crash after GitHub accepts the PR is explicitly reconcilable.
        claim.checkpoint(pr_number=pr.number, pr_url=pr.url)

        observed_pr = github.pr_for_branch(branch)
        report_progress(claim, "verify draft PR", f"PR #{pr.number}")
        delivered = _assert_delivered_pr(
            observed_pr,
            pr,
            branch=branch,
            expected_base=base,
            spec_sha=spec_sha,
            expected_repository=repository_identity,
        )
    except BaseException as exc:
        cleanup_warning = finish_workshop_cleanup(
            workspace, path, success=False, claim=claim
        )
        if cleanup_warning is not None:
            exc.add_note(cleanup_warning)
        raise
    finish_workshop_cleanup(workspace, path, success=True, claim=claim)
    return delivered


def preview_spec_phase(
    issue_number: int,
    config: MachinistConfig,
    *,
    github,
    harness,
    workspace,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Generate a Spec preview without writing, committing, pushing, or opening a PR."""
    _bind_repository(config, github, workspace)
    issue = github.get_issue(issue_number)
    _validate_issue(issue, config)
    _raise_if_cancelled(cancel_check, issue.number)
    base = github.default_branch()
    branch = f"{config.workspace.branch_prefix}issue-{issue.number}"
    preview_task = f"preview-issue-{issue.number}-{uuid4().hex[:12]}"
    path = workspace.provision_preview(preview_task, branch, f"origin/{base}")
    try:
        instructions = config.resolve_instructions("spec", path)
        _raise_if_cancelled(cancel_check, issue.number)
        return _generate_spec(
            issue,
            config,
            harness,
            workspace,
            path,
            instructions,
            cancel_check=cancel_check,
        )
    finally:
        workspace.cleanup_preview(path)


def _select_delivery_pr(
    existing_pr,
    *,
    branch: str,
    expected_base: str,
    revise: bool,
    previous: TaskEvidence,
    remote_sha: str | None,
    expected_repository: str,
):
    """Authorize either an explicit revision or a checkpoint-backed retry."""
    if existing_pr is not None:
        try:
            verify_branch_pr(
                existing_pr,
                branch=branch,
                base=expected_base,
                repository=expected_repository,
            )
        except RepositoryCustodyError as exc:
            raise SpecPhaseError(
                f"PR #{existing_pr.number} failed Spec custody: {exc}"
            ) from exc
    if revise:
        if remote_sha != existing_pr.head_sha:
            raise SpecPhaseError(
                f"existing PR #{existing_pr.number} head {existing_pr.head_sha[:12]} "
                f"does not match remote branch {(remote_sha or 'missing')[:12]}"
            )
        return existing_pr

    recovery_sha = previous.spec_delivery_sha()
    recovery_matches = recovery_sha is not None and remote_sha == recovery_sha
    if existing_pr is not None:
        checkpointed_pr = previous.pr_number
        identity_matches = checkpointed_pr in (None, existing_pr.number)
        if (
            recovery_matches
            and identity_matches
            and existing_pr.head_sha == recovery_sha
        ):
            return existing_pr
        raise SpecPhaseError(
            f"branch {branch!r} already has PR #{existing_pr.number}; refusing to "
            "mutate an existing PR without --revise or matching retry checkpoints"
        )
    if remote_sha is not None and not recovery_matches:
        raise SpecPhaseError(
            f"remote branch {branch!r} already exists at {remote_sha[:12]}; refusing "
            "to extend it without matching retry checkpoints"
        )
    return None


def _assert_delivered_pr(
    observed_pr,
    pr: DraftPR,
    *,
    branch: str,
    expected_base: str,
    spec_sha: str,
    expected_repository: str,
) -> PullRequest:
    """Verify GitHub observes this repository's exact delivered branch and SHA."""
    expected = PullRequestExpectation(
        number=pr.number,
        branch=branch,
        base=expected_base,
        head_sha=spec_sha,
        repository=expected_repository,
        is_draft=True,
    )
    try:
        return verify_pull_request(observed_pr, expected)
    except RepositoryCustodyError as exc:
        raise SpecPhaseError(
            f"GitHub PR delivery verification failed for PR #{pr.number}: {exc}"
        ) from exc


def _bind_repository(config: MachinistConfig, github, workspace) -> str:
    try:
        return bind_repository(config, github, workspace).identity
    except RepositoryCustodyError as exc:
        raise SpecPhaseError(str(exc)) from exc


def _require_pr_base(value: str, *, source: str) -> str:
    try:
        return validate_pr_base(value, source=source)
    except RepositoryCustodyError as exc:
        raise SpecPhaseError(str(exc)) from exc


def _raise_if_cancelled(
    cancel_check: Callable[[], bool] | None, issue_number: int
) -> None:
    if cancel_check is not None and cancel_check():
        raise SpecPhaseCancelled(
            f"Spec for issue #{issue_number} cancelled by operator request"
        )


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
    issue: Issue,
    config: MachinistConfig,
    harness,
    workspace,
    path: Path,
    instructions: str,
    *,
    cancel_check: Callable[[], bool] | None,
) -> str:
    """Run the read-only Harness and prove it returned a bounded Spec and left
    the Workshop untouched. Cancellation is checked before the tree so a
    cancel racing the Harness stays a cancellation, and the whole check runs
    before any controller-owned write, commit, push, label, or PR mutation."""
    spec_text = harness.generate_spec(
        render_spec_prompt(issue, instructions),
        cwd=path,
    )
    _raise_if_cancelled(cancel_check, issue.number)
    if workspace.has_changes(path):
        raise SpecPhaseError(
            f"{harness.name} changed repository files during read-only Spec generation"
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


def _pr_body(issue: Issue, config: MachinistConfig, spec_sha: str) -> str:
    approved = config.github.labels.approved
    return (
        f"AgentMachinist generated an implementation spec for #{issue.number}:\n"
        f"`.machinist/specs/issue-{issue.number}-spec.md` (see Files changed).\n"
        "\n"
        f"**To approve:** apply the `{approved}` label, or comment "
        f"`/machinist-execute {spec_sha}`.\n"
        "(GitHub's review **Approve button** is *not* the mechanism; AgentMachinist\n"
        "acts on the label plus the SHA-bound marker the approve workflow records.)\n"
        "Once approved, the machinist daemon implements the spec on this branch,\n"
        "runs the test gate, and marks this PR ready for review.\n"
        "Please leave this PR as a draft — machinist flips it to ready itself\n"
        "when the implementation lands; marking it ready early pauses the daemon.\n"
        "\n"
        f"Closes #{issue.number}"
    )
