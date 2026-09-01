from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
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
from machinist.doctor import (
    CheckLevel,
    DoctorReport,
    fix_hint_for_check_name,
    run_doctor,
)
from machinist.explain import TaskExplanation, explain_task
from machinist.github import GitHubClient, GitHubError, normalize_repository_identity
from machinist.harness import HarnessError, get_harness, get_harness_descriptor
from machinist.init_wizard import InitAnswers, run_init_wizard
from machinist.lifecycle import LifecycleError, Phase, RunStatus, TaskLifecycle
from machinist.live_status import StatusSnapshot, iter_status_snapshots
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
from machinist.observability import RunReport, build_run_report, summarize_run_report
from machinist.onboarding import OnboardingError, deliver_setup_pr
from machinist.phases.execute import (
    MAX_FEEDBACK_FILE_BYTES,
    ExecutePhaseError,
    normalize_operator_feedback,
    run_execute_phase,
)
from machinist.phases.review import ReviewPhaseError, run_review_phase
from machinist.phases.spec import SpecPhaseError, preview_spec_phase, run_spec_phase
from machinist.phases.status import StatusRow, next_action_for_status, pipeline_status
from machinist.phases.watch import WatchState, plan_watch_tasks, watch_once
from machinist.portfolio import (
    DEFAULT_REGISTRY_PATH,
    PortfolioError,
    PortfolioRegistry,
    collect_local_status,
)
from machinist.process import run_supervised
from machinist.queue_control import QueueControl, QueueControlError
from machinist.rehearsal import (
    RehearsalError,
    run_harness_rehearsal,
    simulate_rehearsal,
)
from machinist.reporting import (
    MetricsReport,
    ReportingError,
    build_metrics_report,
    parse_since_duration,
)
from machinist.service import (
    LaunchdService,
    ServiceError,
    read_log_tail,
    read_watcher_heartbeat,
    write_watcher_heartbeat,
)
from machinist.task_intake import (
    TASK_TEMPLATE_PATH,
    TaskLintReport,
    TaskTemplateDriftError,
    lint_task_body,
    preflight_task_template,
    render_task_body,
    sync_task_template,
)
from machinist.telemetry import build_otlp_payload, export_otlp, validate_otlp_endpoint
from machinist.updates import (
    DEFAULT_TIMEOUT_SECONDS,
    UpdateStatus,
    check_for_update,
)
from machinist.workflows import (
    WorkflowDriftError,
    preflight_workflow_paths,
    preflight_workflow_projection,
)
from machinist.workflows import sync_workflows as project_workflows
from machinist.workspace import Workspace, WorkspaceError

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


