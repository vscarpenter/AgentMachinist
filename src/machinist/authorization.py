"""Repository-bound human Approval and delegated Task authorization.

Delegation records trusted intake provenance and a frozen policy, then binds the
internal Spec. It is never a manufactured human Approval. These controller-owned
records are not an authentication boundary against a hostile same-user process.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from machinist.config import MachinistConfig
from machinist.local_tasks import LocalTask

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class AuthorizationError(ValueError):
    """The Task does not have valid authorization for its exact saved inputs."""


def _digest(value: object) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def digest_config(config: MachinistConfig) -> str:
    """Hash the entire validated effective configuration, including pilot policy."""
    return _digest(config.model_dump(mode="json"))


def _request_digest(task: LocalTask) -> str:
    return _digest({"title": task.title, "body": task.body, "source": task.source})


def _identity(task: LocalTask) -> dict[str, str]:
    return {
        "repository": task.repository,
        "task_id": task.id,
        "base_sha": task.base_sha,
        "base_branch": task.base_branch,
        "branch": task.branch,
        "request_digest": _request_digest(task),
    }


def make_delegation(
    task: LocalTask,
    config: MachinistConfig,
    *,
    actor: str,
    source_event: str,
) -> dict[str, Any]:
    """Create pending delegation after trusted intake has allocated a durable Task."""
    if (
        task.delegation is not None
        or task.spec_sha is not None
        or task.candidate_sha is not None
    ):
        raise AuthorizationError(
            "delegation requires a new Task before its internal Spec"
        )
    delegation = {
        "version": 1,
        **_identity(task),
        "actor": actor,
        "source_event": source_event,
        "delegated_at": datetime.now(UTC).isoformat(),
        "config_digest": digest_config(config),
        "spec_sha": None,
    }
    from dataclasses import replace

    validate_delegation(
        replace(task, delegation=delegation), config, require_spec=False
    )
    return delegation


def validate_delegation(
    task: LocalTask,
    config: MachinistConfig | None = None,
    *,
    require_spec: bool = False,
) -> None:
    """Validate frozen delegation before paid work and, optionally, its exact Spec."""
    delegation = task.delegation
    if not isinstance(delegation, dict):
        raise AuthorizationError("Task requires a delegation record")
    if task.approval is not None:
        raise AuthorizationError("delegation cannot coexist with human Approval")
    if type(delegation.get("version")) is not int or delegation["version"] != 1:
        raise AuthorizationError("unsupported delegation version")
    if task.feedback is not None or task.spec_base_sha is not None:
        raise AuthorizationError(
            "delegated Task scope cannot be amended; queue a new Task"
        )
    if not isinstance(task.source, dict) or not task.source:
        raise AuthorizationError("delegation requires its saved source snapshot")
    if any(delegation.get(key) != value for key, value in _identity(task).items()):
        raise AuthorizationError(
            "delegation does not match this repository, Task, base, and request snapshot"
        )
    _actor_time(delegation, "delegated_at", "delegation")
    event = delegation.get("source_event")
    if not isinstance(event, str) or not event.strip():
        raise AuthorizationError("delegation must identify its source event")
    for source_key, value in (("actor", delegation["actor"]), ("event_id", event)):
        if source_key in task.source and task.source[source_key] != value:
            raise AuthorizationError(
                "delegation actor/event does not match its source snapshot"
            )
    for key in ("config_digest", "request_digest"):
        value = delegation.get(key)
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            raise AuthorizationError(f"delegation {key} must be a SHA-256 digest")
    if config is not None and delegation["config_digest"] != digest_config(config):
        raise AuthorizationError(
            "delegated configuration changed; restore the saved policy or queue a new Task"
        )
    bound = delegation.get("spec_sha")
    if bound is not None and (
        not isinstance(bound, str)
        or _FULL_SHA.fullmatch(bound) is None
        or bound != task.spec_sha
    ):
        raise AuthorizationError(
            "delegation does not match the exact internal Spec SHA"
        )
    if require_spec and (not task.spec_sha or bound != task.spec_sha):
        raise AuthorizationError("delegation must bind the exact internal Spec SHA")


def bind_delegation_spec(
    task: LocalTask, config: MachinistConfig | None = None
) -> dict[str, Any]:
    """Bind the first retained internal Spec; never silently reauthorize a new one."""
    validate_delegation(task, config)
    if not isinstance(task.spec_sha, str) or _FULL_SHA.fullmatch(task.spec_sha) is None:
        raise AuthorizationError("delegation cannot bind a missing internal Spec")
    assert task.delegation is not None
    return {**task.delegation, "spec_sha": task.spec_sha}


def validate_authorization(
    task: LocalTask, config: MachinistConfig | None = None
) -> str:
    """Return the authorized Spec SHA for either human or delegated Tasks."""
    if task.delegation is not None:
        validate_delegation(task, config, require_spec=True)
    else:
        approval = task.approval
        if (
            not task.spec_sha
            or not isinstance(approval, dict)
            or any(
                approval.get(key) != value
                for key, value in {
                    "repository": task.repository,
                    "task_id": task.id,
                    "spec_sha": task.spec_sha,
                }.items()
            )
        ):
            raise AuthorizationError(
                "Approval must identify this repository, Task and exact Spec SHA"
            )
        _actor_time(approval, "approved_at", "Approval")
    if not isinstance(task.spec_sha, str) or _FULL_SHA.fullmatch(task.spec_sha) is None:
        raise AuthorizationError("authorization requires an exact Spec SHA")
    return task.spec_sha


def authorization_evidence(task: LocalTask) -> dict[str, Any]:
    """Record authorization truthfully beside the historical approved_sha binding."""
    sha = validate_authorization(task)
    record = task.delegation if task.delegation is not None else task.approval
    assert record is not None
    result = {
        "kind": "delegation" if task.delegation is not None else "human_approval",
        "repository": task.repository,
        "task_id": task.id,
        "spec_sha": sha,
        "actor": record["actor"],
    }
    if task.delegation is not None:
        result.update(
            {
                key: record[key]
                for key in ("source_event", "config_digest", "request_digest")
            }
        )
    return result


def _actor_time(record: dict[str, Any], field: str, name: str) -> None:
    actor, timestamp = record.get("actor"), record.get(field)
    try:
        if (
            not isinstance(actor, str)
            or not actor.strip()
            or not isinstance(timestamp, str)
        ):
            raise ValueError("missing actor or time")
        if datetime.fromisoformat(timestamp).utcoffset() is None:
            raise ValueError("missing timezone")
    except ValueError as exc:
        raise AuthorizationError(f"{name} must record its actor and time") from exc
