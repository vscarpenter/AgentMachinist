"""Delegation is provenance-bound authorization, never a human Approval."""

import json
from dataclasses import replace

import pytest

from machinist.authorization import (
    AuthorizationError,
    bind_delegation_spec,
    digest_config,
    make_delegation,
    validate_authorization,
    validate_delegation,
)
from machinist.config import MachinistConfig
from machinist.local_tasks import LocalTaskStore


@pytest.fixture
def delegated(tmp_path):
    store = LocalTaskStore(tmp_path)
    config = MachinistConfig()
    task = store.create(
        "Repair CSV escaping",
        "Handle records containing commas",
        "main",
        "a" * 40,
        "machinist/",
        source={"provider": "github", "repository": "team/project", "number": 7},
    )
    task = store.update(
        task,
        delegation=make_delegation(
            task, config, actor="vinny", source_event="label:42"
        ),
    )
    return store, task, config


def test_pending_delegation_binds_internal_spec_without_human_approval(delegated):
    store, task, config = delegated
    validate_delegation(task, config, require_spec=False)
    with pytest.raises(AuthorizationError, match="Spec"):
        validate_authorization(task, config)
    task = store.update(task, spec_sha="b" * 40)
    task = store.update(task, delegation=bind_delegation_spec(task, config))
    assert validate_authorization(task, config) == "b" * 40
    assert task.approval is None
    assert store.get(task.id).delegation == task.delegation
    assert bind_delegation_spec(task, config) == task.delegation


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "/elsewhere"),
        ("number", 2),
        ("base_sha", "c" * 40),
        ("title", "A different request"),
        ("body", "Expanded scope"),
        ("source", {"provider": "github", "number": 8}),
        ("feedback", "Do extra work"),
        ("spec_base_sha", "c" * 40),
    ],
)
def test_delegation_rejects_changed_task_bindings_before_spec(delegated, field, value):
    _, task, config = delegated
    with pytest.raises(AuthorizationError):
        validate_delegation(replace(task, **{field: value}), config, require_spec=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor", ""),
        ("source_event", ""),
        ("delegated_at", "2026-09-18"),
        ("config_digest", "garbage"),
        ("request_digest", "garbage"),
        ("version", True),
    ],
)
def test_delegation_rejects_malformed_record(delegated, field, value):
    _, task, config = delegated
    task = replace(task, delegation={**task.delegation, field: value})
    with pytest.raises(AuthorizationError):
        validate_delegation(task, config, require_spec=False)


def test_delegation_rejects_changed_policy_and_spec(delegated):
    store, task, config = delegated
    task = store.update(task, spec_sha="b" * 40)
    task = store.update(task, delegation=bind_delegation_spec(task, config))
    changed = config.model_copy(deep=True)
    changed.limits.max_changed_files += 1
    assert digest_config(config) != digest_config(changed)
    with pytest.raises(AuthorizationError, match="configuration"):
        validate_authorization(task, changed)
    with pytest.raises(AuthorizationError, match="Spec"):
        validate_authorization(replace(task, spec_sha="c" * 40), config)
    with pytest.raises(AuthorizationError, match="Spec"):
        bind_delegation_spec(replace(task, spec_sha="c" * 40), config)


def test_delegation_cannot_coexist_with_human_approval(delegated):
    _, task, config = delegated
    with pytest.raises(AuthorizationError, match="Approval"):
        validate_delegation(
            replace(task, approval={"actor": "fake"}), config, require_spec=False
        )


def test_historical_local_task_without_delegation_remains_readable(delegated):
    store, task, _ = delegated
    path = store.tasks_dir / f"{task.id}.json"
    payload = json.loads(path.read_text())
    del payload["delegation"]
    path.write_text(json.dumps(payload))
    assert store.get(task.id).delegation is None


@pytest.mark.parametrize("phase", ["spec", "execute", "review"])
def test_default_dispatcher_refuses_delegated_host_execution(delegated, phase):
    from machinist.dispatch import TaskDispatcher
    from machinist.lifecycle import LifecycleError, Phase

    store, task, config = delegated
    dispatcher = TaskDispatcher(config, repo_root=store.repo_root)
    with pytest.raises(LifecycleError, match="background runtime"):
        getattr(dispatcher, f"run_local_{phase}")(task, store=store)
    assert dispatcher.lifecycle.record(task.number, Phase(phase)) is None
