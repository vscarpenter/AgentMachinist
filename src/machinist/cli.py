"""machinist — bridge GitHub issues to local coding harnesses."""

from __future__ import annotations

import subprocess
import time
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

import click

from machinist.config import ConfigError, load_config
from machinist.doctor import run_doctor
from machinist.github import GitHubClient, GitHubError
from machinist.harness import HarnessError, get_harness
from machinist.lifecycle import LifecycleError, Phase, TaskLifecycle
from machinist.notify import notify
from machinist.phases.execute import ExecutePhaseError, run_execute_phase
from machinist.phases.spec import SpecPhaseError, run_spec_phase
from machinist.phases.status import pipeline_status
from machinist.phases.watch import WatchState, watch_once
from machinist.workspace import Workspace, WorkspaceError
from machinist.workflows import WorkflowDriftError, sync_workflows as project_workflows

_TEMPLATES = files("machinist") / "templates"
_LABEL_COLORS = {"trigger": "1d76db", "approved": "0e8a16"}


def _make_harness(config):
    harness = get_harness(config.harness)
    harness.on_progress = lambda message: click.echo(f"  … {message}")
    return harness


def _installed_version() -> str:
    try:
        return version("agentmachinist")
    except PackageNotFoundError:
        return "0.0.0+local"


_MACHINIST_ERRORS = (
    ConfigError, GitHubError, HarnessError, SpecPhaseError, ExecutePhaseError, WorkspaceError,
    LifecycleError, WorkflowDriftError,
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
def init(force: bool, install_workflows: bool) -> None:
    """Set up machinist.yaml, .machinist/, and GitHub workflows in this repository."""
    config_path = Path("machinist.yaml")
    if config_path.exists() and not force:
        raise click.ClickException("machinist.yaml already exists (use --force to overwrite)")
    config_path.write_text((_TEMPLATES / "machinist.yaml").read_text())
    click.echo(f"wrote {config_path}")

    specs_dir = Path(".machinist/specs")
    specs_dir.mkdir(parents=True, exist_ok=True)
    (specs_dir / ".gitkeep").touch()
    click.echo(f"created {specs_dir}/")

    try:
        config = load_config()
        if install_workflows:
            report = project_workflows(
                Path.cwd(), config, installed_version=_installed_version(), check=False
            )
            for name in report.written:
                click.echo(f"wrote .github/workflows/{name}")
        github = GitHubClient(repo=config.github.repo)
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
    except (GitHubError, ConfigError) as exc:
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
        config = load_config()
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


@main.command()
@click.argument("pr_number", type=int)
def approve(pr_number: int) -> None:
    """Approve the current head commit of draft PR_NUMBER."""
    try:
        config = load_config()
        github = GitHubClient(repo=config.github.repo)
        pr = next(
            (
                candidate
                for candidate in github.open_machinist_prs(config.workspace.branch_prefix)
                if candidate.number == pr_number
            ),
            None,
        )
        if pr is None:
            raise click.ClickException(f"open machinist draft PR #{pr_number} was not found")
        github.approve_pr(
            pr.number,
            label=config.github.labels.approved,
            head_sha=pr.head_sha,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Approved PR #{pr_number} at {pr.head_sha[:12]}.")


@main.command()
@click.argument("issue_number", type=int)
@click.option("--phase", type=click.Choice([phase.value for phase in Phase]))
def retry(issue_number: int, phase: str | None) -> None:
    """Make a failed Task Run eligible for one explicit retry."""
    try:
        record = TaskLifecycle(Path(".machinist/runs")).retry(
            issue_number, Phase(phase) if phase else None
        )
    except LifecycleError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Issue #{issue_number} {record.phase.value} is retryable "
        f"(previous attempt {record.attempt})."
    )


@main.command()
@click.argument("issue_number", type=int)
def spec(issue_number: int) -> None:
    """Generate a spec and open a draft PR for ISSUE_NUMBER (Phase 1)."""
    try:
        config = load_config()
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        pr = lifecycle.run(
            issue_number,
            Phase.SPEC,
            lambda claim: run_spec_phase(
                issue_number,
                config,
                github=GitHubClient(repo=config.github.repo),
                harness=_make_harness(config),
                workspace=Workspace(repo_root=Path.cwd(), config=config.workspace),
                claim=claim,
            ),
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Draft PR #{pr.number}: {pr.url}")
    click.echo("Review the spec, then approve with the "
               f"'{config.github.labels.approved}' label or a /machinist-execute comment.")


@main.command()
@click.option("--once", is_flag=True, help="Run a single poll pass and exit.")
def watch(once: bool) -> None:
    """Poll GitHub for labeled issues and approved PRs; dispatch the phases."""
    try:
        config = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    github = GitHubClient(repo=config.github.repo)
    repo_root = Path.cwd()
    lifecycle = TaskLifecycle(repo_root / ".machinist/runs")

    def dispatch_spec(issue_number: int):
        return lifecycle.run(
            issue_number,
            Phase.SPEC,
            lambda claim: run_spec_phase(
                issue_number, config,
                github=github,
                harness=_make_harness(config),
                workspace=Workspace(repo_root=repo_root, config=config.workspace),
                claim=claim,
            ),
        )

    def dispatch_execute(issue_number: int):
        return lifecycle.run(
            issue_number,
            Phase.EXECUTE,
            lambda claim: run_execute_phase(
                issue_number, config,
                github=github,
                harness=_make_harness(config),
                workspace=Workspace(repo_root=repo_root, config=config.workspace),
                test_runner=subprocess.run,
                claim=claim,
            ),
        )

    state = WatchState()
    try:
        while True:
            try:
                events = watch_once(
                    config, github,
                    run_spec=dispatch_spec, run_execute=dispatch_execute, state=state,
                    notify=lambda message: notify("machinist watch", message),
                )
            except _MACHINIST_ERRORS as exc:
                if once:
                    raise click.ClickException(str(exc)) from exc
                click.echo(f"poll error: {exc}", err=True)
                events = []
            for event in events:
                click.echo(event)
            if once:
                if not events:
                    click.echo("Nothing to do.")
                return
            time.sleep(config.github.poll_interval_seconds)
    except KeyboardInterrupt:
        click.echo("watch stopped.")


@main.command()
@click.argument("issue_number", type=int)
@click.option(
    "--force",
    is_flag=True,
    help="Re-implement a ready PR; its current head must have fresh approval.",
)
def run(issue_number: int, force: bool) -> None:
    """Implement the approved spec for ISSUE_NUMBER (Phase 3)."""
    try:
        config = load_config()
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        pr = lifecycle.run(
            issue_number,
            Phase.EXECUTE,
            lambda claim: run_execute_phase(
                issue_number,
                config,
                github=GitHubClient(repo=config.github.repo),
                harness=_make_harness(config),
                workspace=Workspace(repo_root=Path.cwd(), config=config.workspace),
                test_runner=subprocess.run,
                force=force,
                claim=claim,
            ),
            repeat_succeeded=force,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"PR #{pr.number} implemented and marked ready for review: {pr.url}")


@main.command()
def status() -> None:
    """Show the pipeline state of machinist-managed issues and PRs."""
    try:
        config = load_config()
        rows = pipeline_status(
            config,
            GitHubClient(repo=config.github.repo),
            lifecycle=TaskLifecycle(Path(".machinist/runs")),
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if not rows:
        click.echo(
            f"No machinist activity: no open '{config.github.labels.trigger}' issues "
            f"and no open '{config.workspace.branch_prefix}*' PRs."
        )
        return
    for row in rows:
        kind = "issue" if row.kind == "issue" else "PR"
        click.echo(f"{kind:<5} #{row.number:<4} {row.state:<18} {row.title}")
        click.echo(f"      {row.url}")
