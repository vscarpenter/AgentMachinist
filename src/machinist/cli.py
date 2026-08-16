"""machinist — bridge GitHub issues to local coding harnesses."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import click

_TEMPLATES = files("machinist") / "templates"
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


def _not_implemented(phase: str) -> None:
    raise click.ClickException(
        f"{phase} is not implemented yet in this milestone; "
        "see docs/superpowers/specs/ for the roadmap."
    )


@main.command()
@click.argument("issue_number", type=int)
def spec(issue_number: int) -> None:
    """Generate a spec and open a draft PR for ISSUE_NUMBER (Phase 1)."""
    _not_implemented("Phase 1 ('machinist spec')")


@main.command()
def watch() -> None:
    """Poll GitHub for labeled issues and approved PRs (daemon)."""
    _not_implemented("The watch daemon")


@main.command()
@click.argument("issue_number", type=int)
def run(issue_number: int) -> None:
    """Implement the approved spec for ISSUE_NUMBER (Phase 3)."""
    _not_implemented("Phase 3 ('machinist run')")


@main.command()
def status() -> None:
    """Show the pipeline state of machinist-managed issues and PRs."""
    _not_implemented("'machinist status'")
