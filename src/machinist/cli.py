from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

from machinist.admission import queue_admission
from machinist.cancellation import CancellationError, CancellationStore
from machinist.config import (
    MAX_CONFIG_BYTES,
    ConfigError,
    HarnessName,
    MachinistConfig,
    NotificationEvent,
    load_config,
    strict_yaml_load,
)
from machinist.config_cli import (
    schema as config_schema,
)
from machinist.config_cli import (
    set_value as set_config_value,
)
from machinist.config_cli import (
    show_effective,
)
from machinist.config_cli import (
    validate as validate_config,
)
from machinist.config_cli import (
    write_schema as write_config_schema,
)
from machinist.doctor import run_doctor
from machinist.github import GitHubClient, GitHubError, normalize_repository_identity
from machinist.harness import HarnessError, get_harness
from machinist.lifecycle import LifecycleError, Phase, RunStatus, TaskLifecycle
from machinist.managed_paths import (
    ManagedPathError,
    managed_file_exists,
    read_managed_text,
    write_managed_text,
)
from machinist.notification_ledger import NotificationLedger
from machinist.notify import (
    NotificationStatus,
    notification_dedupe_key,
    notify_event,
)
from machinist.observability import build_run_report, summarize_run_report
from machinist.phases.execute import (
    MAX_FEEDBACK_FILE_BYTES,
    ExecutePhaseError,
    normalize_operator_feedback,
    run_execute_phase,
)
from machinist.phases.spec import SpecPhaseError, preview_spec_phase, run_spec_phase
from machinist.phases.status import pipeline_status
from machinist.phases.watch import WatchState, plan_watch_tasks, watch_once
from machinist.portfolio import (
    DEFAULT_REGISTRY_PATH,
    PortfolioError,
    PortfolioRegistry,
    collect_local_status,
)
from machinist.process import run_supervised
from machinist.queue_control import QueueControl, QueueControlError
from machinist.service import LaunchdService, ServiceError, read_log_tail
from machinist.workflows import (
    WorkflowDriftError,
    preflight_workflow_paths,
    preflight_workflow_projection,
)
from machinist.workflows import sync_workflows as project_workflows
from machinist.workspace import Workspace, WorkspaceError

_TEMPLATES = files("machinist") / "templates"
_LABEL_COLORS = {"trigger": "1d76db", "approved": "0e8a16"}
_RUNTIME_IGNORE = "/.machinist/runs/"
_SUBPROCESS_TIMEOUT_SECONDS = 30
_MAX_GITIGNORE_BYTES = 1024 * 1024


def _make_harness(config, phase: Phase):
    harness = get_harness(config.harness_for(phase.value))
    harness.on_progress = lambda message: click.echo(f"  … {message}")
    return harness


def _task_harness(config, phase: Phase, issue_number: int, runs_dir: Path):
    harness = _make_harness(config, phase)
    harness.cancel_check = CancellationStore(runs_dir).check(issue_number)
    return harness


def _installed_version() -> str:
    try:
        return version("agentmachinist")
    except PackageNotFoundError:
        return "0.0.0+local"


def _detect_test_command(root: Path) -> str | None:
    if (root / "pyproject.toml").exists() or (root / "uv.lock").exists():
        return "uv run pytest"
    if (root / "package.json").exists():
        return "npm test"
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    return None


def _repository_root(cwd: Path) -> Path:
    """Resolve and require the actual Git repository root before init writes."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise click.ClickException(f"cannot inspect Git repository: {exc}") from exc
    if result.returncode != 0:
        raise click.ClickException(
            "current directory is not a Git repository; run 'git init' or change to a repository root"
        )
    root = Path(result.stdout.strip()).resolve()
    if cwd.resolve() != root:
        raise click.ClickException(
            f"run 'machinist init' from the Git repository root: {root}"
        )
    return root


def _bound_github_client(
    config: MachinistConfig, *, repo_root: Path | None = None
) -> GitHubClient:
    """Bind every gh operation to the controller origin's exact authority."""
    root = (repo_root or Path.cwd()).resolve()
    host, identity = Workspace(
        repo_root=root,
        config=config.workspace,
    ).repository_target()
    configured = normalize_repository_identity(config.github.repo)
    if config.github.repo is not None and configured is None:
        raise WorkspaceError("configured GitHub repository identity is invalid")
    if configured is not None and configured != identity:
        raise WorkspaceError(
            "controller Git origin does not match configured GitHub repository"
        )

    github = GitHubClient(repo=identity)
    binder = getattr(github, "bind_repository", None)
    if callable(binder):
        binder(identity, hostname=host)
    else:
        # Lightweight test doubles predate the binding API.  Production uses
        # GitHubClient.bind_repository; retaining this seam keeps command tests
        # focused on orchestration rather than duplicating the client.
        github.repo = identity
        github.repo_host = host
    return github


def _render_init_config(
    *, harness_name: str | None, test_command: str | None, manage_workflows: bool
) -> str:
    """Render and validate the commented template before replacing any config."""
    text = (_TEMPLATES / "machinist.yaml").read_text()
    if harness_name:
        text = text.replace("name: claude-code", f"name: {harness_name}", 1)
    if test_command:
        # JSON string syntax is a valid YAML scalar and safely preserves ': ',
        # '#', quotes, backslashes, and newlines from a shell command.
        marker = "tests:\n  command: null"
        if marker not in text:
            raise click.ClickException(
                "packaged machinist.yaml template has no legacy tests.command field"
            )
        text = text.replace(
            marker,
            f"tests:\n  command: {json.dumps(test_command)}",
            1,
        )
    if "manage_workflows:" in text:
        text = text.replace(
            "manage_workflows: true",
            f"manage_workflows: {'true' if manage_workflows else 'false'}",
            1,
        )
    try:
        MachinistConfig.model_validate(strict_yaml_load(text) or {})
    except (yaml.YAMLError, ValidationError) as exc:
        raise click.ClickException(
            f"generated machinist.yaml is invalid: {exc}"
        ) from exc
    return text


def _ensure_runtime_ignore(root: Path) -> None:
    relative = Path(".gitignore")
    original = read_managed_text(root, relative, max_bytes=_MAX_GITIGNORE_BYTES) or ""
    if _RUNTIME_IGNORE in original.splitlines():
        return
    separator = "" if not original or original.endswith("\n") else "\n"
    write_managed_text(root, relative, f"{original}{separator}{_RUNTIME_IGNORE}\n")


def _load_setup_config(root: Path) -> MachinistConfig:
    """Load setup config through the no-follow managed-path reader."""
    relative = Path("machinist.yaml")
    text = read_managed_text(root, relative, max_bytes=MAX_CONFIG_BYTES)
    if text is None:
        raise ConfigError("machinist.yaml not found. Run 'machinist init' first.")
    try:
        data = strict_yaml_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"machinist.yaml is not valid YAML: {exc}") from exc
    try:
        return MachinistConfig.model_validate(data or {})
    except ValidationError as exc:
        raise ConfigError(f"machinist.yaml is invalid:\n{exc}") from exc


_MACHINIST_ERRORS = (
    ConfigError,
    GitHubError,
    HarnessError,
    SpecPhaseError,
    ExecutePhaseError,
    WorkspaceError,
    LifecycleError,
    WorkflowDriftError,
    CancellationError,
    QueueControlError,
    PortfolioError,
    ServiceError,
    ManagedPathError,
)


