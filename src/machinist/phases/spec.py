"""Phase 1: GitHub issue → implementation spec → draft PR.

The harness runs in read-only print mode inside an isolated workspace:
it can explore the codebase for context, but machinist itself writes the
spec file, commits, pushes, and opens the draft PR.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from importlib.resources import files
from pathlib import Path
from string import Template
from uuid import uuid4

from machinist.config import MachinistConfig
from machinist.github import DraftPR, Issue, PullRequest, normalize_repository_identity
from machinist.managed_paths import ManagedPathError, write_managed_text
from machinist.phases.progress import bind_harness_progress, report_progress
from machinist.phases.workshop_cleanup import finish_workshop_cleanup

_APPROVED_LABEL_COLOR = "0e8a16"
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
    claim=None,
    revise: bool = False,
    attempt: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> DraftPR:
    if hasattr(workspace, "cancel_check"):
        workspace.cancel_check = cancel_check
    report_progress(claim, "read Task", f"fetching GitHub issue #{issue_number}")
    repository_identity = _assert_repository_custody(config, github, workspace)
    issue = github.get_issue(issue_number)
    _validate_issue(issue, config)
    _raise_if_cancelled(cancel_check, issue.number)
    previous = dict(getattr(claim, "previous_evidence", {}) or {})
    base = _checkpointed_pr_base(previous) or github.default_branch()
    _validate_pr_base(base, source="GitHub default branch")
    if claim is not None:
        claim.checkpoint(pr_base=base)
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
    path = (
        workspace.provision(*provision_args)
        if attempt is None
        else workspace.provision(*provision_args, attempt=attempt)
    )
    try:
        if claim is not None:
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
        recovery_sha = _checkpointed_push_sha(previous)
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
            if claim is not None:
                claim.checkpoint(
                    spec_recovery="delivery-only",
                    spec_sha=spec_sha,
                    push_intended_sha=spec_sha,
                    push_observed_sha=observed_sha,
                )
        else:
            instructions = config.resolve_instructions("spec", path)
            if claim is not None:
                profile = config.instructions.spec
                claim.checkpoint(
                    spec_recovery="generated",
                    instructions_sha256=hashlib.sha256(
                        instructions.encode()
                    ).hexdigest(),
                    instruction_paths=list(profile.paths),
                    instruction_append=profile.append is not None,
                    instruction_source="task-workspace",
                )
            _raise_if_cancelled(cancel_check, issue.number)
            report_progress(claim, "generate Spec", harness.name)
            bind_harness_progress(harness, claim, stage="generate Spec")
            spec_text = _generate_spec(issue, config, harness, path, instructions)
            # The harness may finish just as an operator requests cancellation.
            # This boundary is deliberately before any controller-owned write,
            # commit, push, label, or PR mutation.
            _raise_if_cancelled(cancel_check, issue.number)
            if workspace.has_changes(path):
                raise SpecPhaseError(
                    f"{harness.name} changed repository files during read-only spec generation"
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
            if claim is not None:
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
            if claim is not None:
                claim.checkpoint(push_observed_sha=observed_sha)

        _raise_if_cancelled(cancel_check, issue.number)
        report_progress(claim, "deliver draft PR", branch)
        github.ensure_label(
            config.github.labels.approved,
            color=_APPROVED_LABEL_COLOR,
            description="Machinist: spec approved for implementation",
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
            if not delivery_pr.is_draft and hasattr(github, "mark_draft"):
                _raise_if_cancelled(cancel_check, issue.number)
                github.mark_draft(delivery_pr.number)
            _raise_if_cancelled(cancel_check, issue.number)
            github.update_pr(delivery_pr.number, title=title, body=body)
            pr = DraftPR(number=delivery_pr.number, url=delivery_pr.url)
        if claim is not None:
            # Persist delivery identity before the verification read so a
            # crash after GitHub accepts the PR is explicitly reconcilable.
            claim.checkpoint(pr_number=pr.number, pr_url=pr.url)

        observed_pr = github.pr_for_branch(branch)
        report_progress(claim, "verify draft PR", f"PR #{pr.number}")
        _assert_delivered_pr(
            observed_pr,
            pr,
            branch=branch,
            expected_base=base,
            spec_sha=spec_sha,
            expected_repository=repository_identity,
        )
        if claim is not None:
            claim.checkpoint(
                pr_observed_number=observed_pr.number,
                pr_observed_base=observed_pr.base or base,
                pr_observed_sha=observed_pr.head_sha,
            )
    except BaseException as exc:
        cleanup_warning = finish_workshop_cleanup(
            workspace, path, success=False, claim=claim
        )
        if cleanup_warning is not None:
            exc.add_note(cleanup_warning)
        raise
    finish_workshop_cleanup(workspace, path, success=True, claim=claim)
    return pr


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
    _assert_repository_custody(config, github, workspace)
    issue = github.get_issue(issue_number)
    _validate_issue(issue, config)
    _raise_if_cancelled(cancel_check, issue.number)
    base = github.default_branch()
    branch = f"{config.workspace.branch_prefix}issue-{issue.number}"
    preview_task = f"preview-issue-{issue.number}-{uuid4().hex[:12]}"
    provision_preview = getattr(workspace, "provision_preview", None)
    cleanup_preview = getattr(workspace, "cleanup_preview", None)
    if not callable(provision_preview) or not callable(cleanup_preview):
        raise SpecPhaseError("workspace does not support isolated Spec previews")
    path = provision_preview(preview_task, branch, f"origin/{base}")
    try:
        instructions = config.resolve_instructions("spec", path)
        _raise_if_cancelled(cancel_check, issue.number)
        spec_text = _generate_spec(issue, config, harness, path, instructions)
        _raise_if_cancelled(cancel_check, issue.number)
        if workspace.has_changes(path):
            raise SpecPhaseError(
                f"{harness.name} changed repository files during read-only spec preview"
            )
        return spec_text
    finally:
        cleanup_preview(path)


def _select_delivery_pr(
    existing_pr,
    *,
    branch: str,
    expected_base: str,
    revise: bool,
    previous: Mapping[str, object],
    remote_sha: str | None,
    expected_repository: str,
):
    """Authorize either an explicit revision or a checkpoint-backed retry."""
    if existing_pr is not None and existing_pr.is_cross_repository:
        raise SpecPhaseError(
            f"PR #{existing_pr.number} is cross-repository; refusing Spec custody"
        )
    if existing_pr is not None and not _same_repository_pr(
        existing_pr, expected_repository
    ):
        raise SpecPhaseError(
            f"PR #{existing_pr.number} head repository does not match controller origin"
        )
    if existing_pr is not None and existing_pr.branch != branch:
        raise SpecPhaseError(
            f"GitHub returned PR #{existing_pr.number} for unexpected branch "
            f"{existing_pr.branch!r}; expected {branch!r}"
        )
    if (
        existing_pr is not None
        and existing_pr.base
        and existing_pr.base != expected_base
    ):
        raise SpecPhaseError(
            f"PR #{existing_pr.number} targets base {existing_pr.base!r}; "
            f"expected {expected_base!r}"
        )
    if revise:
        if existing_pr is None:  # guarded before provisioning; keeps helper total
            return None
        if remote_sha != existing_pr.head_sha:
            raise SpecPhaseError(
                f"existing PR #{existing_pr.number} head {existing_pr.head_sha[:12]} "
                f"does not match remote branch {(remote_sha or 'missing')[:12]}"
            )
        return existing_pr

    recovery_sha = _checkpointed_push_sha(previous)
    recovery_matches = recovery_sha is not None and remote_sha == recovery_sha
    if existing_pr is not None:
        checkpointed_pr = previous.get("pr_number")
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


def _checkpointed_push_sha(previous: Mapping[str, object]) -> str | None:
    """Return a consistent prior push intent suitable for custody recovery."""
    spec_sha = previous.get("spec_sha")
    intended_sha = previous.get("push_intended_sha")
    observed_sha = previous.get("push_observed_sha")
    if not isinstance(spec_sha, str) or not isinstance(intended_sha, str):
        return None
    if spec_sha != intended_sha:
        return None
    if observed_sha is not None and observed_sha != intended_sha:
        return None
    return intended_sha


def _checkpointed_pr_base(previous: Mapping[str, object]) -> str | None:
    """Return the base bound by an earlier delivery attempt, if present."""
    value = previous.get("pr_base")
    if value is None:
        return None
    if not isinstance(value, str):
        raise SpecPhaseError("prior Spec checkpoint has an invalid PR base")
    _validate_pr_base(value, source="prior Spec checkpoint")
    return value


def _validate_pr_base(value: str, *, source: str) -> None:
    if (
        not value
        or value != value.strip()
        or any(character in value for character in ("\0", "\n", "\r"))
    ):
        raise SpecPhaseError(f"{source} returned an invalid PR base")


def _assert_delivered_pr(
    observed_pr,
    pr: DraftPR,
    *,
    branch: str,
    expected_base: str,
    spec_sha: str,
    expected_repository: str,
) -> None:
    """Verify GitHub observes this repository's exact delivered branch and SHA."""
    if observed_pr is None:
        raise SpecPhaseError(
            f"GitHub did not observe PR #{pr.number} for branch {branch!r} after delivery"
        )
    mismatches: list[str] = []
    if observed_pr.number != pr.number:
        mismatches.append(f"number #{observed_pr.number} != #{pr.number}")
    if observed_pr.branch != branch:
        mismatches.append(f"branch {observed_pr.branch!r} != {branch!r}")
    if observed_pr.base and observed_pr.base != expected_base:
        mismatches.append(f"base {observed_pr.base!r} != {expected_base!r}")
    if observed_pr.state != "OPEN":
        mismatches.append(f"state {observed_pr.state!r} != 'OPEN'")
    if observed_pr.is_cross_repository:
        mismatches.append("PR is cross-repository")
    elif not _same_repository_pr(observed_pr, expected_repository):
        mismatches.append("PR head repository does not match controller origin")
    if not observed_pr.is_draft:
        mismatches.append("PR is not a draft")
    if observed_pr.head_sha != spec_sha:
        mismatches.append(
            f"head {(observed_pr.head_sha or 'missing')[:12]} != {spec_sha[:12]}"
        )
    if mismatches:
        raise SpecPhaseError(
            f"GitHub PR delivery verification failed for PR #{pr.number}: "
            + "; ".join(mismatches)
        )


