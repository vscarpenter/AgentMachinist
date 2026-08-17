"""machinist — bridge GitHub issues to local coding harnesses."""

from __future__ import annotations

import subprocess
import time
from importlib.resources import files
from pathlib import Path

import click

from machinist.config import ConfigError, load_config
from machinist.github import GitHubClient, GitHubError
from machinist.harness import HarnessError, get_harness
from machinist.phases.execute import ExecutePhaseError, run_execute_phase
from machinist.phases.spec import SpecPhaseError, run_spec_phase
from machinist.phases.status import pipeline_status
from machinist.phases.watch import WatchState, watch_once
from machinist.workspace import Workspace, WorkspaceError

_TEMPLATES = files("machinist") / "templates"
_MACHINIST_ERRORS = (
    ConfigError, GitHubError, HarnessError, SpecPhaseError, ExecutePhaseError, WorkspaceError,
)
_WORKFLOW_NAMES = ("machinist-spec.yml", "machinist-approve.yml")


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

    if install_workflows:
        workflows_dir = Path(".github/workflows")
        workflows_dir.mkdir(parents=True, exist_ok=True)
        for name in _WORKFLOW_NAMES:
            (workflows_dir / name).write_text((_TEMPLATES / "github" / name).read_text())
            click.echo(f"wrote {workflows_dir / name}")

    click.echo(
        "\nNext steps:\n"
        "  1. Review machinist.yaml (harness, labels, test command).\n"
        "  2. Commit the new files and push.\n"
        "  3. For CI spec generation, add an ANTHROPIC_API_KEY repository secret.\n"
        "  4. Label an issue 'agent-task' to start the pipeline."
    )


@main.command()
@click.argument("issue_number", type=int)
def spec(issue_number: int) -> None:
    """Generate a spec and open a draft PR for ISSUE_NUMBER (Phase 1)."""
    try:
        config = load_config()
        pr = run_spec_phase(
            issue_number,
            config,
            github=GitHubClient(repo=config.github.repo),
            harness=get_harness(config.harness),
            workspace=Workspace(repo_root=Path.cwd(), config=config.workspace),
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

    def dispatch_spec(issue_number: int):
        return run_spec_phase(
            issue_number, config,
            github=github,
            harness=get_harness(config.harness),
            workspace=Workspace(repo_root=repo_root, config=config.workspace),
        )

    def dispatch_execute(issue_number: int):
        return run_execute_phase(
            issue_number, config,
            github=github,
            harness=get_harness(config.harness),
            workspace=Workspace(repo_root=repo_root, config=config.workspace),
            test_runner=subprocess.run,
        )

    state = WatchState()
    try:
        while True:
            try:
                events = watch_once(
                    config, github,
                    run_spec=dispatch_spec, run_execute=dispatch_execute, state=state,
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
def run(issue_number: int) -> None:
    """Implement the approved spec for ISSUE_NUMBER (Phase 3)."""
    try:
        config = load_config()
        pr = run_execute_phase(
            issue_number,
            config,
            github=GitHubClient(repo=config.github.repo),
            harness=get_harness(config.harness),
            workspace=Workspace(repo_root=Path.cwd(), config=config.workspace),
            test_runner=subprocess.run,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"PR #{pr.number} implemented and marked ready for review: {pr.url}")


@main.command()
def status() -> None:
    """Show the pipeline state of machinist-managed issues and PRs."""
    try:
        config = load_config()
        rows = pipeline_status(config, GitHubClient(repo=config.github.repo))
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
