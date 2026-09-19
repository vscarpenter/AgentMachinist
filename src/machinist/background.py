"""Single persistent worker coordinating delegated local Tasks.

The journal owns intake and admission. TaskDispatcher still owns every Phase;
publication recovery never repeats a completed Harness invocation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from machinist.authorization import digest_config, make_delegation, validate_delegation
from machinist.background_github import BackgroundGitHubClient
from machinist.background_harness import get_background_harness
from machinist.background_runtime import ContainerRuntime
from machinist.cancellation import CancellationStore
from machinist.config import MachinistConfig, WorkspaceStrategy
from machinist.diagnostics import sanitize_diagnostic
from machinist.evidence import validate_repair_evidence
from machinist.forge import GitHubForgeClient
from machinist.harness import get_harness_descriptor
from machinist.lifecycle import Phase, RunStatus, TaskLifecycle
from machinist.local_tasks import LocalTaskStore
from machinist.local_workflow import LocalWorkflow
from machinist.publication import origin_target, publish_task
from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    atomic_write_text_file,
    open_regular_file,
    read_text_file,
    regular_file_exists,
)

_ACTIVE = frozenset({"accepted", "running", "publishing", "awaiting_ci"})
_TERMINAL = frozenset({"ready", "failed", "cancelled", "needs_attention"})
_PROTECTED = (
    ".machinist",
    ".github/workflows",
    "machinist.yaml",
    "AGENTS.md",
    "CLAUDE.md",
)


class BackgroundError(Exception):
    """Background work is unsafe, unavailable or requires operator attention."""


def background_config(config: MachinistConfig) -> MachinistConfig:
    """Resolve the stricter background policy without changing manual defaults."""
    effective = config.model_copy(deep=True)
    effective.workspace.strategy = WorkspaceStrategy.CLONE
    effective.limits.denied_paths = sorted(
        set(effective.limits.denied_paths) | set(_PROTECTED)
    )
    effective.limits.allow_test_deletions = False
    return effective


class BackgroundState:
    def __init__(self, root: Path):
        self.runtime = RuntimeDirectory.bind(
            root / ".machinist/runs/background", repo_root=root
        )

    @contextmanager
    def claim(self) -> Iterator[None]:
        self.runtime.ensure(create=True)
        fd = open_regular_file(self.runtime.path / "worker.lock", truncate=False)
        with os.fdopen(fd, "a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackgroundError(
                    "another background worker owns this repository"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, dict[str, Any]]:
        self.runtime.ensure(create=False)
        path = self.runtime.path / "state.json"
        try:
            if not regular_file_exists(path):
                return {}
            payload = json.loads(
                read_text_file(path, max_bytes=8 * 1024 * 1024),
                object_pairs_hook=_unique_json,
            )
            if (
                not isinstance(payload, dict)
                or type(payload.get("version")) is not int
                or payload["version"] != 1
            ):
                raise ValueError("unsupported background journal version")
            records = payload.get("records")
            if not isinstance(records, dict):
                raise ValueError("missing background journal records")
            for key, record in records.items():
                if (
                    not isinstance(record, dict)
                    or record.get("status") not in _ACTIVE | _TERMINAL
                ):
                    raise ValueError("invalid background journal record")
                if (
                    not isinstance(record.get("source"), dict)
                    or _event_key(record["source"]) != key
                ):
                    raise ValueError("background intake identity changed")
                if not all(
                    isinstance(record.get(name), str)
                    for name in ("title", "body", "config_digest", "message")
                ):
                    raise ValueError("invalid background snapshot")
                task_id = record.get("task_id")
                if task_id is not None and (
                    not isinstance(task_id, str)
                    or not task_id.startswith("T")
                    or not task_id[1:].isdigit()
                ):
                    raise ValueError("invalid background Task identity")
                if any(
                    type(record.get(name)) not in (int, float)
                    or not math.isfinite(record[name])
                    for name in ("admitted_at", "deadline_at")
                ):
                    raise ValueError("invalid background deadline")
            return records
        except (ValueError, TypeError, KeyError, RuntimePathError, OSError) as exc:
            raise BackgroundError(f"cannot read background journal: {exc}") from exc

    def write(self, records: dict[str, dict[str, Any]]) -> None:
        self.runtime.ensure(create=True)
        atomic_write_text_file(
            self.runtime.path / "state.json",
            json.dumps(
                {"version": 1, "records": records}, sort_keys=True, allow_nan=False
            )
            + "\n",
        )


class _DeadlineCancellation:
    def __init__(
        self,
        store: CancellationStore,
        deadline: Callable[[], float | None],
        clock: Callable[[], float],
    ):
        self.store, self.deadline, self.clock = store, deadline, clock

    def check(self, number: int) -> Callable[[], bool]:
        def check() -> bool:
            limit = self.deadline()
            return self.store.check(number)() or (
                limit is not None and self.clock() >= limit
            )

        return check


class BackgroundWorker:
    def __init__(
        self,
        config: MachinistConfig,
        *,
        repo_root: Path,
        github=None,
        forge=None,
        workflow_factory: Callable[[MachinistConfig], LocalWorkflow] | None = None,
        publisher: Callable[..., Any] = publish_task,
        base_resolver: Callable[..., str] | None = None,
        clock: Callable[[], float] = time.time,
        notify: Callable[[str], None] | None = None,
    ):
        self.config = background_config(config)
        self.root = repo_root.resolve()
        self.state = BackgroundState(self.root)
        self.clock, self.notify, self.publisher = clock, notify, publisher
        self._active: dict[str, Any] | None = None
        self._workflow_factory = workflow_factory
        self._base_resolver = base_resolver
        self._runtime = None
        self._github = github
        self._forge = forge

    def _workflow(self) -> LocalWorkflow:
        if self._workflow_factory is not None:
            workflow = self._workflow_factory(self.config)
        else:
            runtime = self._container()

            def harness_factory(phase, number):
                harness = get_background_harness(
                    self.config.harness_for(phase.value),
                    runtime=runtime,
                    phase=phase.value,
                )
                harness.cancel_check = self._cancel_store().check(number)
                return harness

            workflow = LocalWorkflow(
                self.config,
                repo_root=self.root,
                harness_factory=harness_factory,
                test_runner=runtime.gate_runner,
                delegated_execution=True,
            )
        workflow.dispatcher.cancellation = _DeadlineCancellation(
            workflow.cancellation, self._deadline, self.clock
        )
        return workflow

    def _deadline(self) -> float | None:
        if self._active is None:
            return None
        deadline = self._active["deadline_at"]
        task_id = self._active.get("task_id")
        if task_id is not None:
            lifecycle = TaskLifecycle(
                self.root / ".machinist/runs/local", repo_root=self.root
            )
            execute = lifecycle.record(int(task_id[1:]), Phase.EXECUTE)
            if (
                execute is not None
                and execute.status is RunStatus.RUNNING
                and execute.evidence.get("repair") is not None
            ):
                repair = validate_repair_evidence(execute.evidence["repair"])
                attempt = cast(dict, cast(list, repair["attempts"])[0])
                # Explicit fresh retries initially carry the previous attempt's
                # Evidence. Its repair cannot restrict this new Execute Run.
                belongs_to_run = datetime.fromisoformat(
                    attempt["started_at"]
                ) >= datetime.fromisoformat(execute.started_at)
                if belongs_to_run and repair["status"] in {"running", "verifying"}:
                    repair_deadline = repair["deadline_at"]
                    assert isinstance(repair_deadline, str)
                    # The in-container timeout must retain this shorter bound
                    # even if the controller is killed before it can poll again.
                    deadline = min(
                        deadline,
                        datetime.fromisoformat(repair_deadline).timestamp(),
                    )
        return deadline

    def _cancel_store(self) -> CancellationStore:
        return CancellationStore(
            self.root / ".machinist/runs/local", repo_root=self.root
        )

    def _cancelled(self) -> bool:
        if self._active is None or self._active.get("task_id") is None:
            return False
        return self._cancel_store().check(int(self._active["task_id"][1:]))()

    def _stop_requested(self) -> bool:
        deadline = self._deadline()
        return self._cancelled() or (deadline is not None and self.clock() >= deadline)

    def _container(self):
        if self._runtime is None:
            names = set()
            for phase in ("spec", "execute", "review"):
                descriptor = get_harness_descriptor(self.config.harness_for(phase).name)
                if descriptor.ci_spec is None:
                    raise BackgroundError(
                        "background requires a Harness with an explicit provider credential profile"
                    )
                names.add(descriptor.ci_spec.secret_env)
            self._runtime = ContainerRuntime(
                self.config.background.image,
                controller_root=self.root,
                workshop_root=self.config.workspace.resolved_root(),
                network=self.config.background.network,
                credential_names=tuple(sorted(names)),
                deadline=self._deadline,
                clock=self.clock,
                cancel_check=self._stop_requested,
            )
        return self._runtime

    def _clients(self, workflow: LocalWorkflow):
        host, repository = origin_target(workflow.workspace.origin_url())
        if self.config.github.repo and self.config.github.repo != repository:
            raise BackgroundError("configured GitHub repository does not match origin")
        if self._github is None:
            self._github = BackgroundGitHubClient(repository)
            self._github.bind_repository(repository, hostname=host)
        if self._forge is None:
            self._forge = GitHubForgeClient(repository, host=host)
        if (self._forge.host, self._forge.repository) != (host, repository) or (
            self._github.repo_host,
            self._github.repo,
        ) != (host, repository):
            raise BackgroundError(
                "background adapters do not match the controller origin"
            )
        return self._github, self._forge

    def doctor(self) -> list[str]:
        if not self.config.background.enabled:
            raise BackgroundError(
                "background is disabled; explicitly enable it in the selected configuration"
            )
        if not any(g.required for g in self.config.resolved_verification_gates()):
            raise BackgroundError(
                "background requires at least one required Verification Gate"
            )
        messages = []
        if self._workflow_factory is None:
            for phase in ("spec", "execute", "review"):
                if self.config.harness_for(phase).name != "codex":
                    raise BackgroundError(
                        "the background pilot supports Codex for all three Phases"
                    )
                descriptor = get_harness_descriptor(self.config.harness_for(phase).name)
                profile = descriptor.ci_spec
                if profile is None or not os.environ.get(profile.secret_env):
                    required = (
                        profile.secret_env
                        if profile is not None
                        else "an explicit provider credential profile"
                    )
                    raise BackgroundError(
                        f"background Harness requires {required}; interactive host login is not mounted"
                    )
            result = self._container().probe()
            if not result.ready:
                raise BackgroundError(result.detail)
            messages.append(result.detail)
        workflow = self._workflow()
        self._clients(workflow)
        if workflow.workspace.has_changes(self.root):
            raise BackgroundError(
                "background requires a clean committed controller checkout"
            )
        self.state.read()
        messages.append(
            "Repository, opt-in policy, required checks and runtime state are valid."
        )
        return messages

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "issue_number": record["source"]["issue_number"],
                **{
                    name: record.get(name)
                    for name in (
                        "task_id",
                        "status",
                        "title",
                        "message",
                        "url",
                        "admitted_at",
                        "deadline_at",
                    )
                },
            }
            for record in self.state.read().values()
        ]

    def cancel(self, task_id: str) -> None:
        records = self.state.read()
        if not any(record["task_id"] == task_id for record in records.values()):
            raise BackgroundError("Task is not owned by the background worker")
        task = LocalTaskStore(self.root).get(task_id)
        self._cancel_store().request(
            task.number, "background operator requested cancellation"
        )

    def retry(
        self, task_id: str | None = None, *, issue_number: int | None = None
    ) -> None:
        if (task_id is None) == (issue_number is None):
            raise BackgroundError("retry requires either a Task ID or --issue")
        with self.state.claim():
            records = self.state.read()
            record = next(
                (
                    r
                    for r in records.values()
                    if (task_id is not None and r["task_id"] == task_id)
                    or (
                        issue_number is not None
                        and r["source"]["issue_number"] == issue_number
                    )
                ),
                None,
            )
            if record is None or record["status"] not in {
                "failed",
                "cancelled",
                "needs_attention",
            }:
                raise BackgroundError("retry requires a stopped background Task")
            if record["config_digest"] != digest_config(self.config):
                raise BackgroundError(
                    "policy changed; restore the accepted configuration before retry"
                )
            workflow = self._workflow()
            if record["task_id"] is not None:
                task = workflow.store.get(record["task_id"])
                validate_delegation(
                    task, self.config, require_spec=task.spec_sha is not None
                )
                latest = workflow.lifecycle.latest(task.number)
                if latest is not None and latest.status in {
                    RunStatus.FAILED,
                    RunStatus.RUNNING,
                    RunStatus.CANCELLED,
                    RunStatus.ABANDONED,
                }:
                    workflow.lifecycle.retry(task.number, latest.phase)
                workflow.cancellation.clear(task.number)
            record.update(
                status="accepted",
                deadline_at=self.clock() + self.config.background.timeout_minutes * 60,
                message="Explicit retry requested",
                notified=False,
            )
            self.state.write(records)

    def run_once(self) -> list[dict[str, Any]]:
        self.doctor()
        with self.state.claim():
            records = self.state.read()
            workflow = self._workflow()
            workflow.workspace.ensure_runtime_ignored()
            github, forge = self._clients(workflow)
            if self._workflow_factory is None:
                self._container().cleanup_stale()
            # Recover accepted work before admitting anything new. Completed or
            # failed work never re-enters the Harness because a label remains.
            for record in records.values():
                if record["status"] in _ACTIVE:
                    self._process(record, records, workflow, github, forge)
                self._notify(record, records)
            if any(r["status"] in _ACTIVE for r in records.values()):
                return self.status()
            if (
                github.open_bot_pr_count(self.config.workspace.branch_prefix)
                >= self.config.background.max_open_prs
            ):
                return self.status()
            queued = github.queued_tasks(self.config.background.queue_label)
            for item in queued:
                source = {
                    "provider": "github",
                    "host": forge.host,
                    "repository": forge.repository,
                    "issue_number": item.number,
                    "url": item.url,
                    "event_id": str(item.event_id),
                    "actor": item.actor,
                    "queued_at": item.queued_at,
                }
                key = _event_key(source)
                # Re-labeling an already accepted issue is not permission to
                # abandon its history, reset a failed budget, or create a new PR.
                if key in records or any(
                    r["source"]["issue_number"] == item.number for r in records.values()
                ):
                    continue
                if len(item.body) > self.config.limits.max_issue_body_chars:
                    raise BackgroundError(
                        "queued issue exceeds the configured input limit"
                    )
                record = {
                    "task_id": None,
                    "source": source,
                    "title": item.title,
                    "body": item.body,
                    "status": "accepted",
                    "admitted_at": self.clock(),
                    "deadline_at": self.clock()
                    + self.config.background.timeout_minutes * 60,
                    "config_digest": digest_config(self.config),
                    "message": "Accepted delegated Task",
                    "notified": False,
                }
                records[key] = record
                self.state.write(records)  # Intake intent precedes Task allocation.
                self._process(record, records, workflow, github, forge)
                self._notify(record, records)
                break  # One new Task per pass, even after an immediate failure.
        return self.status()

    def _task(self, record, records, workflow, forge):
        task_id = record["task_id"]
        if task_id is None:
            matches = [t for t in workflow.store.list() if t.source == record["source"]]
            if len(matches) > 1:
                raise BackgroundError("duplicate Task identities for one intake event")
            if matches:
                task = matches[0]
            else:
                branch = forge.default_branch()
                base = (
                    self._base_resolver(workflow.workspace, branch)
                    if self._base_resolver
                    else workflow.workspace.fetch_background_base(branch)
                )
                task = workflow.store.create(
                    record["title"],
                    record["body"],
                    branch,
                    base,
                    self.config.workspace.branch_prefix,
                    source=record["source"],
                )
            record["task_id"] = task.id
            self.state.write(records)
        else:
            task = workflow.store.get(task_id)
        if task.source != record["source"] or (task.title, task.body) != (
            record["title"],
            record["body"],
        ):
            raise BackgroundError("accepted Task snapshot changed")
        if task.delegation is None:
            if task.spec_sha is not None or task.approval is not None:
                raise BackgroundError(
                    "cannot adopt a Task already using manual Approval"
                )
            with workflow.store.claim(task.id) as claimed:
                task = workflow.store.update(
                    claimed,
                    delegation=make_delegation(
                        claimed,
                        self.config,
                        actor=record["source"]["actor"],
                        source_event=record["source"]["event_id"],
                    ),
                )
        return task

    def _process(self, record, records, workflow, github, forge):
        self._active = record
        try:
            if self._cancelled():
                record.update(
                    status="cancelled", message="Cancelled; candidate retained"
                )
                return
            if self.clock() >= record["deadline_at"]:
                record.update(
                    status="needs_attention",
                    message="Task deadline exhausted; explicit retry required",
                )
                return
            if record["config_digest"] != digest_config(self.config):
                raise BackgroundError(
                    "accepted policy changed; restore it before retrying"
                )
            task = self._task(record, records, workflow, forge)
            validate_delegation(
                task, self.config, require_spec=task.spec_sha is not None
            )
            if (
                record["status"] != "awaiting_ci"
                and github.open_bot_pr_count(self.config.workspace.branch_prefix)
                >= self.config.background.max_open_prs
            ):
                existing = forge.find_change(task.branch)
                if existing is None:
                    record["message"] = (
                        "Deferred until the outstanding PR backlog is below its limit"
                    )
                    return
                if (
                    existing.host,
                    existing.repository,
                    existing.branch,
                    existing.head_sha,
                ) != (forge.host, forge.repository, task.branch, task.candidate_sha):
                    raise BackgroundError(
                        "existing PR does not belong to this exact Task candidate"
                    )
            if record["status"] != "awaiting_ci":
                record.update(status="running", message="Executing delegated Task")
                self.state.write(records)
                task = workflow.continue_task(task.id)
                if self._stop_requested():
                    raise BackgroundError("Task stopped before publication")
                if (
                    github.open_bot_pr_count(self.config.workspace.branch_prefix)
                    >= self.config.background.max_open_prs
                    and forge.find_change(task.branch) is None
                ):
                    record["message"] = (
                        "Verified candidate retained until the PR backlog is below its limit"
                    )
                    return
                record.update(
                    status="publishing", message="Publishing verified candidate"
                )
                self.state.write(records)
                # publish_task reconciles the same intent after an uncertain
                # push/create; local successful Phases remain untouched.
                result = self.publisher(
                    task.id,
                    store=workflow.store,
                    workspace=workflow.workspace,
                    lifecycle=workflow.lifecycle,
                    forge=forge,
                    draft=True,
                    cancel_check=self._stop_requested,
                )
                delivery = result.publication or {}
                if type(delivery.get("change_number")) is not int or not isinstance(
                    delivery.get("url"), str
                ):
                    raise BackgroundError(
                        "publication did not retain its change identity"
                    )
                record.update(
                    status="awaiting_ci",
                    number=delivery["change_number"],
                    url=delivery["url"],
                    candidate_sha=task.candidate_sha,
                    message="Local verification completed; waiting for required CI checks",
                )
                self.state.write(records)
            task = workflow.store.get(task.id)
            if task.candidate_sha != record.get("candidate_sha"):
                raise BackgroundError("candidate changed while waiting for CI")
            if any(
                f.get("severity") == "high"
                for f in (task.review_report or {}).get("findings", [])
            ):
                record.update(
                    status="needs_attention",
                    message="High-severity automated Review findings; PR remains draft",
                )
                return
            observation = github.observe_ci(
                task.candidate_sha, tuple(self.config.background.required_checks)
            )
            if self._stop_requested():
                record.update(
                    status="cancelled" if self._cancelled() else "needs_attention",
                    message="Task stopped before the PR ready transition",
                )
            elif observation.status == "passed":
                github.request_ready(
                    record["number"],
                    task.candidate_sha,
                    expected_branch=task.branch,
                    expected_base=task.base_branch,
                    cancel_check=self._stop_requested,
                )
                record.update(
                    status="ready",
                    message="Local verification and required CI passed; ready for human review and merge",
                )
            elif observation.status == "failed":
                record.update(status="needs_attention", message=observation.summary)
            else:
                record["message"] = observation.summary
        except Exception as exc:
            record.update(
                status="cancelled" if self._cancelled() else "failed",
                message=sanitize_diagnostic(str(exc), limit=2000),
            )
        finally:
            self.state.write(records)
            self._active = None

    def _notify(self, record, records):
        if record["status"] not in _TERMINAL or record.get("notified"):
            return
        # Persist before side effect: advisory notifications are at-most-once,
        # while status remains authoritative if a process dies before delivery.
        record["notified"] = True
        self.state.write(records)
        if self.notify is not None:
            self.notify(
                f"{record['task_id'] or 'Intake'}: {record['message']}"
                + (f" {record['url']}" if record.get("url") else "")
            )


def _event_key(source: dict[str, Any]) -> str:
    identity = [
        source[name] for name in ("host", "repository", "issue_number", "event_id")
    ]
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate background journal key")
        result[key] = value
    return result
