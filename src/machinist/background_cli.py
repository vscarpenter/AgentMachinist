"""Explicit one-shot and persistent commands for the background Task pilot."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from machinist.background import BackgroundError, BackgroundWorker
from machinist.config import MachinistConfig, load_config
from machinist.diagnostics import sanitize_diagnostic
from machinist.github import GitHubError
from machinist.local_cli import local_errors, validate_task_id
from machinist.local_setup import find_repository_root


@dataclass(frozen=True)
class _Selection:
    cwd: Path
    config_path: Path | None

    def worker(
        self, *, notifications: bool = False
    ) -> tuple[MachinistConfig, BackgroundWorker]:
        root = find_repository_root(self.cwd)
        config = load_config(self.config_path or root / "machinist.yaml")
        worker = BackgroundWorker(
            config,
            repo_root=root,
            notify=_notice if notifications else None,
        )
        return config, worker


@contextmanager
def _errors() -> Iterator[None]:
    try:
        with local_errors():
            yield
    except (click.ClickException, BackgroundError, GitHubError) as exc:
        raise click.ClickException(sanitize_diagnostic(str(exc))) from exc


def _notice(message: str) -> None:
    click.echo(sanitize_diagnostic(message, limit=4000))


def _render(records: list[dict[str, Any]]) -> None:
    if not records:
        click.echo("No background Tasks recorded.")
        return
    for record in records:
        identity = (
            record.get("task_id") or f"Issue #{record.get('issue_number', '?')} intake"
        )
        heading = f"{identity}: {record.get('status', 'unknown')}"
        if record.get("title"):
            heading += f" - {record['title']}"
        _notice(heading)
        if record.get("message"):
            _notice(f"  {record['message']}")
        if record.get("url"):
            _notice(f"  {record['url']}")


@click.group("background")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Configuration file; defaults to machinist.yaml at the repository root.",
)
@click.pass_context
def background(ctx: click.Context, config_path: Path | None) -> None:
    """Run opt-in delegated GitHub Tasks without intermediate Spec approval.

    Queue work explicitly and keep human review and merge at the PR boundary.
    Commands never install a service or enable background operation implicitly.
    """
    with _errors():
        # Preserve symlink leaves for load_config's regular-file checks. Resolving
        # an explicit path here would bypass that existing protection.
        selected = None if config_path is None else config_path.expanduser().absolute()
        ctx.obj = _Selection(Path.cwd(), selected)


@background.command("doctor")
@click.pass_obj
def doctor(selection: _Selection) -> None:
    """Check opt-in policy, runtime and repository without executing Tasks."""
    with _errors():
        _, worker = selection.worker()
        for message in worker.doctor():
            _notice(message)


@background.command("run")
@click.option("--once", is_flag=True, help="Run one worker pass, then exit.")
@click.pass_obj
def run(selection: _Selection, once: bool) -> None:
    """Process queued Tasks once or poll until interrupted.

    One-shot exit status is 1 if a recorded Task is failed, cancelled or needs
    attention. Pending CI returns 0. Persistent operation reports new terminal
    results and reloads the selected configuration on every pass.
    """
    try:
        while True:
            with _errors():
                config, worker = selection.worker(notifications=not once)
                records = worker.run_once()
            if once:
                _render(records)
                if any(
                    record.get("status") in {"failed", "cancelled", "needs_attention"}
                    for record in records
                ):
                    raise click.exceptions.Exit(1)
                return
            time.sleep(config.background.poll_interval_seconds)
    except KeyboardInterrupt:
        click.echo("Background worker stopped; durable Task records are retained.")


@background.command("status")
@click.argument("task_id", required=False)
@click.option(
    "--json", "as_json", is_flag=True, help="Print the local journal as JSON."
)
@click.pass_obj
def status(selection: _Selection, task_id: str | None, as_json: bool) -> None:
    """Read background outcomes without runtime probes or execution."""
    with _errors():
        if task_id is not None:
            validate_task_id(task_id)
        _, worker = selection.worker()
        records = worker.status()
        if task_id is not None:
            records = [record for record in records if record.get("task_id") == task_id]
            if not records:
                raise BackgroundError("Task is not owned by the background worker")
    if as_json:
        click.echo(json.dumps(records, indent=2, sort_keys=True))
    else:
        _render(records)


@background.command("cancel")
@click.argument("task_id")
@click.pass_obj
def cancel(selection: _Selection, task_id: str) -> None:
    """Request cancellation of a delegated Task such as T1."""
    validate_task_id(task_id)
    with _errors():
        _, worker = selection.worker()
        worker.cancel(task_id)
    click.echo(f"Cancellation requested for {task_id}.")


@background.command("retry")
@click.argument("task_id", required=False)
@click.option(
    "--issue",
    "issue_number",
    type=click.IntRange(min=1),
    help="Retry failed intake before a local Task ID was allocated.",
)
@click.pass_obj
def retry(selection: _Selection, task_id: str | None, issue_number: int | None) -> None:
    """Allow one stopped Task or failed issue intake to resume on the next pass."""
    if (task_id is None) == (issue_number is None):
        raise click.UsageError(
            "provide exactly one Task ID such as T1 or --issue NUMBER"
        )
    if task_id is not None:
        validate_task_id(task_id)
    with _errors():
        _, worker = selection.worker()
        if task_id is not None:
            worker.retry(task_id)
        else:
            worker.retry(issue_number=issue_number)
    target = task_id if task_id is not None else f"issue #{issue_number}"
    click.echo(f"Retry requested for {target}; work resumes on the next worker pass.")


def register_background_commands(group: click.Group) -> None:
    group.add_command(background)
