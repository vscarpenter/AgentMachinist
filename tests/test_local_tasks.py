"""Durable local Task identity, concurrent updates and unsafe runtime paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from machinist.local_tasks import (
    LocalTaskConflict,
    LocalTaskError,
    LocalTaskNotFound,
    LocalTaskStore,
)

BASE_SHA = "a" * 40
SPEC_SHA = "b" * 40


def create(store, **changes):
    args = dict(
        title="Add export",
        body="## Acceptance criteria\n- Export a text file.",
        base_branch="main",
        base_sha=BASE_SHA,
        branch_prefix="agent/",
    )
    args.update(changes)
    return store.create(**args)


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return LocalTaskStore(root)


def test_fresh_reads_do_not_create_runtime_state(store):
    assert store.list() == ()
    with pytest.raises(LocalTaskNotFound, match="T1"):
        store.get("T1")
    assert not (store.repo_root / ".machinist").exists()


def test_create_roundtrip_and_monotonic_allocation_survive_task_deletion(store):
    first = create(store, source={"kind": "gitlab", "issue": 51})
    assert first.id == "T1"
    assert first.number == 1
    assert first.repository == str(store.repo_root)
    assert first.branch == "agent/task-1"
    assert first.revision == 1
    assert first.created_at == first.updated_at
    assert store.get(1) == first
    assert store.get("T1") == first
    assert store.list() == (first,)
    (store.tasks_dir / "T1.json").unlink()
    reopened = LocalTaskStore(store.repo_root)
    assert create(reopened).id == "T2"


def test_parallel_allocations_have_unique_ids(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = list(
            pool.map(lambda _: create(LocalTaskStore(store.repo_root)), range(12))
        )
    assert sorted(task.number for task in tasks) == list(range(1, 13))
    assert [task.number for task in store.list()] == list(range(1, 13))


def test_update_uses_compare_and_swap_and_preserves_identity(store):
    first = create(store)
    updated = store.update(first, spec_sha=SPEC_SHA, approval={"spec_sha": SPEC_SHA})
    assert updated.revision == 2
    assert updated.spec_sha == SPEC_SHA
    assert updated.created_at == first.created_at
    with pytest.raises(LocalTaskConflict, match="changed"):
        store.update(first, title="Stale overwrite")
    with pytest.raises(LocalTaskError, match="immutable"):
        store.update(updated, number=99)
    assert store.get(1) == updated


def test_in_memory_dict_mutation_does_not_silently_replace_stored_data(store):
    task = create(store, source={"kind": "gitlab"})
    task.source["kind"] = "different"
    updated = store.update(task, title="Explicit title change")
    assert updated.source == {"kind": "gitlab"}


def test_task_claim_is_nonblocking_independent_and_returns_fresh_task(store):
    task = create(store)
    with store.claim(task.id) as claimed:
        assert claimed == task
        updated = store.update(claimed, title="Changed while operation is claimed")
        with pytest.raises(LocalTaskConflict, match="active operation"):
            with LocalTaskStore(store.repo_root).claim(task.id):
                pytest.fail("second operation entered")
        other = create(store)
        with store.claim(other.id):
            pass
    with store.claim(task.id) as claimed:
        assert claimed == updated


def test_claim_releases_on_operation_failure(store):
    task = create(store)
    with pytest.raises(ValueError, match="operation failed"):
        with store.claim(task.id):
            raise ValueError("operation failed")
    with store.claim(task.id):
        pass


def test_report_save_checks_revision_and_roundtrips(store):
    task = create(store)
    assert store.read_report(task.id) is None
    path = store.save_report(task, "# Review\nNo findings.\n")
    assert path == store.report_path(task)
    assert store.read_report(task.number) == "# Review\nNo findings.\n"
    store.update(task, candidate_sha=SPEC_SHA)
    with pytest.raises(LocalTaskConflict):
        store.save_report(task, "stale report")
    assert store.read_report(task.id) == "# Review\nNo findings.\n"


@pytest.mark.parametrize(
    "identifier", [True, 0, -1, "1", "T0", "T01", "t1", "../T1", "T1.json"]
)
def test_task_id_requires_positive_integer_or_explicit_local_id(store, identifier):
    with pytest.raises(LocalTaskError, match="Task identifier"):
        store.get(identifier)


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"body": ""},
        {"base_sha": "a" * 39},
        {"base_sha": "G" * 40},
        {"base_branch": "../main"},
        {"base_branch": "-main"},
        {"branch_prefix": "../../"},
        {"branch_prefix": "agent"},
        {"source": {"base_sha": "short"}},
        {"source": {"bad": float("nan")}},
    ],
)
def test_invalid_task_input_is_rejected_without_consuming_id(store, changes):
    with pytest.raises(LocalTaskError):
        create(store, **changes)
    assert create(store).id == "T1"


@pytest.mark.parametrize(
    "changes",
    [
        {"spec_sha": "short"},
        {"candidate_sha": 100},
        {"approval": {"spec_sha": "short"}},
        {"integration": {"candidate_sha": "short"}},
        {"publication": []},
        {"unrecognized": True},
    ],
)
def test_invalid_update_is_rejected_without_replacing_record(store, changes):
    task = create(store)
    with pytest.raises(LocalTaskError):
        store.update(task, **changes)
    assert store.get(task.id) == task


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("number", 2),
        ("revision", True),
        ("base_sha", "short"),
        ("repository", "/different/repo"),
        ("created_at", "yesterday"),
    ],
)
def test_corrupt_record_fields_fail_closed(store, field, value):
    task = create(store)
    path = store.tasks_dir / "T1.json"
    payload = json.loads(path.read_text())
    payload[field] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(LocalTaskError):
        store.get(task.id)
    with pytest.raises(LocalTaskError):
        store.list()


def test_unknown_fields_and_duplicate_json_keys_are_rejected(store):
    task = create(store)
    path = store.tasks_dir / "T1.json"
    original = path.read_text()
    path.write_text(original[:-2] + ', "version": 1}\n')
    with pytest.raises(LocalTaskError, match="duplicate"):
        store.get(task.id)
    payload = json.loads(original)
    payload["foreign"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(LocalTaskError, match="fields"):
        store.get(task.id)


def test_foreign_copied_runtime_cannot_gain_authority_at_new_root(store, tmp_path):
    task = create(store)
    destination = tmp_path / "copied"
    shutil.copytree(store.repo_root, destination)
    copied = LocalTaskStore(destination)
    with pytest.raises(LocalTaskError, match="repository"):
        copied.get(task.id)
    with pytest.raises(LocalTaskError):
        create(copied)
    with pytest.raises(LocalTaskError):
        copied.update(replace(task, repository=str(destination)), title="copied")


@pytest.mark.parametrize("missing", ["index.json", "identity.json"])
def test_missing_counter_or_identity_is_corruption_not_reinitialization(store, missing):
    create(store)
    (store.tasks_dir / "T1.json").unlink()
    (store.tasks_dir / missing).unlink()
    with pytest.raises(LocalTaskError, match="incomplete"):
        create(LocalTaskStore(store.repo_root))


@pytest.mark.parametrize(
    "leaf",
    [
        "T1.json",
        "index.json",
        "identity.json",
        "index.lock",
        "T1-operation.lock",
        "T1-report.md",
    ],
)
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_runtime_leaf_links_are_rejected(store, tmp_path, leaf, link_kind):
    task = create(store)
    target = store.tasks_dir / leaf
    outside = tmp_path / "outside.txt"
    outside.write_text(target.read_text() if target.exists() else "untouched")
    if target.exists():
        target.unlink()
    if link_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    original = outside.read_text()
    with pytest.raises(LocalTaskError, match="symlink|hard link"):
        if leaf == "T1-operation.lock":
            with store.claim(task.id):
                pass
        elif leaf == "T1-report.md":
            store.save_report(task, "must not replace")
        elif leaf == "index.lock":
            store.update(task, title="unsafe")
        else:
            store.get(task.id)
    assert outside.read_text() == original


def test_runtime_directory_revalidated_after_construction(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.repo_root / ".machinist").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LocalTaskError, match="symlink"):
        create(store)
    assert list(outside.iterdir()) == []


def test_claim_excludes_a_separate_process(store):
    task = create(store)
    script = """