def _stdin_is_interactive() -> bool:
    """init prompts only when a human is attached; CI and pipes stay silent."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _detect_test_command(root: Path) -> str | None:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text())
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            data = {}
        dependency_text = json.dumps(
            {
                "project": data.get("project", {}),
                "dependency-groups": data.get("dependency-groups", {}),
                "tool": {"uv": data.get("tool", {}).get("uv", {})},
            }
        ).casefold()
        pytest_configured = bool(data.get("tool", {}).get("pytest"))
        if pytest_configured or re.search(r"\bpytest(?:\W|$)", dependency_text):
            return (
                "uv run pytest" if (root / "uv.lock").is_file() else "python -m pytest"
            )
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text())
            script = package.get("scripts", {}).get("test")
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            script = None
        if (
            isinstance(script, str)
            and script.strip()
            and "no test specified" not in script.casefold()
        ):
            if (root / "bun.lock").exists() or (root / "bun.lockb").exists():
                return "bun run test"
            if (root / "pnpm-lock.yaml").exists():
                return "pnpm test"
            if (root / "yarn.lock").exists():
                return "yarn test"
            return "npm test"
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    return None


def _print_init_receipt(
    config: MachinistConfig,
    *,
    labels_ready: bool,
    suggested_test_command: str | None,
) -> None:
    gates = config.resolved_verification_gates()
    trigger = config.github.labels.trigger
    source = config.github.spec_source.value
    click.echo("\nSetup receipt:")
    click.echo(f"  Spec dispatch: {source}")
    click.echo("  Execute dispatch: local watcher")
    if gates:
        click.echo(
            "  Verification: "
            + ", ".join(
                f"{gate.name} ({'required' if gate.required else 'advisory'})"
                for gate in gates
            )
        )
    else:
        suggestion = (
            f" Suggested command: {suggested_test_command!r}."
            if suggested_test_command
            else ""
        )
        click.echo(
            "  Verification: NOT CONFIGURED — a PR can be marked ready without tests."
            + suggestion
        )
    click.echo(f"  Labels: {'ready' if labels_ready else 'setup incomplete'}")
    click.echo(
        "  Approval workflow: "
        + ("managed" if config.github.manage_workflows else "external setup required")
    )

    click.echo("\nNext steps:")
    step = 1
    if not gates:
        command = suggested_test_command or "<your test command>"
        click.echo(
            f"  {step}. Configure a required gate: machinist config set tests.command {json.dumps(command)}"
        )
        click.echo(
            f"     (or: machinist init --force --test-cmd {json.dumps(command)})"
        )
        step += 1
    if not labels_ready:
        click.echo(f"  {step}. Create/update labels: machinist sync-labels --apply")
        step += 1
    if source == "github-actions" and config.github.manage_workflows:
        descriptor = get_harness_descriptor(config.harness_for("spec").name)
        secret_name = config.github.spec_secret_env or (
            descriptor.ci_spec.secret_env if descriptor.ci_spec is not None else None
        )
        if secret_name is None:
            raise click.ClickException(
                "selected Harness has no hosted Spec CI credential metadata"
            )
        click.echo(
            f"  {step}. Configure CI authentication: gh secret set {secret_name}"
        )
        step += 1
    # Collapse the old 4-check preflight into a single doctor invocation; doctor
    # covers labels, workflow drift, the sealed task template, and the gates.
    click.echo(
        f"  {step}. Verify setup (one command checks everything): "
        "machinist doctor --run-gates"
    )
    step += 1
    click.echo(f"  {step}. Commit the generated files (copy-paste):")
    click.echo("       git status --short")
    click.echo("       git add machinist.yaml .machinist/specs/.gitkeep .gitignore")
    click.echo("       git add .github/ISSUE_TEMPLATE/agentmachinist-task.yml")
    if config.github.manage_workflows:
        click.echo("       git add -p .github/workflows   # review each hunk")
    click.echo("       git diff --cached              # verify what will be committed")
    click.echo('       git commit -m "chore: configure AgentMachinist"')
    click.echo("       git push")
    step += 1
    click.echo(
        f"  {step}. Start dispatch: machinist watch  (or, on macOS, machinist service install)"
    )
    step += 1
    click.echo(
        f"  {step}. Create a ready Task: machinist task new --title <title> --dispatch"
    )
    click.echo(
        f"     Or lint an existing issue before applying {trigger!r}: "
        "machinist task lint <issue>"
    )
    click.echo("")
    click.echo(
        "  Visual walkthrough: https://agentmachinist.vinny.dev/first-run-guide.html"
    )


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


def _workflow_drift_notice() -> str | None:
    """Return an upgrade advisory when managed workflows are out of date.

    A managed-workflow change ships in a projected file rather than in library
    code, so upgrading the package alone leaves the old workflow in place. That
    is how a security fix can be installed but not in effect. This runs on the
    paths an operator already walks so the gap is not silent.

    Advisory only: any failure returns None rather than blocking the caller.
    """
    try:
        config = load_config()
        if not getattr(config.github, "manage_workflows", True):
            return None
        project_workflows(
            Path.cwd(), config, installed_version=_installed_version(), check=True
        )
    except WorkflowDriftError:
        return (
            "Managed GitHub workflows do not match this installation. "
            "Run 'machinist sync-workflows' to apply them; until then this "
            "repository keeps the workflows projected by an earlier version."
        )
    except Exception:  # noqa: BLE001 - an advisory must never break a command
        return None
    return None


def _render_init_config(
    *,
    harness_name: str | None,
    test_command: str | None,
    manage_workflows: bool,
    spec_source: str | None = None,
    notification_backend: str | None = None,
    notification_events: list[str] | None = None,
) -> str:
    """Render and validate a minimal first-run config.

    The full 94-line reference stays in src/machinist/templates/machinist.yaml
    and docs/getting-started.md; the file written to disk is intentionally
    minimal (~18 lines) covering only essential keys.
    """
    harness = harness_name or "claude-code"
    spec = spec_source or "local"
    # JSON string syntax is a valid YAML scalar and safely preserves ': ',
    # '#', quotes, backslashes, and newlines from a shell command.
    command_value = json.dumps(test_command) if test_command else "null"
    lines: list[str] = [
        "# AgentMachinist configuration",
        "# See docs/getting-started.md for all options.",
        "version: 1",
        "",
        "harness:",
        f"  name: {harness}",
        "",
        "tests:",
        f"  command: {command_value}",
        "",
        "github:",
        "  repo: null",
        f"  spec_source: {spec}",
    ]
    # Only emit manage_workflows when it diverges from the default (true).
    if not manage_workflows:
        lines.append("  manage_workflows: false")
    lines.extend(
        [
            "  labels:",
            "    trigger: agent-task",
            '    approved: "machinist:approved"',
            "  poll_interval_seconds: 60",
            "",
            "workspace:",
            "  root: ~/.machinist/workspaces",
            "  strategy: worktree",
            "  cleanup: on_success",
            "  branch_prefix: agent/",
            "",
            "review:",
            "  enabled: true",
        ]
    )
    # Notifications are advanced; include only when explicitly configured.
    if notification_backend is not None or notification_events is not None:
        lines.extend(["", "notifications:"])
        if notification_backend is not None:
            lines.append(f"  backend: {notification_backend}")
        if notification_events is not None:
            lines.append(f"  events: [{', '.join(notification_events)}]")
    text = "\n".join(lines) + "\n"
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
        raise ConfigError(
            "machinist.yaml not found. This repo is not configured yet. "
            "Run 'machinist onboard' (recommended) or 'machinist onboard --setup-pr' on GitHub "
            "— fallback: 'machinist init'. "
            "After setup, verify with 'machinist doctor --run-gates'. "
            "Guide: https://agentmachinist.vinny.dev/first-run-guide.html"
        )
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
    ReviewPhaseError,
    WorkspaceError,
    LifecycleError,
    WorkflowDriftError,
    CancellationError,
    QueueControlError,
    PortfolioError,
    ServiceError,
    ManagedPathError,
    OnboardingError,
    RehearsalError,
    TaskTemplateDriftError,
    ReportingError,
)


_COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    (
        "Setup  — first run & health",
        [
            "onboard",
            "init",
            "doctor",
            "rehearse",
            "sync-labels",
            "sync-workflows",
            "update-check",
        ],
    ),
    (
        "Tasks  — create and approve work",
        ["task", "spec", "approve"],
    ),
    (
        "Build  — implement and review",
        ["run", "review", "amend", "retry", "cancel"],
    ),
    (
        "Operate — daily",
        ["watch", "status"],
    ),
    (
        "Operate — advanced",
        [
            "queue",
            "service",
            "explain",
            "inspect",
            "report",
            "runs",
            "config",
            "clean",
            "repo",
        ],
    ),
]


class MachinistGroup(click.Group):
    """Render top-level help in workflow order instead of alphabetically."""

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        # Show commands grouped by workflow phase so the onboarding path is obvious.
        # Hidden commands are filtered exactly as click.Group.format_commands does.
        visible = {
            name: command
            for name in self.list_commands(ctx)
            if (command := self.get_command(ctx, name)) is not None
            and not command.hidden
        }
        if not visible:
            return
        # Match Click's spacing: short help is truncated to the remaining width.
        limit = formatter.width - 6 - max(len(name) for name in visible)

        def section(label: str, names: list[str]) -> None:
            rows = [
                (name, visible[name].get_short_help_str(limit))
                for name in names
                if name in visible
            ]
            if rows:
                with formatter.section(label):
                    formatter.write_dl(rows)

        # Declaration order inside each group is the workflow order, not alphabetical.
        for label, members in _COMMAND_GROUPS:
            section(label, members)
        grouped = {name for _, members in _COMMAND_GROUPS for name in members}
        section("Other", sorted(set(visible) - grouped))


@click.group(cls=MachinistGroup)
@click.version_option(package_name="agentmachinist")
def main() -> None:
    """AgentMachinist: spec, approve, and execute GitHub issues with local coding agents.

    Start with 'machinist onboard' — it creates machinist.yaml, workflows, and
    labels, then prints the exact next steps. See
    https://agentmachinist.vinny.dev/first-run-guide.html for a visual walkthrough.
    """


@main.command()
@click.option("--force", is_flag=True, help="Overwrite an existing machinist.yaml.")
@click.option(
    "--workflows/--no-workflows",
    "install_workflows",
    default=None,
    help="Install managed GitHub workflows (approval + optional CI dispatch).",
)
@click.option(
    "--harness",
    "harness_name",
    type=click.Choice([h.value for h in HarnessName]),
    help="Coding harness to use (default: claude-code). Managed CI installs the selected harness.",
)
@click.option(
    "--test-cmd",
    help="Test command that must pass before a PR is marked ready (e.g. 'uv run pytest').",
)
@click.option(
    "--spec-source",
    type=click.Choice(["local", "github-actions"]),
    help="Who generates specs: 'local' via machinist watch, or 'github-actions' via CI.",
)
@click.option(
    "--notifications",
    type=click.Choice(["desktop", "disabled"]),
    help="Desktop notification backend.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip prompts and use safe defaults plus auto-detected test command (hands-free quickstart). Implies --no-input.",
)
@click.option(
    "--no-input",
    is_flag=True,
    help="Skip prompts; use flags and safe defaults (for CI or scripts). Use --yes to also auto-enable the detected test command.",
)
def init(
    force: bool,
    install_workflows: bool | None,
    harness_name: str | None = None,
    test_cmd: str | None = None,
    spec_source: str | None = None,
    notifications: str | None = None,
    no_input: bool = False,
    yes: bool = False,
) -> None:
    """Set up machinist.yaml, .machinist/, and GitHub workflows in this repository.

    Prefer 'machinist onboard' for new repositories — it wraps this command
    with a guided receipt and an optional reviewable draft PR.
    """
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
        preflight_task_template(repo_root)
    except (ManagedPathError, TaskTemplateDriftError) as exc:
        raise click.ClickException(str(exc)) from exc
    if config_exists and not force:
        raise click.ClickException(
            "machinist.yaml already exists (use --force to overwrite)"
        )

    detected_test_cmd = _detect_test_command(repo_root)
    interactive = not no_input and not yes and _stdin_is_interactive()
    if interactive:
        answers = run_init_wizard(
            detected_test_command=detected_test_cmd,
            spec_source=spec_source,
            harness_name=harness_name,
            test_command=test_cmd,
            install_workflows=install_workflows,
            notifications=notifications,
        )
    else:
        # --no-input skips prompts but intentionally does NOT enable auto-detected
        # test command. --yes implies --no-input plus auto-enable detected command.
        effective_test_cmd = test_cmd
        if yes and effective_test_cmd is None:
            effective_test_cmd = detected_test_cmd
        answers = InitAnswers(
            spec_source=spec_source,
            harness_name=harness_name,
            test_command=effective_test_cmd,
            install_workflows=True if install_workflows is None else install_workflows,
            notification_backend=notifications,
            notification_events=None,
        )
    template_text = _render_init_config(
        harness_name=answers.harness_name,
        test_command=answers.test_command,
        manage_workflows=answers.install_workflows,
        spec_source=answers.spec_source,
        notification_backend=answers.notification_backend,
        notification_events=answers.notification_events,
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
    if not interactive and detected_test_cmd and not test_cmd and not yes:
        click.echo(
            f"suggested test runner (not enabled): '{detected_test_cmd}'; "
            "confirm it with --test-cmd"
        )

    labels_ready = False
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
        task_template = sync_task_template(repo_root, check=False)
        if task_template.written:
            click.echo(f"wrote {TASK_TEMPLATE_PATH}")
    except (
        ConfigError,
        WorkflowDriftError,
        TaskTemplateDriftError,
        ManagedPathError,
    ) as exc:
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
        labels_ready = True
    except (GitHubError, WorkspaceError) as exc:
        click.echo(f"note: could not create GitHub labels yet ({exc})")

    _print_init_receipt(
        config,
        labels_ready=labels_ready,
        suggested_test_command=detected_test_cmd,
    )


@main.command()
@click.argument("issue_number", type=click.IntRange(min=1))
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable policy.")
def explain(issue_number: int, as_json: bool) -> None:
    """Explain one Task's effective policy, state, and exact next action."""
    repo_root = Path.cwd()
    runs_dir = repo_root / ".machinist/runs"
    try:
        config = load_config()
        lifecycle = TaskLifecycle(runs_dir, repo_root=repo_root)
        result = explain_task(
            issue_number,
            config,
            _bound_github_client(config, repo_root=repo_root),
            lifecycle=lifecycle,
            cancellation=CancellationStore(runs_dir, repo_root=repo_root),
            workspace=Workspace(repo_root=repo_root, config=config.workspace),
        )
    except (ValueError, *_MACHINIST_ERRORS) as exc:
        raise click.ClickException(str(exc)) from exc
    payload = result.to_dict()
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    _print_task_explanation(result)


