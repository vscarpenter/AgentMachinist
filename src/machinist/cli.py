from __future__ import annotations

import subprocess
import time
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

import click

from machinist.config import ConfigError, HarnessName, load_config
from machinist.doctor import run_doctor
from machinist.github import GitHubClient, GitHubError
from machinist.harness import HarnessError, get_harness
from machinist.lifecycle import LifecycleError, Phase, RunStatus, TaskLifecycle
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
    config_path = Path("machinist.yaml")
    if config_path.exists() and not force:
        raise click.ClickException("machinist.yaml already exists (use --force to overwrite)")

    template_text = (_TEMPLATES / "machinist.yaml").read_text()
    if harness_name:
        template_text = template_text.replace("name: claude-code", f"name: {harness_name}")

    resolved_test_cmd = test_cmd or _detect_test_command(Path.cwd())
    if resolved_test_cmd:
        template_text = template_text.replace("command: null                # e.g. \"pytest -q\"", f"command: {resolved_test_cmd}        # auto-detected test command")
        if not test_cmd:
            click.echo(f"auto-detected test runner: '{resolved_test_cmd}'")

    config_path.write_text(template_text)
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
@click.argument("target", type=int)
def approve(target: int) -> None:
    """Approve the current head commit of draft PR or issue TARGET."""
    try:
        config = load_config()
        github = GitHubClient(repo=config.github.repo)
        open_prs = github.open_machinist_prs(config.workspace.branch_prefix)
        pr = next((candidate for candidate in open_prs if candidate.number == target), None)
        if pr is None:
            issue_branch = f"{config.workspace.branch_prefix}issue-{target}"
            pr = next((candidate for candidate in open_prs if candidate.branch == issue_branch), None)
        if pr is None:
            raise click.ClickException(f"open machinist draft PR for #{target} was not found")
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
@click.option("--run", "run_now", is_flag=True, help="Immediately execute the phase after marking retryable.")
def retry(issue_number: int, phase: str | None, run_now: bool) -> None:
    """Make a failed Task Run eligible for one explicit retry."""
    try:
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        record = lifecycle.retry(
            issue_number, Phase(phase) if phase else None
        )
        click.echo(
            f"Issue #{issue_number} {record.phase.value} is retryable "
            f"(previous attempt {record.attempt})."
        )
        if run_now:
            config = load_config()
            repo_root = Path.cwd()
            if record.phase is Phase.SPEC:
                pr = lifecycle.run(
                    issue_number,
                    Phase.SPEC,
                    lambda claim: run_spec_phase(
                        issue_number,
                        config,
                        github=GitHubClient(repo=config.github.repo),
                        harness=_make_harness(config),
                        workspace=Workspace(repo_root=repo_root, config=config.workspace),
                        claim=claim,
                    ),
                )
                click.echo(f"Draft PR #{pr.number}: {pr.url}")
            else:
                pr = lifecycle.run(
                    issue_number,
                    Phase.EXECUTE,
                    lambda claim: run_execute_phase(
                        issue_number,
                        config,
                        github=GitHubClient(repo=config.github.repo),
                        harness=_make_harness(config),
                        workspace=Workspace(repo_root=repo_root, config=config.workspace),
                        test_runner=subprocess.run,
                        claim=claim,
                    ),
                )
                click.echo(f"PR #{pr.number} implemented and marked ready for review: {pr.url}")
    except (_MACHINIST_ERRORS, LifecycleError) as exc:
        raise click.ClickException(str(exc)) from exc


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
@click.option("-v", "--verbose", is_flag=True, help="Log polling passes and heartbeats.")
@click.option("--interval", type=int, help="Override polling interval in seconds.")
def watch(once: bool, verbose: bool = False, interval: int | None = None) -> None:
    """Poll GitHub for labeled issues and approved PRs; dispatch the phases."""
    try:
        config = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    poll_interval = interval or config.github.poll_interval_seconds
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
            if verbose:
                click.echo(f"[{time.strftime('%X')}] polling GitHub (interval {poll_interval}s)...")
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
def run(issue_number: int, force: bool, retry_failed: bool = False) -> None:
    """Implement the approved spec for ISSUE_NUMBER (Phase 3)."""
    try:
        config = load_config()
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        if retry_failed:
            prior = lifecycle.record(issue_number, Phase.EXECUTE)
            if prior and prior.status == RunStatus.FAILED:
                lifecycle.retry(issue_number, Phase.EXECUTE)
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
@click.option("--issue", "issue_number", type=int, help="Remove the workspace for this specific issue.")
@click.option("--all", "all_workspaces", is_flag=True, help="Remove all workspaces for this repository.")
@click.option("--force", is_flag=True, help="Force removal of uncommitted or dirty workspaces.")
def clean(issue_number: int | None, all_workspaces: bool, force: bool) -> None:
    """Remove retained or stale workspaces under workspace.root."""
    try:
        config = load_config()
        ws = Workspace(repo_root=Path.cwd(), config=config.workspace)
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc

    if issue_number is not None:
        target = ws.workspace_for_task(f"issue-{issue_number}")
        if not target.exists():
            click.echo(f"No workspace found for issue #{issue_number} ({target}).")
            return
        ws.remove_workspace(target, force=force)
        click.echo(f"Removed workspace for issue #{issue_number} ({target}).")
        return

    workspaces = ws.list_workspaces()
    if not workspaces:
        click.echo("No workspaces found for this repository.")
        return

    if all_workspaces:
        for path in workspaces:
            ws.remove_workspace(path, force=force)
            click.echo(f"Removed {path}")
        click.echo(f"Cleaned {len(workspaces)} workspace(s).")
        return

    click.echo(f"Found {len(workspaces)} workspace(s) for this repository:")
    for path in workspaces:
        click.echo(f"  {path}")
    click.echo("\nUse 'machinist clean --all' to remove all or 'machinist clean --issue <n>' for one.")


