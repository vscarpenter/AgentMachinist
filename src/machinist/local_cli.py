"""Foreground commands for local Tasks and explicit forge publication."""

from __future__ import annotations

import getpass
import json
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import click

from machinist.cancellation import CancellationError
from machinist.config import ConfigError, MachinistConfig
from machinist.forge import (
    ForgeError,
    GitHubForgeClient,
    normalize_host,
    normalize_repository,
)
from machinist.gitlab import GitLabClient
from machinist.harness import HarnessError
from machinist.init_wizard import HarnessChoice
from machinist.lifecycle import LifecycleError, Phase
from machinist.local_setup import (
    ensure_local_config,
    find_repository_root,
    load_local_config,
)
from machinist.local_tasks import LocalTaskError, LocalTaskStore
from machinist.local_workflow import LocalWorkflow, LocalWorkflowError
from machinist.managed_paths import ManagedPathError
from machinist.phases.execute import ExecutePhaseError
from machinist.phases.local import LocalPhaseError
from machinist.process import ProcessSupervisionError
from machinist.publication import PublicationError, origin_target, publish_task
from machinist.runtime_paths import RuntimePathError, read_text_file
from machinist.verification import VerificationError
from machinist.workspace import WorkspaceError

_TASK_ID = re.compile(r"T[1-9][0-9]*")
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_MAX_BODY_BYTES = 50_000


def _workflow(
    config: MachinistConfig, root: Path, *, progress: bool = True
) -> LocalWorkflow:
    return LocalWorkflow(
        config, repo_root=root, progress=click.echo if progress else None
    )