def _print_task_explanation(result: TaskExplanation) -> None:
    click.echo(f"Issue #{result.issue}: {result.state}")
    click.echo(f"  {result.url}")
    click.echo(f"  Next: {result.next_action or 'Human review or no action required'}")
    dispatch = result.dispatch
    managed = "managed" if dispatch["managed_workflows"] else "external"
    click.echo(
        f"Dispatch: Spec via {dispatch['spec_source']} ({dispatch['spec_install']}, "
        f"{managed} workflows); ready transition: {dispatch['ready_transition_owner']}"
    )
    click.echo("Effective Harness profiles:")
    for phase, profile in result.profiles.items():
        enabled = "enabled" if profile["enabled"] else "disabled"
        model = profile["model"] or "provider default"
        click.echo(
            f"  {phase}: {profile['harness']} · {model} · "
            f"{profile['timeout_minutes']}m · {enabled}"
        )
    gates = result.verification
    click.echo("Verification:")
    if gates:
        for gate in gates:
            click.echo(f"  {gate['name']}: {gate['command']}")
    else:
        click.echo("  No gates configured.")
    click.echo("Instruction overlays:")
    for phase, policy in result.instructions.items():
        paths = ", ".join(policy["paths"]) or "none"
        append = " + inline append" if policy["inline_append"] else ""
        click.echo(f"  {phase}: {paths}{append}")
    click.echo(
        f"Workspace: {result.workspace['strategy']} · {result.workspace['branch']} · "
        f"cleanup {result.workspace['cleanup']} · retained "
        f"{len(result.workspace['retained'])}"
    )
    click.echo("Limits: " + json.dumps(result.limits, sort_keys=True))
    click.echo("Queue: " + json.dumps(result.queue, sort_keys=True))
    click.echo("Attempts:")
    for phase, attempt in result.attempts.items():
        summary = (
            "none"
            if attempt is None
            else (f"#{attempt['attempt']} {attempt['status']}")
        )
        click.echo(f"  {phase}: {summary}")
    if result.cancellation is not None:
        click.echo(f"Cancellation: requested ({result.cancellation['reason']})")
    click.echo(
        "Credentials: values hidden; allowed Harness names: "
        + ", ".join(result.credentials["allowed_names"])
    )