@main.command()
@click.argument("issue_number", type=int)
def inspect(issue_number: int) -> None:
    """Show diagnostic and runtime history for ISSUE_NUMBER."""
    try:
        config = load_config()
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        ws = Workspace(repo_root=Path.cwd(), config=config.workspace)
        github = GitHubClient(repo=config.github.repo)
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Task Inspection: Issue #{issue_number}")
    try:
        issue = github.get_issue(issue_number)
        click.echo(f"  Title: {issue.title}")
        click.echo(f"  URL:   {issue.url}")
        click.echo(f"  Labels: {', '.join(issue.labels) or '(none)'}")
    except GitHubError:
        click.echo("  GitHub Issue: (could not fetch)")

    branch = f"{config.workspace.branch_prefix}issue-{issue_number}"
    open_prs = github.open_machinist_prs(config.workspace.branch_prefix)
    pr = next((p for p in open_prs if p.branch == branch), None)
    if pr:
        approval_sha = github.approval_sha(pr.number)
        click.echo(f"  PR:    #{pr.number} ({pr.url})")
        click.echo(f"  Draft: {pr.is_draft} | HEAD: {pr.head_sha[:12]}")
        click.echo(f"  Approval SHA: {approval_sha[:12] if approval_sha else '(none)'}")
    else:
        click.echo("  PR:    (no open PR)")

    ws_path = ws.workspace_for_task(f"issue-{issue_number}")
    click.echo(f"  Workspace: {ws_path} ({'exists' if ws_path.exists() else 'absent'})")

    for phase in (Phase.SPEC, Phase.EXECUTE):
        rec = lifecycle.record(issue_number, phase)
        if rec:
            click.echo(f"  Phase [{phase.value}]: {rec.status.value} (attempt {rec.attempt}, updated {rec.updated_at})")
            if rec.error:
                click.echo(f"    Error: {rec.error}")
            if rec.evidence:
                click.echo(f"    Evidence: {rec.evidence}")
        else:
            click.echo(f"  Phase [{phase.value}]: (no runs recorded)")


@main.command()
@click.option("-v", "--verbose", is_flag=True, help="Show additional task run and workspace details.")
def status(verbose: bool = False) -> None:
    """Show the pipeline state of machinist-managed issues and PRs."""
    try:
        config = load_config()
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
        rows = pipeline_status(
            config,
            GitHubClient(repo=config.github.repo),
            lifecycle=lifecycle,
        )
        ws = Workspace(repo_root=Path.cwd(), config=config.workspace) if verbose else None
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
        if verbose and row.issue_number is not None:
            rec = lifecycle.latest(row.issue_number)
            if rec and rec.error:
                click.echo(f"      Error: {rec.error}")
            if ws is not None:
                target_ws = ws.workspace_for_task(f"issue-{row.issue_number}")
                if target_ws.exists():
                    click.echo(f"      Workspace: {target_ws}")