@contextmanager
def local_errors() -> Iterator[None]:
    try:
        yield
    except (
        ConfigError,
        LocalTaskError,
        LocalWorkflowError,
        WorkspaceError,
        LifecycleError,
        CancellationError,
        ForgeError,
        PublicationError,
        HarnessError,
        RuntimePathError,
        OSError,
        ValueError,
        LocalPhaseError,
        ExecutePhaseError,
        ManagedPathError,
        VerificationError,
        ProcessSupervisionError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc


def validate_task_id(value: str) -> str:
    if _TASK_ID.fullmatch(value) is None:
        raise click.UsageError("local Task identifier must look like T1")
    return value


def read_body_file(value: str, *, max_bytes: int = _MAX_BODY_BYTES) -> str:
    """Read bounded UTF-8 body input; '-' deliberately consumes standard input."""
    try:
        if value == "-":
            text = sys.stdin.read(max_bytes + 1)
        else:
            text = read_text_file(Path(value).expanduser(), max_bytes=max_bytes)
    except (OSError, UnicodeError, RuntimePathError) as exc:
        raise click.ClickException(f"cannot read task body: {exc}") from exc
    if len(text.encode("utf-8")) > max_bytes:
        raise click.ClickException(f"task body exceeds {max_bytes} bytes")
    if not text.strip() or "\x00" in text:
        raise click.ClickException(
            "task body must be nonempty UTF-8 text without NUL bytes"
        )
    return text


def _render_status(
    payload: dict[str, Any], *, as_json: bool = False, show_spec: bool = False
) -> None:
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo(f"{payload['id']}: {payload['title']}")
    click.echo(f"State: {payload['state']}")
    if show_spec and payload.get("spec"):
        click.echo("\n" + payload["spec"].rstrip() + "\n")
    for label, key in (
        ("Spec SHA", "spec_sha"),
        ("Candidate", "candidate_sha"),
        ("Review report", "report"),
    ):
        if payload.get(key):
            click.echo(f"{label}: {payload[key]}")
    if payload["state"] == "awaiting approval":
        click.echo("Read the Spec before approving it.")
    elif payload["state"] == "ready to integrate":
        click.echo("Inspect the Review report and candidate diff before integrating.")
    if payload.get("next_action"):
        click.echo(f"Next: {payload['next_action']}")
    if payload["state"] == "integrated":
        click.echo(
            "Optional: share this candidate through your chosen forge:\n"
            f"  machinist publish {payload['id']} --provider github\n"
            "Or, for GitLab:\n"
            f"  machinist publish {payload['id']} --provider gitlab"
        )


def _existing_workflow() -> LocalWorkflow:
    root = find_repository_root(Path.cwd())
    return _workflow(load_local_config(root), root)


def approve_local(task_id: str, spec_sha: str) -> None:
    validate_task_id(task_id)
    if _SHA.fullmatch(spec_sha) is None:
        raise click.UsageError(
            "--spec-sha must be the full 40-character Spec SHA shown by start/status"
        )
    with local_errors():
        workflow = _existing_workflow()
        task = workflow.approve(
            task_id, expected_sha=spec_sha.lower(), actor=getpass.getuser()
        )
        _render_status(workflow.status(task.id))


def retry_local(task_id: str, phase: str | None, *, resume: bool) -> None:
    validate_task_id(task_id)
    if phase is None:
        raise click.UsageError("local retries require --phase spec, execute, or review")
    with local_errors():
        workflow = _existing_workflow()
        task = workflow.retry(task_id, phase=Phase(phase), resume=resume)
        _render_status(workflow.status(task.id), show_spec=phase == "spec")


def amend_local(task_id: str, feedback: str) -> None:
    validate_task_id(task_id)
    with local_errors():
        workflow = _existing_workflow()
        task = workflow.amend(task_id, feedback)
        _render_status(workflow.status(task.id), show_spec=True)


def cancel_local(task_id: str, reason: str, *, clear: bool) -> None:
    validate_task_id(task_id)
    with local_errors():
        workflow = _existing_workflow()
        workflow.cancel(task_id, reason, clear=clear)
        click.echo(f"{task_id}: cancellation {'cleared' if clear else 'requested'}.")


def has_local_configuration() -> bool:
    """Route default status only when this checkout has adopted the local flow."""
    try:
        root = find_repository_root(Path.cwd())
    except (ConfigError, OSError, RuntimeError, ValueError):
        return False
    return (root / ".machinist/runs/local/config.yaml").is_file()


def local_status(
    task_id: str | None, *, as_json: bool, watch: bool = False, interval: float = 2.0
) -> None:
    if task_id is not None:
        validate_task_id(task_id)
    with local_errors():
        root = find_repository_root(Path.cwd())
        config = load_local_config(root)
        previous: str | None = None
        try:
            while True:
                workflow = _workflow(config, root, progress=False)
                identifiers = (
                    [task_id]
                    if task_id
                    else [task.id for task in LocalTaskStore(root).list()]
                )
                snapshots = [workflow.status(identifier) for identifier in identifiers]
                payload = snapshots[0] if task_id else {"tasks": snapshots}
                current = json.dumps(payload, sort_keys=True)
                if current != previous:
                    if as_json and watch:
                        click.echo(current)
                    elif task_id:
                        _render_status(payload, as_json=as_json, show_spec=True)
                    elif as_json:
                        click.echo(json.dumps(payload, indent=2, sort_keys=True))
                    elif snapshots:
                        for snapshot in snapshots:
                            _render_status(snapshot)
                    else:
                        click.echo(
                            "No local Tasks. Start one with machinist start OBJECTIVE."
                        )
                    previous = current
                if not watch:
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            return


def _forge_client(provider: str, repository: str, host: str):
    if provider == "github":
        return GitHubForgeClient(repository, host=host)
    return GitLabClient(repository, host=host)


def parse_issue_url(
    url: str, *, provider: str | None, host: str | None
) -> tuple[str, str, str, int]:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ForgeError("invalid issue URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ForgeError(
            "issue URL must use HTTPS without credentials, query, or fragment"
        )
    bound_host = normalize_host(parsed.netloc)
    if host is not None and normalize_host(host) != bound_host:
        raise ForgeError("--host must match the issue URL host")
    resolved_provider = provider or (
        "gitlab" if "/-/issues/" in parsed.path else "github"
    )
    pattern = (
        r"/(.+)/-/issues/([1-9][0-9]*)"
        if resolved_provider == "gitlab"
        else r"/([^/]+/[^/]+)/issues/([1-9][0-9]*)"
    )
    match = re.fullmatch(pattern, parsed.path)
    if match is None:
        raise ForgeError(f"issue URL does not identify a {resolved_provider} issue")
    repository = normalize_repository(match.group(1))
    return resolved_provider, repository, bound_host, int(match.group(2))


@click.command("start")
@click.argument("objective", required=False)
@click.option(
    "--body-file", help="Read task detail from a UTF-8 file, or '-' for stdin."
)
@click.option(
    "--harness",
    "harness_name",
    type=HarnessChoice(),
    help="Use an installed full-pipeline Harness.",
)
@click.option(
    "--test-cmd", help="Required verification command for this local repository."
)
@click.option("--from-issue", help="Explicitly import a GitHub or GitLab issue URL.")
@click.option(
    "--provider",
    type=click.Choice(["github", "gitlab"]),
    help="Provider for issue import.",
)
@click.option("--host", help="Expected host for issue import; must match its URL.")
def start_command(
    objective: str | None,
    body_file: str | None,
    harness_name: str | None,
    test_cmd: str | None,
    from_issue: str | None,
    provider: str | None,
    host: str | None,
) -> None:
    """Create a local Task, generate its Spec, and stop for exact-SHA Approval."""
    if from_issue and (objective or body_file):
        raise click.UsageError(
            "--from-issue cannot be combined with OBJECTIVE or --body-file"
        )
    if not from_issue and (provider or host):
        raise click.UsageError("--provider and --host require --from-issue")
    if not from_issue and (objective is None or not objective.strip()):
        raise click.UsageError("provide an OBJECTIVE or --from-issue URL")
    with local_errors():
        source_target = (
            parse_issue_url(from_issue, provider=provider, host=host)
            if from_issue
            else None
        )
        root = find_repository_root(Path.cwd())
        first_setup = not (root / ".machinist/runs/local/config.yaml").is_file()
        config = ensure_local_config(
            root, harness_name=harness_name, test_command=test_cmd
        )
        if first_setup:
            click.echo(
                "Verification runs in an isolated Workshop. Include dependency setup "
                "in --test-cmd when needed, for example 'npm ci && npm test'."
            )
        body = (
            read_body_file(body_file, max_bytes=config.limits.max_issue_body_chars)
            if body_file
            else ""
        )
        workflow = _workflow(config, root)
        if source_target:
            source_provider, repository, source_host, number = source_target
            issue = _forge_client(source_provider, repository, source_host).get_issue(
                number
            )
            task = workflow.start(
                issue.title,
                issue.body,
                source={
                    "provider": issue.provider,
                    "host": issue.host,
                    "repository": issue.repository,
                    "number": issue.number,
                    "url": issue.url,
                },
            )
        else:
            assert objective is not None
            task = workflow.start(objective, body)
        _render_status(workflow.status(task.id), show_spec=True)


@click.command("continue")
@click.argument("task_id")
def continue_command(task_id: str) -> None:
    """Continue the next eligible local Phase; never bypass human Approval."""
    validate_task_id(task_id)
    with local_errors():
        workflow = _existing_workflow()
        task = workflow.continue_task(task_id)
        _render_status(workflow.status(task.id), show_spec=True)


@click.command("integrate")
@click.argument("task_id")
def integrate_command(task_id: str) -> None:
    """Explicitly integrate the reviewed local candidate into its original base."""
    validate_task_id(task_id)
    with local_errors():
        workflow = _existing_workflow()
        task = workflow.integrate(task_id)
        _render_status(workflow.status(task.id))


@click.command("publish")
@click.argument("task_id")
@click.option("--provider", required=True, type=click.Choice(["github", "gitlab"]))
@click.option("--host", help="Expected publication host; must match Git origin.")
def publish_command(task_id: str, provider: str, host: str | None) -> None:
    """Publish a reviewed local Task to the explicitly selected forge."""
    validate_task_id(task_id)
    with local_errors():
        workflow = _existing_workflow()
        origin_host, repository = origin_target(workflow.workspace.origin_url())
        if host is not None and normalize_host(host) != origin_host:
            raise ForgeError("--host does not match the Git origin host")
        forge = _forge_client(provider, repository, origin_host)
        task = publish_task(
            task_id,
            store=workflow.store,
            workspace=workflow.workspace,
            forge=forge,
            lifecycle=workflow.lifecycle,
        )
        assert task.publication is not None
        click.echo(f"Published {task.id}: {task.publication['url']}")


def register_local_commands(group: click.Group) -> None:
    for command in (
        start_command,
        continue_command,
        integrate_command,
        publish_command,
    ):
        group.add_command(command)
