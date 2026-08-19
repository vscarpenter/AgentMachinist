"""Repository-bound runtime state cannot escape through symlinks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from machinist.cancellation import CancellationError, CancellationStore
from machinist.lifecycle import LifecycleError, Phase, TaskLifecycle
from machinist.notification_ledger import NotificationLedger
from machinist.notify import NotificationResult, NotificationStatus
from machinist.phases.execute import _capture_harness_report
from machinist.queue_control import QueueControl, QueueControlError
from machinist.runtime_paths import RuntimePathError, resolve_runtime_dir


def _repo_and_outside(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (repo / ".machinist").mkdir(parents=True)
    outside.mkdir()
    return repo, repo / ".machinist" / "runs", outside


def _delivered() -> NotificationResult:
    return NotificationResult(
        NotificationStatus.DELIVERED,
        "desktop",
        "failure",
        "runtime-path-test",
    )


def test_runtime_resolver_creates_contained_directories_without_symlinks(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    runs = resolve_runtime_dir(
        repo / ".machinist" / "runs",
        repo_root=repo,
        create=True,
    )

    assert runs == repo / ".machinist" / "runs"
    assert runs.is_dir()
    assert not runs.is_symlink()
    assert not runs.parent.is_symlink()


@pytest.mark.parametrize("component", [".machinist", ".machinist/runs"])
def test_runtime_resolver_rejects_symlink_components(tmp_path: Path, component: str):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    target = repo / component
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimePathError, match="symlink component"):
        resolve_runtime_dir(
            repo / ".machinist" / "runs",
            repo_root=repo,
            create=True,
        )


def test_state_stores_revalidate_runs_directory_before_every_write(tmp_path: Path):
    repo, runs, outside = _repo_and_outside(tmp_path)
    queue = QueueControl(runs, repo_root=repo)
    cancellations = CancellationStore(runs, repo_root=repo)
    lifecycle = TaskLifecycle(runs, repo_root=repo)
    ledger = NotificationLedger(runs, repo_root=repo)

    runs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(QueueControlError, match="symlink component"):
        queue.pause("must stay in the repository")
    with pytest.raises(CancellationError, match="symlink component"):
        cancellations.request(42, "stop")
    with pytest.raises(LifecycleError, match="symlink component"):
        lifecycle.run(42, Phase.SPEC, lambda _claim: None)

    delivered = ledger.deliver_once("runtime-path-test", _delivered)
    assert delivered.notification is not None
    assert "symlink component" in (delivered.warning or "")
    assert list(outside.iterdir()) == []


def test_state_stores_persist_normally_in_a_valid_runtime_directory(tmp_path: Path):
    repo, runs, outside = _repo_and_outside(tmp_path)
    queue = QueueControl(runs, repo_root=repo)
    cancellations = CancellationStore(runs, repo_root=repo)
    lifecycle = TaskLifecycle(runs, repo_root=repo)
    ledger = NotificationLedger(runs, repo_root=repo)

    queue.pause("maintenance")
    cancellations.request(42, "stop after current command")
    lifecycle.run(43, Phase.SPEC, lambda _claim: None)
    notification = ledger.deliver_once("runtime-path-test", _delivered)

    assert notification.notification is not None
    assert (runs / "queue-control.json").is_file()
    assert (runs / "cancellations" / "issue-42.json").is_file()
    assert (runs / "issue-43-spec.json").is_file()
    assert (runs / "notification-ledger.json").is_file()
    assert list(outside.iterdir()) == []


def test_queue_write_rejects_directory_swap_after_validation(
    tmp_path: Path, monkeypatch
):
    repo, runs, outside = _repo_and_outside(tmp_path)
    queue = QueueControl(runs, repo_root=repo)
    original_ensure = queue._ensure_runs
    calls = 0

    def swap_after_validation(*, create: bool):
        nonlocal calls
        validated = original_ensure(create=create)
        calls += 1
        if calls == 2:
            retained = runs.with_name("runs-retained")
            runs.rename(retained)
            runs.symlink_to(outside, target_is_directory=True)
        return validated

    monkeypatch.setattr(queue, "_ensure_runs", swap_after_validation)

    with pytest.raises(QueueControlError, match="symlink component"):
        queue.pause("must not escape")

    assert list(outside.iterdir()) == []
    assert not (runs.with_name("runs-retained") / "queue-control.json").exists()


def test_fresh_runtime_state_syncs_created_directories_and_state_parent(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    runs = repo / ".machinist/runs"
    real_fsync = os.fsync
    synced_directories: list[tuple[int, int]] = []

    def recording_fsync(descriptor: int):
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced_directories.append((metadata.st_dev, metadata.st_ino))
        return real_fsync(descriptor)

    monkeypatch.setattr("machinist.runtime_paths.os.fsync", recording_fsync)

    QueueControl(runs, repo_root=repo).pause("durable")

    assert len(synced_directories) >= 3
    assert (runs / "queue-control.json").is_file()


def test_harness_report_cannot_clobber_a_symlinked_log_leaf(tmp_path: Path):
    repo, runs, outside = _repo_and_outside(tmp_path)
    victim = outside / "victim.txt"
    victim.write_text("keep\n")
    log_parent = runs / "logs" / "issue-42" / "execute" / "attempt-1"
    log_parent.mkdir(parents=True)
    (log_parent / "harness-report.txt").symlink_to(victim)
    lifecycle = TaskLifecycle(runs, repo_root=repo)

    with pytest.raises(LifecycleError, match="symlink or non-regular"):
        lifecycle.run(
            42,
            Phase.EXECUTE,
            lambda claim: _capture_harness_report(claim, "CLOBBER"),
        )

    assert victim.read_text() == "keep\n"