def _same_repository_pr(pr: PullRequest, expected_repository: str) -> bool:
    if pr.is_cross_repository:
        return False
    return not (
        pr.head_repository is not None
        and normalize_repository_identity(pr.head_repository) != expected_repository
    )


def _assert_repository_custody(config: MachinistConfig, github, workspace) -> str:
    resolver = getattr(workspace, "repository_target", None)
    if not callable(resolver):
        raise SpecPhaseError("cannot prove controller Git origin repository identity")
    try:
        origin_host, raw_origin_identity = resolver()
        origin_identity = normalize_repository_identity(raw_origin_identity)
    except Exception as exc:
        raise SpecPhaseError(
            "cannot prove controller Git origin repository identity"
        ) from exc
    if not isinstance(origin_host, str) or not origin_host:
        raise SpecPhaseError("cannot prove controller Git origin repository host")
    configured = normalize_repository_identity(config.github.repo)
    if (
        (config.github.repo is not None and configured is None)
        or origin_identity is None
        or (configured is not None and configured != origin_identity)
    ):
        raise SpecPhaseError(
            "controller Git origin does not match configured GitHub repository"
        )
    client_target = normalize_repository_identity(getattr(github, "repo", None))
    if client_target is not None and client_target != origin_identity:
        raise SpecPhaseError(
            "configured GitHub repository does not match the GitHub client target"
        )
    client_host = getattr(github, "repo_host", None)
    if (
        client_host is not None
        and str(client_host).casefold() != origin_host.casefold()
    ):
        raise SpecPhaseError(
            "GitHub client host does not match controller Git origin host"
        )
    binder = getattr(github, "bind_repository", None)
    try:
        if callable(binder):
            binder(origin_identity, hostname=origin_host)
        else:
            github.repo = origin_identity
            github.repo_host = origin_host
    except Exception as exc:
        raise SpecPhaseError(
            "could not bind GitHub client to controller origin repository"
        ) from exc
    return origin_identity


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
    path: Path,
    instructions: str,
) -> str:
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


def _pr_body(issue: Issue, config: MachinistConfig, spec_sha: str) -> str:
    approved = config.github.labels.approved
    return (
        f"AgentMachinist generated an implementation spec for #{issue.number}:\n"
        f"`.machinist/specs/issue-{issue.number}-spec.md` (see Files changed).\n"
        "\n"
        f"**To approve:** apply the `{approved}` label, or comment "
        f"`/machinist-execute {spec_sha}`.\n"
        "(GitHub's review **Approve button** is *not* the mechanism — GitHub blocks it\n"
        "on your own PRs, and machinist only watches the label.)\n"
        "Once approved, the machinist daemon implements the spec on this branch,\n"
        "runs the test gate, and marks this PR ready for review.\n"
        "Please leave this PR as a draft — machinist flips it to ready itself\n"
        "when the implementation lands; marking it ready early pauses the daemon.\n"
        "\n"
        f"Closes #{issue.number}"
    )