import sys
from machinist.local_tasks import LocalTaskStore, LocalTaskConflict
try:
    with LocalTaskStore(sys.argv[1]).claim('T1'):
        sys.exit(2)
except LocalTaskConflict:
    sys.exit(0)
"""
    with store.claim(task.id):
        result = subprocess.run(
            [sys.executable, "-c", script, str(store.repo_root)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr


def test_allocation_crash_never_reuses_reserved_id(store, monkeypatch):
    original = store._write_task

    def interrupted(*_args):
        raise LocalTaskError("interrupted before record")

    monkeypatch.setattr(store, "_write_task", interrupted)
    with pytest.raises(LocalTaskError, match="interrupted"):
        create(store)
    monkeypatch.setattr(store, "_write_task", original)
    assert create(store).id == "T2"
    assert not (store.tasks_dir / "T1.json").exists()


def test_foreign_record_uuid_rejected_even_if_root_text_is_changed(store, tmp_path):
    task = create(store)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_store = LocalTaskStore(other_root)
    create(other_store)
    foreign = json.loads((other_store.tasks_dir / "T1.json").read_text())
    foreign["repository"] = str(store.repo_root)
    (store.tasks_dir / "T1.json").write_text(json.dumps(foreign))
    with pytest.raises(LocalTaskError, match="repository identity"):
        store.get(task.id)


def test_amendment_fields_preserve_candidate_and_support_exact_spec_base(store):
    task = store.update(create(store), spec_sha=SPEC_SHA, candidate_sha="c" * 40)
    updated = store.update(
        task,
        feedback="Also export the title",
        spec_base_sha=task.candidate_sha,
        spec_sha=None,
        approval=None,
        review_report=None,
    )
    assert updated.feedback == "Also export the title"
    assert updated.spec_base_sha == "c" * 40
    assert updated.candidate_sha == task.candidate_sha
    assert updated.spec_sha is None


def test_cyclic_evidence_is_rejected_as_local_task_error(store):
    task = create(store)
    cyclic = {}
    cyclic["cycle"] = cyclic
    with pytest.raises(LocalTaskError, match="JSON"):
        store.update(task, review_report=cyclic)
    assert store.get(task.id) == task


def test_local_operation_claim_does_not_conflict_with_phase_claim(store):
    from machinist.lifecycle import Phase, TaskLifecycle

    task = create(store)
    lifecycle = TaskLifecycle(
        store.tasks_dir.parent,
        repo_root=store.repo_root,
    )
    with store.claim(task.id):
        assert lifecycle.run(task.number, Phase.SPEC, lambda _claim: "spec") == "spec"


def test_unallocated_record_is_rejected_after_index_rollback(store):
    task = create(store)
    path = store.tasks_dir / "index.json"
    payload = json.loads(path.read_text())
    payload["next_number"] = 1
    path.write_text(json.dumps(payload))
    with pytest.raises(LocalTaskError, match="allocated identity"):
        store.get(task.id)
    with pytest.raises(LocalTaskError, match="reuse"):
        create(store)