@main.command()
@click.option(
    "--setup-pr",
    is_flag=True,
    help="Create a draft PR with setup files for team review (recommended on GitHub).",
)
@click.option(
    "--workflows/--no-workflows",
    "install_workflows",
    default=None,
    help="Install managed GitHub workflows (approval + optional CI dispatch).",
)
@click.option(
    "--harness",
    "harness_name",
    type=click.Choice([h.value for h in HarnessName]),
    help="Coding harness to use (default: claude-code).",
)
@click.option(
    "--test-cmd",
    help="Test command that must pass before a PR is marked ready (e.g. 'uv run pytest').",
)
@click.option(
    "--spec-source",
    type=click.Choice(["local", "github-actions"]),
    help="Who generates specs: 'local' via machinist watch, or 'github-actions' via CI.",
)
@click.option(
    "--notifications",
    type=click.Choice(["desktop", "disabled"]),
    help="Desktop notification backend.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip prompts and use safe defaults plus auto-detected test command (hands-free quickstart). Implies --no-input.",
)
@click.option(
    "--no-input",
    is_flag=True,
    help="Skip prompts; use flags and safe defaults (for CI or scripts). Use --yes to also auto-enable the detected test command.",
)
@click.pass_context
def onboard(
    ctx: click.Context,
    setup_pr: bool,
    install_workflows: bool | None,
    harness_name: str | None,
    test_cmd: str | None,
    spec_source: str | None,
    notifications: str | None,
    no_input: bool,
    yes: bool = False,
) -> None:
    """Set up this repository for AgentMachinist (recommended first command).

    Creates machinist.yaml, .machinist/specs/, the sealed task issue form,
    and managed GitHub workflows/labels. In a terminal it asks a few
    questions (dispatch mode, harness, test gate) with safe defaults — use
    flags or --no-input to pre-answer.

    Prefer 'machinist onboard --setup-pr' when setup should be reviewed
    as a draft PR rather than committed directly.

    Visual walkthrough: https://agentmachinist.vinny.dev/first-run-guide.html
    """
    arguments = {
        "force": False,
        "install_workflows": install_workflows,
        "harness_name": harness_name,
        "test_cmd": test_cmd,
        "spec_source": spec_source,
        "notifications": notifications,
        "no_input": no_input,
        "yes": yes,
    }
    if not setup_pr:
        ctx.invoke(init, **arguments)
        return
    repo_root = _repository_root(Path.cwd())

    def initialize() -> None:
        ctx.invoke(init, **arguments)

    def validate() -> None:
        config = _load_setup_config(repo_root)
        project_workflows(
            repo_root,
            config,
            installed_version=_installed_version(),
            check=True,
        )
        sync_task_template(repo_root, check=True)
        readiness = run_doctor(
            repo_root,
            config,
            installed_version=_installed_version(),
            run_gates=True,
        )
        failures = [
            f"{check.name}: {check.detail}"
            for check in readiness.checks
            if check.level.value == "FAIL"
        ]
        if failures:
            raise OnboardingError("setup preflight failed: " + "; ".join(failures))

    try:
        result = deliver_setup_pr(
            repo_root,
            github=GitHubClient(),
            initialize=initialize,
            validate=validate,
        )
    except (OnboardingError, GitHubError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Draft setup PR #{result.pr.number}: {result.pr.url}")
    click.echo("Next: review the generated files and run machinist doctor --run-gates.")


@main.command()
@click.option(
    "--harness",
    "use_harness",
    is_flag=True,
    help="Invoke configured Harnesses in the disposable rehearsal repository.",
)
def rehearse(use_harness: bool) -> None:
    """Rehearse the lifecycle locally without creating GitHub artifacts."""
    try:
        config = load_config()
        if use_harness:
            result = run_harness_rehearsal(
                config,
                harness_factory=lambda phase: _make_harness(config, Phase(phase)),
            )
            mode = "configured Harnesses; API usage may have occurred"
        else:
            result = simulate_rehearsal(review_enabled=config.review.enabled)
            mode = "controller simulation; no model or API usage"
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Rehearsal passed ({mode}).")
    for transition in result.transitions:
        click.echo(f"  ✓ {transition}")
    click.echo("Next: create or lint a real Task before applying the trigger label.")


@main.group()
def task() -> None:
    """Create, check, and lint AgentMachinist Tasks."""


@task.command("template")
@click.option("--write", is_flag=True, help="Write the managed issue form.")
@click.option("--check", is_flag=True, help="Check the managed issue form for drift.")
def task_template(write: bool, check: bool) -> None:
    """Project or check the managed GitHub issue form."""
    if write == check:
        raise click.UsageError("choose exactly one of --write or --check")
    try:
        report = sync_task_template(Path.cwd(), check=check)
    except (TaskTemplateDriftError, ManagedPathError) as exc:
        raise click.ClickException(str(exc)) from exc
    if check:
        click.echo("Managed task template matches AgentMachinist.")
    elif report.written:
        click.echo(f"wrote {TASK_TEMPLATE_PATH}")
    else:
        click.echo("Managed task template already matches AgentMachinist.")


@task.command("lint")
@click.argument("issue_number", type=click.IntRange(min=1))
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable report.")
def task_lint(issue_number: int, as_json: bool) -> None:
    """Check whether a live GitHub issue is ready for dispatch."""
    try:
        config = load_config()
        issue = _bound_github_client(config).get_issue(issue_number)
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    report = lint_task_body(issue.body)
    _print_task_lint(report, as_json=as_json)
    if not report.ready:
        raise click.exceptions.Exit(1)


@task.command("new")
@click.option("--title", required=True, help="GitHub issue title.")
@click.option(
    "--dispatch",
    is_flag=True,
    help="Apply the configured trigger label after readiness lint passes.",
)
def task_new(title: str, dispatch: bool) -> None:
    """Prompt for a structured Task and create a GitHub issue."""
    click.echo("Creating a structured Task — 5 prompts, then local lint before GitHub:")
    click.echo(
        "  Objective: one sentence describing the outcome (e.g. 'Make auth errors actionable')."
    )
    click.echo(
        "  Acceptance: checkboxes the reviewer can verify (e.g. '- [ ] Error names the credential')."
    )
    click.echo(
        "  Constraints: what must not change (e.g. 'Preserve the public CLI contract')."
    )
    click.echo(
        "  Verification: how to prove it (e.g. 'Run uv run pytest tests/test_auth.py')."
    )
    click.echo("  Context: background or links (optional, default: Not provided).")
    click.echo("")
    body = render_task_body(
        objective=click.prompt("Objective", prompt_suffix=" — one sentence outcome: "),
        acceptance=click.prompt(
            "Acceptance criteria", prompt_suffix=" — checkboxes to verify: "
        ),
        constraints=click.prompt(
            "Constraints", prompt_suffix=" — what must not change: "
        ),
        verification=click.prompt(
            "Verification", prompt_suffix=" — how to prove it (command): "
        ),
        context=click.prompt(
            "Context",
            default="Not provided",
            show_default=False,
            prompt_suffix=" — background/links: ",
        ),
    )
    report = lint_task_body(body)
    if not report.ready:
        _print_task_lint(report, as_json=False)
        raise click.exceptions.Exit(1)
    try:
        config = load_config()
        github = _bound_github_client(config)
        issue = github.create_issue(title=title, body=body)
        if dispatch:
            github.add_issue_label(issue.number, config.github.labels.trigger)
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Created Task #{issue.number}: {issue.url}")
    if dispatch:
        click.echo(f"Dispatched with label {config.github.labels.trigger!r}.")
    else:
        click.echo(f"Next: machinist task lint {issue.number}")


def _print_task_lint(report: TaskLintReport, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(report.to_dict(), sort_keys=True))
        return
    if report.ready:
        click.echo("Task is ready for dispatch.")
        return
    click.echo("Task is not ready for dispatch:")
    for finding in report.errors:
        click.echo(f"  {finding.field}: {finding.message}")


@main.command()
@click.option(
    "--since",
    "since_text",
    default="30d",
    show_default=True,
    help="Positive integer reporting window with h, d, or w suffix.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable report.")
@click.option(
    "--otlp-endpoint",
    help="Export redacted aggregate metrics to this OTLP/HTTP JSON endpoint.",
)
def report(since_text: str, as_json: bool, otlp_endpoint: str | None) -> None:
    """Summarize local Task Run reliability and optionally export metrics."""
    try:
        window = parse_since_duration(since_text)
        config = load_config()
        endpoint = otlp_endpoint or config.telemetry.otlp_endpoint
        if endpoint is not None:
            validate_otlp_endpoint(endpoint)
        lifecycle = TaskLifecycle(Path(".machinist/runs"), repo_root=Path.cwd())
        history = build_run_report(lifecycle).history
        generated_at = datetime.now(UTC)
        metrics = build_metrics_report(
            history,
            since=generated_at - window,
            generated_at=generated_at,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(metrics.to_dict(), indent=2, sort_keys=True))
    else:
        _print_metrics_report(metrics)
    if endpoint is None:
        return
    try:
        repository = Workspace(
            repo_root=Path.cwd(), config=config.workspace
        ).repository_identity()
        export_otlp(
            endpoint,
            build_otlp_payload(metrics, repository=repository),
            timeout_seconds=config.telemetry.timeout_seconds,
        )
    except (ReportingError, WorkspaceError) as exc:
        raise click.ClickException(f"OTLP export failed: {exc}") from exc
    click.echo(f"Exported aggregate metrics to {endpoint}.", err=as_json)


def _print_metrics_report(report: MetricsReport) -> None:
    click.echo(f"Local Task Run report since {report.since}:")
    if not report.attempts:
        click.echo("  No Task Run attempts in this window.")
        return
    success = "n/a" if report.success_rate is None else f"{report.success_rate:.1%}"
    click.echo(
        f"  Attempts: {report.attempts} · success: {success} · "
        f"retries: {report.retry_count} · cancellations: {report.cancellation_count}"
    )
    durations = report.duration_seconds
    if durations["median"] is not None:
        click.echo(
            f"  Duration: median {durations['median']:.1f}s · "
            f"p95 {durations['p95']:.1f}s"
        )
    for phase, statuses in report.by_phase.items():
        summary = ", ".join(f"{status} {count}" for status, count in statuses.items())
        click.echo(f"  {phase}: {summary}")


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


@main.command("sync-labels")
@click.option("--check", is_flag=True, help="Check label readiness without writing.")
@click.option("--apply", is_flag=True, help="Create or update required labels.")
@click.pass_context
def sync_labels_command(ctx: click.Context, check: bool, apply: bool) -> None:
    """Check or provision the GitHub labels required by the pipeline."""
    if check and apply:
        raise click.UsageError("--check and --apply are mutually exclusive")
    try:
        config = load_config()
        github = _bound_github_client(config)
        required = {
            config.github.labels.trigger: (
                _LABEL_COLORS["trigger"],
                "Machinist: run the pipeline on this issue",
            ),
            config.github.labels.approved: (
                _LABEL_COLORS["approved"],
                "Machinist: spec approved for implementation",
            ),
        }
        if apply:
            for name, (color, description) in required.items():
                github.ensure_label(name, color=color, description=description)
            click.echo("Required GitHub labels are present and up to date.")
            return
        missing = sorted(set(required) - github.label_names())
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if missing:
        click.echo("Missing required GitHub labels: " + ", ".join(missing))
        click.echo("Run 'machinist sync-labels --apply' to create them.")
        ctx.exit(1)
    click.echo("Required GitHub labels are present.")


def _doctor_fix_hints(report: DoctorReport) -> list[str]:
    """Return one remediation line per failing check, attributed to that check.

    Hints are keyed on the canonical check name rather than matched against
    rendered text, so a new check without a fix fails a test instead of
    silently degrading to generic advice.
    """
    return [
        f"  → fix ({check.name}): {hint}"
        for check in report.checks
        if check.level is CheckLevel.FAIL
        and (hint := fix_hint_for_check_name(check.name))
    ]


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable report.")
@click.option(
    "--run-gates",
    is_flag=True,
    help="Execute configured verification gates in the controller checkout.",
)
@click.pass_context
def doctor(ctx: click.Context, as_json: bool, run_gates: bool) -> None:
    """Diagnose installation readiness; gate execution is opt-in."""
    try:
        config = load_config()
        report = run_doctor(
            Path.cwd(),
            config,
            installed_version=_installed_version(),
            run_gates=run_gates,
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        for check in report.checks:
            click.echo(f"{check.level.value:<4} {check.name:<28} {check.detail}")
        if not report.ok:
            for hint in _doctor_fix_hints(report):
                click.echo(hint)
    if not report.ok:
        ctx.exit(1)


@main.command("update-check")
@click.option(
    "--timeout",
    "timeout_seconds",
    type=click.IntRange(1, 60),
    default=DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    help="Seconds to wait for the package index.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable result.")
def update_check_command(timeout_seconds: int, as_json: bool) -> None:
    """Check PyPI for a newer AgentMachinist release and print how to upgrade."""
    result = check_for_update(_installed_version(), timeout_seconds=timeout_seconds)
    if as_json:
        click.echo(json.dumps(result.as_dict(), sort_keys=True))
    else:
        for line in result.report_lines():
            click.echo(line)
        notice = _workflow_drift_notice()
        if notice is not None:
            click.echo("")
            click.echo(notice)
    if result.status is UpdateStatus.UNKNOWN:
        raise click.exceptions.Exit(1)


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
    click.echo(
        f"Requested approval for PR #{pr.number} at {pr.head_sha[:12]}. "
        "The approval workflow will verify the current head and record Evidence."
    )


@main.command()
@click.argument("issue_number", type=click.IntRange(min=1))
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
        cancellation = CancellationStore(Path(".machinist/runs"))
        observed_cancellation = cancellation.get(issue_number)
        selected = (
            lifecycle.record(issue_number, Phase(phase))
            if phase is not None
            else lifecycle.latest(issue_number)
        )
        if resume and selected is not None and selected.phase is not Phase.EXECUTE:
            raise LifecycleError("--resume is available only for the Execute phase")
        record = lifecycle.retry(issue_number, Phase(phase) if phase else None)
        cancellation.clear_if_matches(issue_number, observed_cancellation)
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
            elif record.phase is Phase.EXECUTE:
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
                pr = _run_review_task(
                    issue_number,
                    config,
                    lifecycle,
                    repo_root=repo_root,
                    cancellation=cancellation,
                )
                click.echo(
                    f"PR #{pr.number} passed independent review and is ready: {pr.url}"
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
        action = "Revising" if revise else "Starting"
        click.echo(f"{action} Spec Task Run for issue #{issue_number}...")
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

    drift_notice = _workflow_drift_notice()
    if drift_notice is not None:
        click.echo(drift_notice)

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
        click.echo(f"Dispatching Spec Task Run for issue #{issue_number}...")
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
        click.echo(f"Dispatching Execute Task Run for issue #{issue_number}...")
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
        if not config.review.enabled:
            _notify_pr_ready(config, issue_number, pr.number)
        return pr

    def dispatch_review(issue_number: int):
        click.echo(f"Dispatching Review Task Run for issue #{issue_number}...")
        pr = _run_review_task(
            issue_number,
            config,
            lifecycle,
            repo_root=repo_root,
            cancellation=cancellation_store,
            github=github,
        )
        _notify_pr_ready(config, issue_number, pr.number)
        return pr

    state = WatchState()
    try:
        while True:
            if verbose:
                click.echo(
                    f"[{time.strftime('%X')}] polling GitHub (interval {poll_interval}s)..."
                )
            poll_failure: str | None = None
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
                    run_review=dispatch_review,
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
                poll_failure = str(exc)
                result = []
            for event in result:
                click.echo(event)
            deferred_tasks = tuple(getattr(result, "deferred", ()))
            for task in deferred_tasks:
                reason = deferred_reasons.get(
                    (task.phase, task.issue_number),
                    f"per-pass limit {admission_limit} reached",
                )
                click.echo(
                    f"deferred: {task.phase} for issue #{task.issue_number}: {reason}"
                )
            if deferred_tasks:
                attempted = len(getattr(result, "attempted", ()))
                click.echo(
                    f"Pass summary: {attempted} dispatched, "
                    f"{len(deferred_tasks)} deferred."
                )
            failures = getattr(
                result,
                "failures",
                tuple(event for event in result if event.startswith("error:")),
            )
            failure_count = len(failures) + int(poll_failure is not None)
            try:
                write_watcher_heartbeat(
                    repo_root,
                    attempted=len(getattr(result, "attempted", ())),
                    deferred=len(deferred_tasks),
                    failures=failure_count,
                    interval_seconds=poll_interval,
                )
            except ServiceError as exc:
                if once:
                    raise click.ClickException(str(exc)) from exc
                click.echo(f"watcher health error: {exc}", err=True)
            if once:
                if not result and not deferred_tasks:
                    click.echo("Nothing to do.")
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
            observed_cancellation = cancellations.get(issue_number)
            prior = lifecycle.record(issue_number, Phase.EXECUTE)
            if prior and prior.status in {
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.ABANDONED,
            }:
                lifecycle.retry(issue_number, Phase.EXECUTE)
                cancellations.clear_if_matches(issue_number, observed_cancellation)
        if cancellations.requested(issue_number):
            raise LifecycleError(
                f"issue #{issue_number} has a cancellation request; "
                f"run 'machinist cancel {issue_number} --clear' before starting"
            )
        click.echo(f"Starting Execute Task Run for issue #{issue_number}...")
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
    if config.review.enabled:
        click.echo(
            f"PR #{pr.number} implemented; independent review is pending. "
            f"{pr.url}\nNext: machinist review {issue_number}"
        )
    else:
        click.echo(f"PR #{pr.number} implemented and ready for review: {pr.url}")
        _notify_pr_ready(config, issue_number, pr.number)


@main.command()
@click.argument("issue_number", type=click.IntRange(min=1))
def review(issue_number: int) -> None:
    """Independently review ISSUE_NUMBER's implemented draft pull request."""
    repo_root = Path.cwd()
    runs_dir = repo_root / ".machinist/runs"
    try:
        config = load_config()
        lifecycle = TaskLifecycle(runs_dir, repo_root=repo_root)
        pr = _run_review_task(
            issue_number,
            config,
            lifecycle,
            repo_root=repo_root,
            cancellation=CancellationStore(runs_dir, repo_root=repo_root),
        )
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"PR #{pr.number} passed independent review and is ready: {pr.url}")
    _notify_pr_ready(config, issue_number, pr.number)


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
        click.echo(f"Starting amendment Task Run for issue #{issue_number}...")
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
    if config.review.enabled:
        click.echo(
            f"PR #{pr.number} amended; independent review is pending. "
            f"Next: machinist review {issue_number}"
        )
    else:
        click.echo(f"PR #{pr.number} amended and ready for review: {pr.url}")
        _notify_pr_ready(config, issue_number, pr.number)


def _fresh_attempt(claim) -> int | None:
    """Keep the first-run path stable and isolate every later attempt."""
    attempt = getattr(claim, "attempt", 1)
    return attempt if attempt > 1 else None


def _run_review_task(
    issue_number: int,
    config: MachinistConfig,
    lifecycle: TaskLifecycle,
    *,
    repo_root: Path,
    cancellation: CancellationStore,
    github=None,
):
    execute = lifecycle.record(issue_number, Phase.EXECUTE)
    if execute is None or execute.status is not RunStatus.SUCCEEDED:
        raise LifecycleError(
            f"issue #{issue_number} has no successful Execute Task Run to review"
        )
    github = github or _bound_github_client(config, repo_root=repo_root)
    return lifecycle.run(
        issue_number,
        Phase.REVIEW,
        lambda claim: run_review_phase(
            issue_number,
            config,
            github=github,
            harness=_task_harness(
                config,
                Phase.REVIEW,
                issue_number,
                repo_root / ".machinist/runs",
            ),
            workspace=Workspace(repo_root=repo_root, config=config.workspace),
            execute_evidence=dict(execute.evidence),
            claim=claim,
            cancel_check=cancellation.check(issue_number),
        ),
    )


def _notify_pr_ready(config: MachinistConfig, issue: int, pr: int) -> None:
    _deliver_notification(
        config,
        NotificationEvent.PR_READY,
        "Machinist PR ready",
        f"Issue #{issue} implementation is ready in PR #{pr}",
        issue=issue,
        pr=pr,
    )


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
@click.argument("issue_number", type=click.IntRange(min=1))
@click.option("--offline", is_flag=True, help="Read only local runs and Workshops.")
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the complete JSON read model."
)
def inspect(issue_number: int, offline: bool = False, as_json: bool = False) -> None:
    """Show diagnostic and runtime history for ISSUE_NUMBER."""
    try:
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc

    config = None
    config_error = None
    try:
        config = load_config()
    except ConfigError as exc:
        if not offline:
            raise click.ClickException(str(exc)) from exc
        config_error = str(exc)

    remote_sources = {}
    if config is not None:
        ws = Workspace(repo_root=Path.cwd(), config=config.workspace)
        remote_sources["workspaces"] = lambda: _workspace_source(ws, issue_number)
    if not offline:
        assert config is not None
        github = _bound_github_client(config, repo_root=Path.cwd())
        branch = f"{config.workspace.branch_prefix}issue-{issue_number}"
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
    if config_error is not None:
        click.echo(f"  Configuration: unavailable ({config_error})")
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

    workspaces = source_map.get("workspaces")
    if workspaces is not None:
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
@click.option(
    "--watch",
    "watch_status",
    is_flag=True,
    help="Keep watching and print only changed pipeline snapshots.",
)
@click.option(
    "--interval",
    "status_interval",
    type=click.FloatRange(min=0.1),
    default=2.0,
    show_default=True,
    help="Seconds between live status reads.",
)
def status(
    verbose: bool = False,
    local_only: bool = False,
    as_json: bool = False,
    all_repositories: bool = False,
    registry: Path = DEFAULT_REGISTRY_PATH,
    watch_status: bool = False,
    status_interval: float = 2.0,
) -> None:
    """Show the pipeline state of machinist-managed issues and PRs."""
    if watch_status:
        if local_only or all_repositories:
            raise click.UsageError("--watch cannot be combined with --local or --all")
        _watch_status(status_interval, as_json=as_json)
        return
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
        lifecycle = TaskLifecycle(Path(".machinist/runs"))
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc
    if local_only:
        try:
            report = build_run_report(lifecycle)
        except _MACHINIST_ERRORS as exc:
            raise click.ClickException(str(exc)) from exc
        if as_json:
            click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
            return
        for line in summarize_run_report(report, lifecycle=lifecycle):
            click.echo(line)
        return

    try:
        config = load_config()
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
        for line in summarize_run_report(report, lifecycle=lifecycle):
            click.echo(line)
        return
    rows = pipeline_source.data
    if not rows:
        if report.current or report.history or report.corrupt:
            click.echo("No open GitHub pipeline items. Local Task Run activity:")
            for line in summarize_run_report(report, lifecycle=lifecycle):
                click.echo(line)
        else:
            click.echo(
                f"No machinist activity: no open '{config.github.labels.trigger}' issues "
                f"and no open '{config.workspace.branch_prefix}*' PRs."
            )
        return
    for row in rows:
        kind = "issue" if row["kind"] == "issue" else "PR"
        identity = f"#{row['number']}"
        if kind == "PR" and row["issue_number"] is not None:
            identity += f" · issue #{row['issue_number']}"
        click.echo(f"{kind:<5} {identity:<20} {row['state']:<20} {row['title']}")
        click.echo(f"      {row['url']}")
        next_action = next_action_for_status(
            StatusRow(
                kind=row["kind"],
                number=row["number"],
                title=row["title"],
                state=row["state"],
                url=row["url"],
                issue_number=row["issue_number"],
            )
        )
        if next_action is not None:
            click.echo(f"      Next: {next_action}")
        if verbose and row["issue_number"] is not None:
            rec = lifecycle.latest(row["issue_number"])
            if rec and rec.error:
                click.echo(f"      Error: {rec.error}")
            if ws is not None:
                targets = ws.list_task_workspaces(f"issue-{row['issue_number']}")
                for target_ws in targets:
                    click.echo(f"      Workspace: {target_ws}")

    visible_issues = {
        row["issue_number"] for row in rows if row["issue_number"] is not None
    }
    hidden_recovery = tuple(
        record
        for record in report.current
        if record.issue not in visible_issues
        and record.status is not RunStatus.SUCCEEDED
    )
    if hidden_recovery:
        click.echo("Local recovery not represented by an open GitHub item:")
        recovery_report = RunReport(
            issue=None,
            current=hidden_recovery,
            history=tuple(
                record
                for record in report.history
                if record.issue in {item.issue for item in hidden_recovery}
            ),
        )
        for line in summarize_run_report(recovery_report, lifecycle=lifecycle):
            click.echo(line)


def _watch_status(interval: float, *, as_json: bool) -> None:
    repo_root = Path.cwd()
    try:
        config = load_config()
        lifecycle = TaskLifecycle(repo_root / ".machinist/runs", repo_root=repo_root)
        github = _bound_github_client(config, repo_root=repo_root)

        def load_rows() -> list[dict]:
            return [
                _status_row_dict(row)
                for row in pipeline_status(config, github, lifecycle=lifecycle)
            ]

        first = True
        for snapshot in iter_status_snapshots(
            load_rows,
            interval_seconds=interval,
        ):
            if as_json:
                click.echo(json.dumps(snapshot.to_dict(), sort_keys=True))
            else:
                if not first and sys.stdout.isatty():
                    click.clear()
                _render_status_snapshot(snapshot)
            first = False
    except KeyboardInterrupt:
        click.echo("status watch stopped.")
    except _MACHINIST_ERRORS as exc:
        raise click.ClickException(str(exc)) from exc


def _render_status_snapshot(snapshot: StatusSnapshot) -> None:
    click.echo(f"Pipeline at {snapshot.observed_at}")
    if not snapshot.rows:
        click.echo("  No open pipeline items.")
        return
    for row in snapshot.rows:
        kind = "issue" if row["kind"] == "issue" else "PR"
        click.echo(f"  {kind} #{row['number']}: {row['state']} · {row['title']}")
        status_row = StatusRow(**row)
        action = next_action_for_status(status_row)
        if action:
            click.echo(f"    Next: {action}")


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
    for line in summarize_run_report(report, lifecycle=lifecycle):
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


def _active_task_runs() -> list[dict[str, object]]:
    try:
        lifecycle = TaskLifecycle(Path(".machinist/runs"), repo_root=Path.cwd())
        records = lifecycle.inventory().records
        return [
            {
                "issue": record.issue,
                "phase": record.phase.value,
                "attempt": record.attempt,
                "stage": record.evidence.get("current_stage"),
            }
            for record in records
            if record.status is RunStatus.RUNNING and lifecycle.claim_held(record.issue)
        ]
    except LifecycleError as exc:
        raise click.ClickException(
            f"cannot determine whether Task Runs are active: {exc}"
        ) from exc


def _require_idle_service(action: str, *, force: bool) -> None:
    active = _active_task_runs()
    if force or not active:
        return
    tasks = ", ".join(
        f"issue #{item['issue']} {item['phase']} attempt {item['attempt']}"
        for item in active
    )
    raise click.ClickException(
        f"refusing to {action} the watcher while Task Runs are active: {tasks}. "
        f"Wait for them to finish or rerun with --force."
    )


@service_command.command("install")
@click.option(
    "--force",
    is_flag=True,
    help="Replace the service even if a Task Run currently holds a Claim.",
)
def service_install(force: bool) -> None:
    """Install, register, and immediately start the repository watcher."""
    _require_idle_service("replace", force=force)
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
@click.option(
    "--force",
    is_flag=True,
    help="Restart even if a Task Run currently holds a Claim.",
)
def service_restart(force: bool) -> None:
    """Restart the watcher process immediately."""
    _require_idle_service("restart", force=force)
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
@click.option(
    "--force",
    is_flag=True,
    help="Stop even if a Task Run currently holds a Claim.",
)
def service_stop(force: bool) -> None:
    """Stop the watcher while preserving its plist and logs."""
    _require_idle_service("stop", force=force)
    service = _launchd_service()
    try:
        service.stop()
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Stopped {service.label}")


@service_command.command("status")
@click.option("--json", "as_json", is_flag=True)
def service_status(as_json: bool) -> None:
    """Show launchd registration, watcher health, and active Task Runs."""
    service = _launchd_service()
    try:
        status = service.status()
        heartbeat = read_watcher_heartbeat(Path.cwd())
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    active = _active_task_runs()
    heartbeat_payload = heartbeat.as_dict() if heartbeat is not None else None
    age_seconds = round(heartbeat.age_seconds, 1) if heartbeat is not None else None
    if not status.loaded:
        health = "stopped"
    elif heartbeat is None:
        health = "waiting_for_first_poll"
    elif heartbeat.age_seconds > max(30, heartbeat.interval_seconds * 3):
        health = "stale"
    elif heartbeat.failures:
        health = "degraded"
    else:
        health = "healthy"
    payload = {
        "label": status.label,
        "installed": status.installed,
        "loaded": status.loaded,
        "returncode": status.returncode,
        "output": status.output,
        "error": status.error,
        "plist": str(service.plist_path),
        "logs": [str(path) for path in service.log_paths],
        "health": health,
        "heartbeat": heartbeat_payload,
        "heartbeat_age_seconds": age_seconds,
        "active_task_runs": active,
    }
    if as_json:
        click.echo(json.dumps(payload))
        return
    state = "loaded/scheduled" if status.loaded else "not loaded"
    installed = "installed" if status.installed else "not installed"
    click.echo(f"{status.label}: {state}, {installed}")
    click.echo(f"  health: {health}")
    if heartbeat is not None:
        click.echo(
            f"  last poll: {heartbeat.polled_at} ({age_seconds:.1f}s ago; "
            f"{heartbeat.attempted} dispatched, {heartbeat.deferred} deferred, "
            f"{heartbeat.failures} failed)"
        )
    if active:
        click.echo("  active Task Runs:")
        for item in active:
            stage = f" · {item['stage']}" if item["stage"] else ""
            click.echo(
                f"    issue #{item['issue']} {item['phase']} "
                f"attempt {item['attempt']}{stage}"
            )
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
@click.option(
    "--force",
    is_flag=True,
    help="Uninstall even if a Task Run currently holds a Claim.",
)
def service_uninstall(force: bool) -> None:
    """Stop and remove the watcher plist while preserving logs."""
    _require_idle_service("uninstall", force=force)
    service = _launchd_service()
    try:
        removed = service.uninstall()
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    if removed:
        click.echo(f"Uninstalled {service.label}; logs retained at {service.logs_dir}")
    else:
        click.echo(f"Service is not installed; logs retained at {service.logs_dir}")