@click.group()
@click.version_option(package_name="agentmachinist")
def main() -> None:
    """AgentMachinist: spec, approve, and execute GitHub issues with local coding agents."""


@main.command()
@click.option("--force", is_flag=True, help="Overwrite an existing machinist.yaml.")
@click.option(
    "--workflows/--no-workflows",
    "install_workflows",
    default=True,
    help="Install the GitHub Actions workflow templates.",
)
@click.option(
    "--harness",
    "harness_name",
    type=click.Choice([h.value for h in HarnessName]),
    help="Coding harness to configure (default: claude-code).",
)
@click.option(
    "--test-cmd",
    help="Test command to run for the implementation test gate.",
)
def init(
    force: bool,
    install_workflows: bool,
    harness_name: str | None = None,
    test_cmd: str | None = None,
) -> None:
    """Set up machinist.yaml, .machinist/, and GitHub workflows in this repository."""
    repo_root = _repository_root(Path.cwd())
    config_relative = Path("machinist.yaml")
    try:
        config_exists = managed_file_exists(repo_root, config_relative)
        # Inspect every setup-managed target before the first mutation.  This
        # makes --force safe in repositories containing symlink traps and
        # avoids a partially initialized repository when a later target is
        # unsafe.
        read_managed_text(
            repo_root,
            Path(".gitignore"),
            max_bytes=_MAX_GITIGNORE_BYTES,
        )
        managed_file_exists(repo_root, Path(".machinist/specs/.gitkeep"))
        # Both modes own these two managed paths. `--no-workflows` means
        # remove prior projections, not merely stop writing new ones.
        preflight_workflow_paths(repo_root)
    except ManagedPathError as exc:
        raise click.ClickException(str(exc)) from exc
    if config_exists and not force:
        raise click.ClickException(
            "machinist.yaml already exists (use --force to overwrite)"
        )

    resolved_test_cmd = test_cmd or _detect_test_command(repo_root)
    template_text = _render_init_config(
        harness_name=harness_name,
        test_command=resolved_test_cmd,
        manage_workflows=install_workflows,
    )
    planned_config = MachinistConfig.model_validate(
        strict_yaml_load(template_text) or {}
    )
    try:
        preflight_workflow_projection(
            repo_root,
            planned_config,
            installed_version=_installed_version(),
        )
        write_managed_text(repo_root, config_relative, template_text)
    except (ManagedPathError, WorkflowDriftError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("wrote machinist.yaml")
    if resolved_test_cmd and not test_cmd:
        click.echo(f"auto-detected test runner: '{resolved_test_cmd}'")

    try:
        gitkeep = Path(".machinist/specs/.gitkeep")
        if not managed_file_exists(repo_root, gitkeep):
            write_managed_text(repo_root, gitkeep, "")
        _ensure_runtime_ignore(repo_root)
    except ManagedPathError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("created .machinist/specs/")
    click.echo(f"ensured {_RUNTIME_IGNORE} is ignored by Git")

    try:
        config = _load_setup_config(repo_root)
        report = project_workflows(
            repo_root, config, installed_version=_installed_version(), check=False
        )
        for name in report.written:
            click.echo(f"wrote .github/workflows/{name}")
        for name in report.removed:
            click.echo(f"removed .github/workflows/{name}")
    except (ConfigError, WorkflowDriftError, ManagedPathError) as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        github = _bound_github_client(config, repo_root=repo_root)
        github.ensure_label(
            config.github.labels.trigger,
            color=_LABEL_COLORS["trigger"],
            description="Machinist: run the pipeline on this issue",
        )
        github.ensure_label(
            config.github.labels.approved,
            color=_LABEL_COLORS["approved"],
            description="Machinist: spec approved for implementation",
        )
        click.echo(
            f"ensured GitHub labels '{config.github.labels.trigger}' "
            f"and '{config.github.labels.approved}'"
        )
    except (GitHubError, WorkspaceError) as exc:
        click.echo(f"note: could not create GitHub labels yet ({exc})")

    click.echo(
        "\nNext steps:\n"
        "  1. Review machinist.yaml (harness, labels, test command).\n"
        "  2. Commit the new files and push.\n"
        "  3. Run 'machinist doctor' and resolve any FAIL checks.\n"
        "  4. Label an issue 'agent-task' to start the pipeline."
    )


@main.command("sync-workflows")
@click.option("--check", is_flag=True, help="Report drift without changing files.")
def sync_workflows_command(check: bool) -> None:
    """Project config into managed GitHub workflow files."""
    try:
        config = _load_setup_config(Path.cwd())
        report = project_workflows(
            Path.cwd(), config, installed_version=_installed_version(), check=check
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if check:
        click.echo("Managed workflows match machinist.yaml.")
        return
    for name in report.written:
        click.echo(f"wrote .github/workflows/{name}")
    for name in report.removed:
        click.echo(f"removed .github/workflows/{name}")
    if not report.written and not report.removed:
        click.echo("Managed workflows already match machinist.yaml.")


@main.command()
def doctor() -> None:
    """Run read-only installation and repository diagnostics."""
    try:
        config = load_config()
        report = run_doctor(Path.cwd(), config, installed_version=_installed_version())
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    for check in report.checks:
        click.echo(f"{check.level.value:<4} {check.name:<24} {check.detail}")
    if not report.ok:
        raise click.ClickException("doctor found blocking problems")


@main.group("config")
def config_command() -> None:
    """Validate, inspect, and update machinist.yaml."""


@config_command.command("validate")
@click.option("--path", type=click.Path(path_type=Path), default=Path("machinist.yaml"))
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable result.")
def config_validate(path: Path, as_json: bool) -> None:
    """Validate a config file without running a pipeline command."""
    result = validate_config(path)
    if as_json:
        click.echo(json.dumps(result.as_dict(), sort_keys=True))
    elif result.ok:
        click.echo(f"{path} is valid.")
    else:
        assert result.error is not None
        click.echo(result.error.message, err=True)
    if not result.ok:
        raise click.exceptions.Exit(1)


@config_command.command("show")
@click.option("--path", type=click.Path(path_type=Path), default=Path("machinist.yaml"))
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of YAML.")
def config_show(path: Path, as_json: bool) -> None:
    """Show the effective phase-resolved configuration."""
    try:
        effective = show_effective(path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        json.dumps(effective, indent=2, sort_keys=True)
        if as_json
        else yaml.safe_dump(effective, sort_keys=False).rstrip()
    )


@config_command.command("schema")
@click.option(
    "--output", type=click.Path(path_type=Path), help="Atomically write JSON Schema."
)
def config_schema_command(output: Path | None) -> None:
    """Print or write the generated config JSON Schema."""
    if output is None:
        click.echo(json.dumps(config_schema(), indent=2, sort_keys=True))
        return
    try:
        written = write_config_schema(output)
    except OSError as exc:
        raise click.ClickException(f"could not write {output}: {exc}") from exc
    click.echo(f"wrote {written}")


@config_command.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--path", type=click.Path(path_type=Path), default=Path("machinist.yaml"))
def config_set(key: str, value: str, path: Path) -> None:
    """Set a dotted value after full validation; rewrites canonical YAML."""
    try:
        set_config_value(key, value, path)
    except (ConfigError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"set {key} in {path} (comments were normalized)")


@main.command()
@click.argument("target", type=int, required=False)
@click.option(
    "--issue", "issue_target", type=int, help="Approve by source issue number."
)
@click.option("--pr", "pr_target", type=int, help="Approve by pull request number.")
def approve(
    target: int | None, issue_target: int | None, pr_target: int | None
) -> None:
    """Approve the exact current head of a draft PR.

    TARGET remains accepted when it identifies only one Task. Use --issue or
    --pr when GitHub issue and pull-request numbers overlap.
    """
    explicit = [value is not None for value in (issue_target, pr_target)]
    if target is not None and any(explicit):
        raise click.UsageError("use TARGET, --issue NUMBER, or --pr NUMBER; not both")
    if target is None and sum(explicit) != 1:
        raise click.UsageError("provide TARGET, --issue NUMBER, or --pr NUMBER")
    try:
        config = load_config()
        github = _bound_github_client(config)
        open_prs = github.open_machinist_prs(config.workspace.branch_prefix)
        lookup_issue = issue_target if issue_target is not None else target
        lookup_pr = pr_target if pr_target is not None else target
        by_pr = (
            next(
                (candidate for candidate in open_prs if candidate.number == lookup_pr),
                None,
            )
            if issue_target is None
            else None
        )
        issue_branch = (
            f"{config.workspace.branch_prefix}issue-{lookup_issue}"
            if lookup_issue is not None and pr_target is None
            else None
        )
        by_issue = (
            next(
                (
                    candidate
                    for candidate in open_prs
                    if candidate.branch == issue_branch
                ),
                None,
            )
            if issue_branch is not None
            else None
        )
        if (
            target is not None
            and by_pr is not None
            and by_issue is not None
            and by_pr.number != by_issue.number
        ):
            raise click.ClickException(
                f"target #{target} is ambiguous: it names PR #{by_pr.number} and issue "
                f"#{target}'s PR #{by_issue.number}; use '--pr {target}' or '--issue {target}'"
            )
        pr = by_issue if issue_target is not None else by_pr or by_issue
        if pr is None:
            requested = (
                issue_target if issue_target is not None else pr_target or target
            )
            raise click.ClickException(
                f"open machinist draft PR for #{requested} was not found"
            )
        github.approve_pr(
            pr.number,
            label=config.github.labels.approved,
            head_sha=pr.head_sha,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Approved PR #{pr.number} at {pr.head_sha[:12]}.")


@main.command()
@click.argument("issue_number", type=int)
@click.option("--phase", type=click.Choice([phase.value for phase in Phase]))
@click.option(
    "--run",
    "run_now",
    is_flag=True,
    help="Immediately execute the phase after marking retryable.",
)
@click.option(
    "--resume",
    is_flag=True,
    help="Resume the retained Execute Workshop and preserve its manual changes.",
)
@click.option(
    "--fresh",
    is_flag=True,
    help="Start Execute from the approved remote head (the safe default).",
)
def retry(
    issue_number: int,
    phase: str | None,
    run_now: bool,
    resume: bool,
    fresh: bool,
) -> None:
    """Make a failed Task Run eligible for one explicit retry."""
    if resume and fresh:
        raise click.UsageError("--resume and --fresh are mutually exclusive")
    if (resume or fresh) and not run_now:
        raise click.UsageError("--resume/--fresh require --run")
    try:
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        selected = (
            lifecycle.record(issue_number, Phase(phase))
            if phase is not None
            else lifecycle.latest(issue_number)
        )
        if resume and selected is not None and selected.phase is not Phase.EXECUTE:
            raise LifecycleError("--resume is available only for the Execute phase")
        record = lifecycle.retry(issue_number, Phase(phase) if phase else None)
        cancellation = CancellationStore(Path(".machinist/runs"))
        cancellation.clear(issue_number)
        if run_now:
            click.echo(
                f"Retrying issue #{issue_number} {record.phase.value} "
                f"after attempt {record.attempt}."
            )
            config = load_config()
            repo_root = Path.cwd()
            cancellation = CancellationStore(repo_root / ".machinist/runs")
            if record.phase is Phase.SPEC:
                pr = lifecycle.run(
                    issue_number,
                    Phase.SPEC,
                    lambda claim: run_spec_phase(
                        issue_number,
                        config,
                        github=_bound_github_client(config, repo_root=repo_root),
                        harness=_task_harness(
                            config,
                            Phase.SPEC,
                            issue_number,
                            repo_root / ".machinist/runs",
                        ),
                        workspace=Workspace(
                            repo_root=repo_root, config=config.workspace
                        ),
                        claim=claim,
                        attempt=_fresh_attempt(claim),
                        cancel_check=cancellation.check(issue_number),
                    ),
                )
                click.echo(f"Draft PR #{pr.number}: {pr.url}")
                _deliver_notification(
                    config,
                    NotificationEvent.SPEC_READY,
                    "Machinist Spec ready",
                    f"Issue #{issue_number} has draft PR #{pr.number}",
                    issue=issue_number,
                    pr=pr.number,
                )
            else:
                pr = lifecycle.run(
                    issue_number,
                    Phase.EXECUTE,
                    lambda claim: run_execute_phase(
                        issue_number,
                        config,
                        github=_bound_github_client(config, repo_root=repo_root),
                        harness=_task_harness(
                            config,
                            Phase.EXECUTE,
                            issue_number,
                            repo_root / ".machinist/runs",
                        ),
                        workspace=Workspace(
                            repo_root=repo_root, config=config.workspace
                        ),
                        test_runner=run_supervised,
                        claim=claim,
                        recovery="resume" if resume else "fresh",
                        cancel_check=cancellation.check(issue_number),
                    ),
                )
                click.echo(
                    f"PR #{pr.number} implemented and marked ready for review: {pr.url}"
                )
                _deliver_notification(
                    config,
                    NotificationEvent.PR_READY,
                    "Machinist PR ready",
                    f"Issue #{issue_number} implementation is ready in PR #{pr.number}",
                    issue=issue_number,
                    pr=pr.number,
                )
        else:
            click.echo(
                f"Issue #{issue_number} {record.phase.value} is retryable "
                f"(previous attempt {record.attempt})."
            )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.argument("issue_number", type=int)
@click.option(
    "--revise",
    is_flag=True,
    help="Regenerate the successful Spec on its existing branch and draft PR.",
)
@click.option(
    "--abandon",
    is_flag=True,
    help="Abandon the Spec Task, remove trigger/approval labels, and close its PR.",
)
@click.option(
    "--reason",
    help="Reason recorded with --abandon.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print a read-only Spec preview without commits, pushes, or a PR.",
)
def spec(
    issue_number: int,
    revise: bool = False,
    abandon: bool = False,
    reason: str | None = None,
    dry_run: bool = False,
) -> None:
    """Generate, revise, or abandon the Spec for ISSUE_NUMBER (Phase 1)."""
    if sum((revise, abandon, dry_run)) > 1:
        raise click.UsageError(
            "--revise, --abandon, and --dry-run are mutually exclusive"
        )
    if reason is not None and not abandon:
        raise click.UsageError("--reason can only be used with --abandon")
    try:
        config = load_config()
        runs_dir = Path(".machinist/runs")
        lifecycle = TaskLifecycle(runs_dir)
        cancellations = CancellationStore(runs_dir)
        github = _bound_github_client(config)
        branch = f"{config.workspace.branch_prefix}issue-{issue_number}"
        if dry_run:
            preview = preview_spec_phase(
                issue_number,
                config,
                github=github,
                harness=_task_harness(
                    config,
                    Phase.SPEC,
                    issue_number,
                    Path(".machinist/runs"),
                ),
                workspace=Workspace(repo_root=Path.cwd(), config=config.workspace),
                cancel_check=cancellations.check(issue_number),
            )
            click.echo(preview)
            return
        if abandon:
            pr = github.pr_for_branch(branch)
            if pr is not None and pr.state == "MERGED":
                raise LifecycleError(
                    f"PR #{pr.number} is merged; issue #{issue_number} cannot be abandoned"
                )
            lifecycle.abandon(
                issue_number,
                Phase.SPEC,
                reason or "Spec rejected by the operator",
            )
            issue = github.get_issue(issue_number)
            if config.github.labels.trigger in issue.labels:
                github.remove_issue_label(issue_number, config.github.labels.trigger)
            if pr is not None:
                if config.github.labels.approved in pr.labels:
                    github.remove_pr_label(pr.number, config.github.labels.approved)
                if pr.state == "OPEN":
                    github.close_pr(pr.number)
            click.echo(f"Abandoned Spec for issue #{issue_number}.")
            return
        pr = lifecycle.run(
            issue_number,
            Phase.SPEC,
            lambda claim: run_spec_phase(
                issue_number,
                config,
                github=github,
                harness=_task_harness(
                    config,
                    Phase.SPEC,
                    issue_number,
                    Path(".machinist/runs"),
                ),
                workspace=Workspace(repo_root=Path.cwd(), config=config.workspace),
                claim=claim,
                revise=revise,
                attempt=_fresh_attempt(claim),
                cancel_check=cancellations.check(issue_number),
            ),
            repeat_succeeded=revise,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    prefix = "Revised draft" if revise else "Draft"
    click.echo(f"{prefix} PR #{pr.number}: {pr.url}")
    record = lifecycle.record(issue_number, Phase.SPEC)
    spec_sha = record.evidence.get("spec_sha") if record is not None else None
    approval_hint = (
        "Review the spec, then approve with "
        f"'machinist approve --issue {issue_number}' or the "
        f"'{config.github.labels.approved}' label"
    )
    if isinstance(spec_sha, str):
        approval_hint += f", or comment '/machinist-execute {spec_sha}'"
    click.echo(f"{approval_hint}.")
    _deliver_notification(
        config,
        NotificationEvent.SPEC_READY,
        "Machinist Spec ready",
        f"Issue #{issue_number} has draft PR #{pr.number}",
        issue=issue_number,
        pr=pr.number,
    )


@main.command()
@click.option("--once", is_flag=True, help="Run a single poll pass and exit.")
@click.option(
    "-v", "--verbose", is_flag=True, help="Log polling passes and heartbeats."
)
@click.option(
    "--interval",
    type=click.IntRange(min=10),
    help="Override polling interval in seconds (minimum: 10).",
)
@click.option(
    "--max-tasks",
    type=click.IntRange(min=0),
    help="Maximum Tasks to dispatch in each poll pass.",
)
@click.option(
    "--dry-run", is_flag=True, help="Show eligible work without dispatching it."
)
def watch(
    once: bool,
    verbose: bool = False,
    interval: int | None = None,
    max_tasks: int | None = None,
    dry_run: bool = False,
) -> None:
    """Poll GitHub for labeled issues and approved PRs; dispatch the phases."""
    try:
        config = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    poll_interval = (
        interval if interval is not None else config.github.poll_interval_seconds
    )
    repo_root = Path.cwd()
    try:
        github = _bound_github_client(config, repo_root=repo_root)
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        lifecycle = TaskLifecycle(repo_root / ".machinist/runs", repo_root=repo_root)
        cancellation_store = CancellationStore(
            repo_root / ".machinist/runs", repo_root=repo_root
        )
        queue_control = QueueControl(repo_root / ".machinist/runs", repo_root=repo_root)
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    configured_max = getattr(getattr(config, "queue", None), "max_tasks_per_pass", None)
    admission_limit = max_tasks if max_tasks is not None else configured_max

    if dry_run:
        try:
            tasks = plan_watch_tasks(config, github, lifecycle=lifecycle)
            if not tasks:
                click.echo("Nothing to do.")
                return
            admitted = 0
            for task in tasks:
                reason = None
                cancellation = cancellation_store.get(task.issue_number)
                if cancellation is not None:
                    reason = f"cancelled: {cancellation.reason}"
                queue_decision = queue_control.admission(task)
                if reason is None and not queue_decision:
                    reason = queue_decision.reason
                budget_decision = queue_admission(
                    config,
                    lifecycle,
                    additionally_admitted=admitted,
                )
                if reason is None and not budget_decision.allowed:
                    reason = budget_decision.reason
                if reason is None and admitted >= admission_limit:
                    reason = f"per-pass limit {admission_limit} reached"
                if reason is None:
                    admitted += 1
                    click.echo(
                        f"eligible: {task.phase} issue #{task.issue_number} "
                        f"({task.row.state})"
                    )
                else:
                    click.echo(
                        f"deferred: {task.phase} issue #{task.issue_number}: {reason}"
                    )
        except _MACHINIST_ERRORS as exc:
            raise click.ClickException(str(exc)) from exc
        return

    def dispatch_spec(issue_number: int):
        pr = lifecycle.run(
            issue_number,
            Phase.SPEC,
            lambda claim: run_spec_phase(
                issue_number,
                config,
                github=github,
                harness=_task_harness(
                    config,
                    Phase.SPEC,
                    issue_number,
                    repo_root / ".machinist/runs",
                ),
                workspace=Workspace(repo_root=repo_root, config=config.workspace),
                claim=claim,
                attempt=_fresh_attempt(claim),
                cancel_check=cancellation_store.check(issue_number),
            ),
        )
        _deliver_notification(
            config,
            NotificationEvent.SPEC_READY,
            "Machinist Spec ready",
            f"Issue #{issue_number} has draft PR #{pr.number}",
            issue=issue_number,
            pr=pr.number,
        )
        return pr

    def dispatch_execute(issue_number: int):
        pr = lifecycle.run(
            issue_number,
            Phase.EXECUTE,
            lambda claim: run_execute_phase(
                issue_number,
                config,
                github=github,
                harness=_task_harness(
                    config,
                    Phase.EXECUTE,
                    issue_number,
                    repo_root / ".machinist/runs",
                ),
                workspace=Workspace(repo_root=repo_root, config=config.workspace),
                test_runner=run_supervised,
                claim=claim,
                recovery="fresh",
                cancel_check=CancellationStore(repo_root / ".machinist/runs").check(
                    issue_number
                ),
            ),
        )
        _deliver_notification(
            config,
            NotificationEvent.PR_READY,
            "Machinist PR ready",
            f"Issue #{issue_number} implementation is ready in PR #{pr.number}",
            issue=issue_number,
            pr=pr.number,
        )
        return pr

    state = WatchState()
    try:
        while True:
            if verbose:
                click.echo(
                    f"[{time.strftime('%X')}] polling GitHub (interval {poll_interval}s)..."
                )
            try:
                deferred_reasons: dict[tuple[str, int], str] = {}

                def admit(
                    task,
                    *,
                    deferred_reasons: dict[tuple[str, int], str] = deferred_reasons,
                ) -> bool:
                    cancellation = cancellation_store.get(task.issue_number)
                    if cancellation is not None:
                        deferred_reasons[(task.phase, task.issue_number)] = (
                            f"cancelled: {cancellation.reason}"
                        )
                        return False
                    control_decision = queue_control.admission(task)
                    if not control_decision:
                        deferred_reasons[(task.phase, task.issue_number)] = (
                            control_decision.reason
                            or "queue control deferred this Task"
                        )
                        return False
                    decision = queue_admission(
                        config,
                        lifecycle,
                    )
                    if decision.allowed:
                        return True
                    deferred_reasons[(task.phase, task.issue_number)] = (
                        decision.reason or "queue policy deferred this Task"
                    )
                    return False

                result = watch_once(
                    config,
                    github,
                    run_spec=dispatch_spec,
                    run_execute=dispatch_execute,
                    state=state,
                    notify=lambda message: _deliver_notification(
                        config,
                        NotificationEvent.FAILURE,
                        "Machinist task failed",
                        message,
                    ),
                    notify_stale=lambda issue, message: _deliver_notification(
                        config,
                        NotificationEvent.APPROVAL_STALE,
                        "Machinist approval stale",
                        message,
                        issue=issue,
                    ),
                    max_tasks=admission_limit,
                    admit=admit,
                    lifecycle=lifecycle,
                )
            except _MACHINIST_ERRORS as exc:
                if once:
                    raise click.ClickException(str(exc)) from exc
                click.echo(f"poll error: {exc}", err=True)
                result = []
            for event in result:
                click.echo(event)
            if verbose:
                for task in getattr(result, "deferred", ()):
                    reason = deferred_reasons.get(
                        (task.phase, task.issue_number),
                        f"per-pass limit {admission_limit} reached",
                    )
                    click.echo(
                        f"deferred: {task.phase} for issue #{task.issue_number}: {reason}"
                    )
            if once:
                if not result:
                    click.echo("Nothing to do.")
                failures = getattr(
                    result,
                    "failures",
                    tuple(event for event in result if event.startswith("error:")),
                )
                if failures:
                    raise click.ClickException(
                        f"{len(failures)} Task dispatch(es) failed"
                    )
                return
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        click.echo("watch stopped.")


@main.command()
@click.argument("issue_number", type=int)
@click.option(
    "--force",
    is_flag=True,
    help="Re-implement a ready PR; its current head must have fresh approval.",
)
@click.option(
    "--retry",
    "retry_failed",
    is_flag=True,
    help="Mark a failed task run retryable before executing.",
)
@click.option(
    "--resume",
    is_flag=True,
    help="Resume the retained failed Workshop and preserve its manual changes.",
)
@click.option(
    "--fresh",
    is_flag=True,
    help="Start from the approved remote head (the safe default).",
)
def run(
    issue_number: int,
    force: bool,
    retry_failed: bool = False,
    resume: bool = False,
    fresh: bool = False,
) -> None:
    """Implement the approved spec for ISSUE_NUMBER (Phase 3)."""
    if resume and fresh:
        raise click.UsageError("--resume and --fresh are mutually exclusive")
    try:
        config = load_config()
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        cancellations = CancellationStore(Path(".machinist/runs"))
        if retry_failed:
            prior = lifecycle.record(issue_number, Phase.EXECUTE)
            if prior and prior.status in {
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.ABANDONED,
            }:
                lifecycle.retry(issue_number, Phase.EXECUTE)
                cancellations.clear(issue_number)
        if cancellations.requested(issue_number):
            raise LifecycleError(
                f"issue #{issue_number} has a cancellation request; "
                f"run 'machinist cancel {issue_number} --clear' before starting"
            )
        pr = lifecycle.run(
            issue_number,
            Phase.EXECUTE,
            lambda claim: run_execute_phase(
                issue_number,
                config,
                github=_bound_github_client(config),
                harness=_task_harness(
                    config,
                    Phase.EXECUTE,
                    issue_number,
                    Path(".machinist/runs"),
                ),
                workspace=Workspace(repo_root=Path.cwd(), config=config.workspace),
                test_runner=run_supervised,
                force=force,
                claim=claim,
                recovery="resume" if resume else "fresh",
                cancel_check=cancellations.check(issue_number),
            ),
            repeat_succeeded=force,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"PR #{pr.number} implemented and marked ready for review: {pr.url}")
    _deliver_notification(
        config,
        NotificationEvent.PR_READY,
        "Machinist PR ready",
        f"Issue #{issue_number} implementation is ready in PR #{pr.number}",
        issue=issue_number,
        pr=pr.number,
    )


def _read_feedback_file(path: Path) -> str:
    """Read a small regular UTF-8 feedback file without following its leaf."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise click.ClickException(f"feedback file {path} is not a regular file")
        if metadata.st_size > MAX_FEEDBACK_FILE_BYTES:
            raise click.UsageError(
                f"feedback file is too large (maximum {MAX_FEEDBACK_FILE_BYTES} UTF-8 bytes)"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(MAX_FEEDBACK_FILE_BYTES + 1)
        if len(payload) > MAX_FEEDBACK_FILE_BYTES:
            raise click.UsageError(
                f"feedback file is too large (maximum {MAX_FEEDBACK_FILE_BYTES} UTF-8 bytes)"
            )
        return payload.decode("utf-8")
    except click.ClickException:
        raise
    except UnicodeError as exc:
        raise click.ClickException(f"feedback file {path} is not valid UTF-8") from exc
    except OSError as exc:
        raise click.ClickException(
            f"could not safely read feedback file {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@main.command()
@click.argument("issue_number", type=int)
@click.option("--feedback", help="Bounded operator feedback for the amendment.")
@click.option(
    "--feedback-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Read operator feedback from a UTF-8 file.",
)
def amend(
    issue_number: int,
    feedback: str | None,
    feedback_file: Path | None,
) -> None:
    """Rework a ready PR from explicit feedback after fresh approval."""
    if (feedback is None) == (feedback_file is None):
        raise click.UsageError("provide exactly one of --feedback or --feedback-file")
    if feedback_file is not None:
        feedback = _read_feedback_file(feedback_file)
    assert feedback is not None
    try:
        feedback = normalize_operator_feedback(feedback)
    except ExecutePhaseError as exc:
        raise click.UsageError(str(exc)) from exc
    assert feedback is not None
    try:
        config = load_config()
        runs_dir = Path(".machinist/runs")
        lifecycle = TaskLifecycle(runs_dir)
        cancellations = CancellationStore(runs_dir)
        if cancellations.requested(issue_number):
            raise LifecycleError(
                f"issue #{issue_number} has a cancellation request; clear it first"
            )
        pr = lifecycle.run(
            issue_number,
            Phase.EXECUTE,
            lambda claim: run_execute_phase(
                issue_number,
                config,
                github=_bound_github_client(config),
                harness=_task_harness(
                    config,
                    Phase.EXECUTE,
                    issue_number,
                    runs_dir,
                ),
                workspace=Workspace(repo_root=Path.cwd(), config=config.workspace),
                test_runner=run_supervised,
                force=True,
                claim=claim,
                recovery="fresh",
                feedback=feedback,
                cancel_check=cancellations.check(issue_number),
            ),
            repeat_succeeded=True,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"PR #{pr.number} amended and marked ready for review: {pr.url}")
    _deliver_notification(
        config,
        NotificationEvent.PR_READY,
        "Machinist PR amended",
        f"Issue #{issue_number} amendment is ready in PR #{pr.number}",
        issue=issue_number,
        pr=pr.number,
    )


def _fresh_attempt(claim) -> int | None:
    """Keep the first-run path stable and isolate every later attempt."""
    attempt = getattr(claim, "attempt", 1)
    return attempt if attempt > 1 else None


def _deliver_notification(
    config: MachinistConfig,
    event: NotificationEvent,
    title: str,
    message: str,
    **context,
) -> None:
    """Best-effort configured delivery; notification failures never fail a Task."""
    key = notification_dedupe_key(
        event,
        title,
        message,
        context=context,
    )
    outcome = NotificationLedger(
        Path(".machinist/runs"), repo_root=Path.cwd()
    ).deliver_once(
        key,
        lambda: notify_event(
            config.notifications,
            event,
            title,
            message,
            context=context,
            dedupe_key=key,
        ),
    )
    if outcome.warning is not None:
        click.echo(f"notification warning: {outcome.warning}", err=True)
    if (
        outcome.notification is not None
        and outcome.notification.status is NotificationStatus.FAILED
    ):
        # Preserve advisory semantics while making manual commands diagnosable.
        click.echo(f"notification warning: {outcome.notification.error}", err=True)


@main.command()
@click.argument("issue_number", type=int)
@click.option(
    "--reason",
    default="operator requested cancellation",
    show_default=True,
    help="Reason persisted with the cooperative cancellation request.",
)
@click.option("--clear", is_flag=True, help="Clear a prior cancellation request.")
def cancel(issue_number: int, reason: str, clear: bool) -> None:
    """Cancel an active supervised Task or prevent its next dispatch."""
    try:
        store = CancellationStore(Path(".machinist/runs"), repo_root=Path.cwd())
        if clear:
            removed = store.clear(issue_number)
            click.echo(
                f"Cleared cancellation for issue #{issue_number}."
                if removed
                else f"No cancellation exists for issue #{issue_number}."
            )
            return
        request = store.request(issue_number, reason)
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        state = (
            "active process will stop cooperatively"
            if lifecycle.claim_held(issue_number)
            else "future watcher dispatches are blocked"
        )
        click.echo(
            f"Cancellation requested for issue #{issue_number} at "
            f"{request.requested_at}; {state}."
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc


@main.group("queue")
def queue_command() -> None:
    """Pause, defer, resume, and inspect watcher admission."""


def _queue_control() -> QueueControl:
    return QueueControl(Path(".machinist/runs"), repo_root=Path.cwd())


@queue_command.command("pause")
@click.option("--reason", default="paused by operator", show_default=True)
def queue_pause(reason: str) -> None:
    """Pause all new watcher dispatches."""
    try:
        state = _queue_control().pause(reason)
    except (QueueControlError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Queue paused: {state['pause']['reason']}")


@queue_command.command("resume")
def queue_resume() -> None:
    """Resume globally paused dispatches; issue deferrals remain."""
    try:
        _queue_control().resume()
    except (QueueControlError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Queue resumed.")


@queue_command.command("defer")
@click.argument("issue_number", type=int)
@click.option("--reason", required=True, help="Why this Task should wait.")
def queue_defer(issue_number: int, reason: str) -> None:
    """Defer one issue until explicitly allowed."""
    try:
        _queue_control().defer(issue_number, reason)
    except (QueueControlError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Deferred issue #{issue_number}: {reason}")


@queue_command.command("allow")
@click.argument("issue_number", type=int)
def queue_allow(issue_number: int) -> None:
    """Remove one issue's deferral."""
    try:
        _queue_control().allow(issue_number)
    except (QueueControlError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Issue #{issue_number} is allowed.")


@queue_command.command("show")
@click.option("--json", "as_json", is_flag=True)
def queue_show(as_json: bool) -> None:
    """Show durable queue controls."""
    try:
        state = _queue_control().inspect()
    except (QueueControlError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(state, indent=2, sort_keys=True))
        return
    status_text = "paused" if state["paused"] else "running"
    click.echo(f"Queue: {status_text}")
    if state["error"]:
        click.echo(f"  Error: {state['error']}", err=True)
    if state["pause"]:
        click.echo(f"  Reason: {state['pause']['reason']}")
    for issue, entry in state["deferred"].items():
        click.echo(f"  Deferred #{issue}: {entry['reason']}")


@main.command()
@click.option(
    "--issue",
    "issue_number",
    type=int,
    help="Remove the workspace for this specific issue.",
)
@click.option(
    "--all",
    "all_workspaces",
    is_flag=True,
    help="Remove all workspaces for this repository.",
)
@click.option(
    "--force", is_flag=True, help="Force removal of uncommitted or dirty workspaces."
)
def clean(issue_number: int | None, all_workspaces: bool, force: bool) -> None:
    """Remove retained or stale workspaces under workspace.root."""
    try:
        config = load_config()
        repo_root = Path.cwd()
        ws = Workspace(repo_root=repo_root, config=config.workspace)
        lifecycle = TaskLifecycle(repo_root / ".machinist/runs")

        if issue_number is not None:
            if lifecycle.claim_held(issue_number):
                raise LifecycleError(
                    f"issue #{issue_number} is actively claimed; refusing to remove its Workshop"
                )
            targets = ws.list_task_workspaces(f"issue-{issue_number}")
            if not targets:
                target = ws.workspace_for_task(f"issue-{issue_number}")
                click.echo(f"No workspace found for issue #{issue_number} ({target}).")
                return
            for target in targets:
                ws.remove_workspace(target, force=force)
                click.echo(f"Removed workspace for issue #{issue_number} ({target}).")
            return

        workspaces = ws.list_workspaces()
        if not workspaces:
            click.echo("No workspaces found for this repository.")
            return

        if all_workspaces:
            claimed = [
                number
                for path in workspaces
                if (number := _issue_from_workspace_path(repo_root, path)) is not None
                and lifecycle.claim_held(number)
            ]
            if claimed:
                joined = ", ".join(f"#{number}" for number in sorted(set(claimed)))
                raise LifecycleError(
                    f"active Task Claims prevent cleanup for issue(s) {joined}"
                )
            for path in workspaces:
                ws.remove_workspace(path, force=force)
                click.echo(f"Removed {path}")
            click.echo(f"Cleaned {len(workspaces)} workspace(s).")
            return

        click.echo(f"Found {len(workspaces)} workspace(s) for this repository:")
        for path in workspaces:
            click.echo(f"  {path}")
        click.echo(
            "\nUse 'machinist clean --all' to remove all or "
            "'machinist clean --issue <n>' for one."
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc


def _issue_from_workspace_path(repo_root: Path, path: Path) -> int | None:
    prefix = f"{repo_root.name}-issue-"
    if not path.name.startswith(prefix):
        return None
    tail = path.name.removeprefix(prefix).split("-attempt-", 1)[0]
    return int(tail) if tail.isdigit() else None


@main.command()
@click.argument("issue_number", type=int)
@click.option("--offline", is_flag=True, help="Read only local runs and Workshops.")
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the complete JSON read model."
)
def inspect(issue_number: int, offline: bool = False, as_json: bool = False) -> None:
    """Show diagnostic and runtime history for ISSUE_NUMBER."""
    try:
        config = load_config()
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        ws = Workspace(repo_root=Path.cwd(), config=config.workspace)
        github = None if offline else _bound_github_client(config, repo_root=Path.cwd())
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc

    branch = f"{config.workspace.branch_prefix}issue-{issue_number}"
    remote_sources = {
        "workspaces": lambda: _workspace_source(ws, issue_number),
    }
    if not offline:
        assert github is not None
        remote_sources.update(
            {
                "github_issue": lambda: _github_issue_source(github, issue_number),
                "github_pr": lambda: _github_pr_source(github, branch),
            }
        )
    try:
        report = build_run_report(
            lifecycle,
            issue=issue_number,
            remote_sources=remote_sources,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return

    click.echo(f"Task Inspection: Issue #{issue_number}")
    source_map = {source.source: source for source in report.sources}
    issue_source = source_map.get("github_issue")
    if issue_source is not None and issue_source.ok and issue_source.data is not None:
        issue = issue_source.data
        click.echo(f"  Title: {issue['title']}")
        click.echo(f"  URL:   {issue['url']}")
        click.echo(f"  Labels: {', '.join(issue['labels']) or '(none)'}")
    elif not offline:
        detail = (
            issue_source.error.message
            if issue_source and issue_source.error
            else "not found"
        )
        click.echo(f"  GitHub Issue: (unavailable: {detail})")

    pr_source = source_map.get("github_pr")
    if pr_source is not None and pr_source.ok and pr_source.data is not None:
        pr = pr_source.data
        click.echo(f"  PR:    #{pr['number']} ({pr['url']})")
        click.echo(
            f"  Draft: {pr['is_draft']} | State: {pr['state']} | HEAD: {pr['head_sha'][:12]}"
        )
        approval_sha = pr["approval_sha"]
        click.echo(f"  Approval SHA: {approval_sha[:12] if approval_sha else '(none)'}")
    elif not offline:
        detail = pr_source.error.message if pr_source and pr_source.error else "no PR"
        click.echo(f"  PR:    ({detail})")

    workspaces = source_map["workspaces"]
    if workspaces.ok:
        paths = workspaces.data
        if paths:
            for item in paths:
                click.echo(f"  Workspace: {item['path']} ({item['state']})")
        else:
            click.echo("  Workspace: (absent)")
    else:
        click.echo(f"  Workspace: (unavailable: {workspaces.error.message})")

    records = {(record.phase, record.attempt): record for record in report.history}
    for phase in (Phase.SPEC, Phase.EXECUTE):
        phase_records = sorted(
            (
                record
                for (candidate, _), record in records.items()
                if candidate is phase
            ),
            key=lambda record: record.attempt,
        )
        if not phase_records:
            click.echo(f"  Phase [{phase.value}]: (no runs recorded)")
            continue
        for rec in phase_records:
            click.echo(
                f"  Phase [{phase.value}]: {rec.status.value} "
                f"(attempt {rec.attempt}, updated {rec.updated_at})"
            )
            if rec.error:
                click.echo(f"    Error: {rec.error}")
            if rec.evidence:
                click.echo(f"    Evidence: {json.dumps(rec.evidence, sort_keys=True)}")
    for artifact in report.corrupt:
        click.echo(f"  Corrupt runtime artifact: {artifact.path}", err=True)


@main.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Show additional task run and workspace details.",
)
@click.option(
    "--local", "local_only", is_flag=True, help="Show local Task Runs without GitHub."
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit a machine-readable read model."
)
@click.option(
    "--all", "all_repositories", is_flag=True, help="Show every registered repository."
)
@click.option(
    "--registry",
    type=click.Path(path_type=Path),
    default=DEFAULT_REGISTRY_PATH,
    help="Portfolio registry path used with --all.",
)
def status(
    verbose: bool = False,
    local_only: bool = False,
    as_json: bool = False,
    all_repositories: bool = False,
    registry: Path = DEFAULT_REGISTRY_PATH,
) -> None:
    """Show the pipeline state of machinist-managed issues and PRs."""
    if all_repositories:
        try:
            statuses = collect_local_status(PortfolioRegistry(registry))
        except PortfolioError as exc:
            raise click.ClickException(str(exc)) from exc
        if as_json:
            click.echo(
                json.dumps(
                    {"repositories": [item.to_dict() for item in statuses]},
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        if not statuses:
            click.echo("No repositories registered; run 'machinist repo add PATH'.")
            return
        for item in statuses:
            click.echo(str(item.path))
            if not item.ok:
                click.echo(f"  unavailable: {item.error_type}: {item.error}")
                continue
            for line in summarize_run_report(item.report):
                click.echo(f"  {line}")
        return
    try:
        config = load_config()
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        ws = (
            Workspace(repo_root=Path.cwd(), config=config.workspace)
            if verbose
            else None
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    sources = {}
    if not local_only:
        try:
            github = _bound_github_client(config)
        except _MACHINIST_ERRORS as exc:
            raise click.ClickException(str(exc)) from exc
        sources["pipeline"] = lambda: [
            _status_row_dict(row)
            for row in pipeline_status(config, github, lifecycle=lifecycle)
        ]
    try:
        report = build_run_report(lifecycle, remote_sources=sources)
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    if local_only:
        for line in summarize_run_report(report):
            click.echo(line)
        return

    pipeline_source = next(
        (source for source in report.sources if source.source == "pipeline"), None
    )
    if pipeline_source is None or not pipeline_source.ok:
        detail = (
            pipeline_source.error.message
            if pipeline_source is not None and pipeline_source.error is not None
            else "unknown GitHub error"
        )
        click.echo(f"GitHub pipeline unavailable: {detail}", err=True)
        for line in summarize_run_report(report):
            click.echo(line)
        return
    rows = pipeline_source.data
    if not rows:
        click.echo(
            f"No machinist activity: no open '{config.github.labels.trigger}' issues "
            f"and no open '{config.workspace.branch_prefix}*' PRs."
        )
        return
    for row in rows:
        kind = "issue" if row["kind"] == "issue" else "PR"
        click.echo(f"{kind:<5} #{row['number']:<4} {row['state']:<18} {row['title']}")
        click.echo(f"      {row['url']}")
        if verbose and row["issue_number"] is not None:
            rec = lifecycle.latest(row["issue_number"])
            if rec and rec.error:
                click.echo(f"      Error: {rec.error}")
            if ws is not None:
                targets = ws.list_task_workspaces(f"issue-{row['issue_number']}")
                for target_ws in targets:
                    click.echo(f"      Workspace: {target_ws}")


@main.command("runs")
@click.option(
    "--issue",
    "issue_number",
    type=click.IntRange(min=1),
    help="Limit output to one issue.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the complete JSON read model."
)
def runs_command(issue_number: int | None, as_json: bool) -> None:
    """List local current, historical, orphaned, and corrupt Task Runs."""
    try:
        lifecycle = TaskLifecycle(Path(".machinist/runs"), repo_root=Path.cwd())
        report = build_run_report(lifecycle, issue=issue_number)
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    for line in summarize_run_report(report):
        click.echo(line)


def _github_issue_source(github, issue_number: int) -> dict:
    issue = github.get_issue(issue_number)
    return {
        "number": issue.number,
        "title": issue.title,
        "url": issue.url,
        "labels": list(issue.labels),
    }


def _github_pr_source(github, branch: str) -> dict | None:
    pr = github.pr_for_branch(branch)
    if pr is None:
        return None
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "branch": pr.branch,
        "is_draft": pr.is_draft,
        "state": pr.state,
        "head_sha": pr.head_sha,
        "approval_sha": github.approval_sha(pr.number),
    }


def _workspace_source(workspace, issue_number: int) -> list[dict[str, str]]:
    return [
        {"path": str(path), "state": "exists" if path.exists() else "absent"}
        for path in workspace.list_task_workspaces(f"issue-{issue_number}")
    ]


def _status_row_dict(row) -> dict:
    return {
        "kind": row.kind,
        "number": row.number,
        "title": row.title,
        "state": row.state,
        "url": row.url,
        "issue_number": row.issue_number,
    }


@main.group("repo")
def repository_command() -> None:
    """Manage the optional multi-repository portfolio."""


def _registry(path: Path) -> PortfolioRegistry:
    return PortfolioRegistry(path)


@repository_command.command("add")
@click.argument("path", type=click.Path(path_type=Path), default=Path("."))
@click.option(
    "--registry", type=click.Path(path_type=Path), default=DEFAULT_REGISTRY_PATH
)
def repository_add(path: Path, registry: Path) -> None:
    """Register a repository by its canonical Git root."""
    try:
        root = _registry(registry).add(path)
    except PortfolioError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Registered {root}")


@repository_command.command("remove")
@click.argument("path", type=click.Path(path_type=Path), default=Path("."))
@click.option(
    "--registry", type=click.Path(path_type=Path), default=DEFAULT_REGISTRY_PATH
)
def repository_remove(path: Path, registry: Path) -> None:
    """Remove a repository from the portfolio."""
    try:
        removed = _registry(registry).remove(path)
    except PortfolioError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Removed {path}" if removed else f"Repository not registered: {path}")


@repository_command.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--registry", type=click.Path(path_type=Path), default=DEFAULT_REGISTRY_PATH
)
def repository_list(as_json: bool, registry: Path) -> None:
    """List registered repository roots."""
    try:
        repositories = _registry(registry).list()
    except PortfolioError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps({"repositories": [str(path) for path in repositories]}))
        return
    if not repositories:
        click.echo("No repositories registered.")
        return
    for path in repositories:
        click.echo(str(path))


@main.group("service")
def service_command() -> None:
    """Manage the macOS launchd watcher for this repository."""


def _launchd_service(*, for_install: bool = False) -> LaunchdService:
    if sys.platform != "darwin":
        raise click.ClickException(
            "the managed watcher service currently supports macOS launchd only"
        )
    root = _repository_root(Path.cwd())
    if not for_install:
        try:
            return LaunchdService.for_management(root)
        except ServiceError as exc:
            raise click.ClickException(str(exc)) from exc

    try:
        config = load_config(root / "machinist.yaml")
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    executable = shutil.which("machinist")
    if executable is None:
        raise click.ClickException(
            "cannot locate the machinist executable on PATH; activate the install environment"
        )
    try:
        return LaunchdService(
            root,
            Path(executable),
            start_interval=config.github.poll_interval_seconds,
            path_environment=os.environ.get("PATH"),
        )
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc


@service_command.command("install")
def service_install() -> None:
    """Install, register, and immediately start the repository watcher."""
    service = _launchd_service(for_install=True)
    try:
        service.stop()
        path = service.install()
        service.bootstrap()
        service.start()
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Installed and started {service.label}")
    click.echo(f"  plist: {path}")
    click.echo(f"  logs: {service.logs_dir}")


@service_command.command("start")
def service_start() -> None:
    """Start an installed watcher immediately."""
    service = _launchd_service()
    try:
        status = service.status()
        if not status.installed:
            raise ServiceError(
                f"service plist is not installed at {service.plist_path}; "
                "run 'machinist service install' first"
            )
        if not status.loaded:
            service.bootstrap()
        service.start()
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Started {service.label}")


@service_command.command("restart")
def service_restart() -> None:
    """Restart the watcher process immediately."""
    service = _launchd_service()
    try:
        status = service.status()
        if not status.installed:
            raise ServiceError(
                f"service plist is not installed at {service.plist_path}; "
                "run 'machinist service install' first"
            )
        if status.loaded:
            service.restart()
        else:
            service.bootstrap()
            service.start()
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Restarted {service.label}")


@service_command.command("stop")
def service_stop() -> None:
    """Stop the watcher while preserving its plist and logs."""
    service = _launchd_service()
    try:
        service.stop()
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Stopped {service.label}")


@service_command.command("status")
@click.option("--json", "as_json", is_flag=True)
def service_status(as_json: bool) -> None:
    """Show whether the watcher is installed and loaded."""
    service = _launchd_service()
    try:
        status = service.status()
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "label": status.label,
        "installed": status.installed,
        "loaded": status.loaded,
        "returncode": status.returncode,
        "output": status.output,
        "error": status.error,
        "plist": str(service.plist_path),
        "logs": [str(path) for path in service.log_paths],
    }
    if as_json:
        click.echo(json.dumps(payload))
        return
    state = "loaded/scheduled" if status.loaded else "not loaded"
    installed = "installed" if status.installed else "not installed"
    click.echo(f"{status.label}: {state}, {installed}")
    if status.error:
        click.echo(f"  launchd: {status.error}")
    click.echo(f"  plist: {service.plist_path}")
    click.echo(f"  logs: {service.logs_dir}")


@service_command.command("logs")
@click.option(
    "--lines",
    type=click.IntRange(min=1, max=1_000),
    default=100,
    show_default=True,
    help="Maximum lines to show from each log.",
)
def service_logs(lines: int) -> None:
    """Show bounded recent output without starting a long-running tail."""
    service = _launchd_service()
    for path in service.log_paths:
        click.echo(f"==> {path} <==")
        try:
            tail = read_log_tail(path, lines=lines)
        except FileNotFoundError:
            click.echo("(no log yet)")
            continue
        except OSError as exc:
            raise click.ClickException(f"cannot read {path}: {exc}") from exc
        except ServiceError as exc:
            raise click.ClickException(str(exc)) from exc
        if tail.text:
            click.echo(tail.text)


@service_command.command("uninstall")
def service_uninstall() -> None:
    """Stop and remove the watcher plist while preserving logs."""
    service = _launchd_service()
    try:
        removed = service.uninstall()
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    if removed:
        click.echo(f"Uninstalled {service.label}; logs retained at {service.logs_dir}")
    else:
        click.echo(f"Service is not installed; logs retained at {service.logs_dir}")
