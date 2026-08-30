"""Side-effect-free explanation of one Task's effective controller policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from machinist.config import HarnessPhase, MachinistConfig, harness_identifier
from machinist.lifecycle import Phase, TaskLifecycle
from machinist.phases.status import next_action_for_status, pipeline_status
from machinist.process import HARNESS_CREDENTIAL_ALLOWLIST


@dataclass(frozen=True)
class TaskExplanation:
    issue: int
    state: str
    url: str
    next_action: str | None
    profiles: dict[str, dict[str, Any]]
    instructions: dict[str, dict[str, Any]]
    verification: tuple[dict[str, Any], ...]
    workspace: dict[str, Any]
    limits: dict[str, Any]
    queue: dict[str, Any]
    credentials: dict[str, Any]
    attempts: dict[str, dict[str, Any] | None]
    cancellation: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "state": self.state,
            "url": self.url,
            "next_action": self.next_action,
            "profiles": self.profiles,
            "instructions": self.instructions,
            "verification": list(self.verification),
            "workspace": self.workspace,
            "limits": self.limits,
            "queue": self.queue,
            "credentials": self.credentials,
            "attempts": self.attempts,
            "cancellation": self.cancellation,
        }


def explain_task(
    issue: int,
    config: MachinistConfig,
    github,
    *,
    lifecycle: TaskLifecycle,
    cancellation,
    workspace=None,
) -> TaskExplanation:
    """Build an allowlisted policy explanation without changing Task state."""
    row = next(
        (
            item
            for item in pipeline_status(config, github, lifecycle=lifecycle)
            if item.issue_number == issue
        ),
        None,
    )
    if row is None:
        raise ValueError(f"issue #{issue} is not represented by the open pipeline")
    return TaskExplanation(
        issue=issue,
        state=row.state,
        url=row.url,
        next_action=next_action_for_status(row),
        profiles=_profiles(config),
        instructions=_instructions(config),
        verification=tuple(
            gate.model_dump(mode="json")
            for gate in config.resolved_verification_gates()
        ),
        workspace=_workspace_policy(config, issue, workspace),
        limits=config.limits.model_dump(mode="json"),
        queue=config.queue.model_dump(mode="json"),
        credentials=_credential_policy(config),
        attempts=_attempts(lifecycle, issue),
        cancellation=_cancellation(cancellation, issue),
    )


def _profiles(config: MachinistConfig) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for phase in HarnessPhase:
        harness = config.harness_for(phase)
        read_only = phase in {HarnessPhase.SPEC, HarnessPhase.REVIEW}
        profiles[phase.value] = {
            "enabled": phase is not HarnessPhase.REVIEW or config.review.enabled,
            "harness": harness_identifier(harness.name),
            "command": harness.command,
            "model": harness.model,
            "timeout_minutes": (
                harness.spec_timeout_minutes if read_only else harness.timeout_minutes
            ),
            "extra_args": list(harness.extra_args),
        }
    return profiles


def _instructions(config: MachinistConfig) -> dict[str, dict[str, Any]]:
    return {
        phase.value: {
            "paths": list(config.instructions.for_phase(phase).paths),
            "inline_append": config.instructions.for_phase(phase).append is not None,
        }
        for phase in HarnessPhase
    }


def _workspace_policy(config: MachinistConfig, issue: int, workspace) -> dict[str, Any]:
    retained: list[str] = []
    if workspace is not None:
        retained = [
            str(path) for path in workspace.list_task_workspaces(f"issue-{issue}")
        ]
    return {
        "root": str(config.workspace.resolved_root()),
        "strategy": config.workspace.strategy.value,
        "cleanup": config.workspace.cleanup.value,
        "branch": f"{config.workspace.branch_prefix}issue-{issue}",
        "retained": retained,
    }


def _credential_policy(config: MachinistConfig) -> dict[str, Any]:
    names = set(HARNESS_CREDENTIAL_ALLOWLIST)
    if config.github.spec_secret_env:
        names.add(config.github.spec_secret_env)
    return {
        "allowed_names": sorted(names),
        "values_included": False,
        "controller_credentials_removed": True,
        "github_authentication": "gh CLI",
    }


def _attempts(lifecycle: TaskLifecycle, issue: int) -> dict[str, dict[str, Any] | None]:
    attempts: dict[str, dict[str, Any] | None] = {}
    for phase in Phase:
        record = lifecycle.record(issue, phase)
        attempts[phase.value] = (
            None
            if record is None
            else {
                "attempt": record.attempt,
                "status": record.status.value,
                "duration_seconds": record.duration_seconds,
                "error": record.error,
            }
        )
    return attempts


def _cancellation(store, issue: int) -> dict[str, Any] | None:
    marker = store.get(issue)
    if marker is None:
        return None
    return {"requested": True, "reason": marker.reason}
