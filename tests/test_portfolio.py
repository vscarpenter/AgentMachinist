"""Atomic multi-repository registry and read-only local status."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from machinist.lifecycle import Phase, TaskLifecycle
from machinist.portfolio import (
    CorruptPortfolioError,
    PortfolioError,
    PortfolioRegistry,
    collect_local_status,
)


def _git_repository(path):
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path.resolve()


def test_add_resolves_nested_path_to_git_root_and_deduplicates(tmp_path):
    repository = _git_repository(tmp_path / "project")
    nested = repository / "src" / "nested"
    nested.mkdir(parents=True)
    registry_path = tmp_path / "state" / "portfolio.json"
    registry = PortfolioRegistry(registry_path)

    assert registry.add(nested) == repository
    assert registry.add(repository) == repository
    assert registry.list() == (repository,)
    assert json.loads(registry_path.read_text()) == {
        "schema_version": 1,
        "repositories": [str(repository)],
    }
    assert registry_path.stat().st_mode & 0o777 == 0o600
    assert not list(registry_path.parent.glob(".portfolio.json.*.tmp"))


def test_list_missing_registry_is_read_only(tmp_path):
    path = tmp_path / "absent" / "portfolio.json"

    assert PortfolioRegistry(path).list() == ()
    assert not path.parent.exists()


def test_remove_accepts_nested_checkout_and_stale_exact_root(tmp_path):
    first = _git_repository(tmp_path / "z-project")
    second = _git_repository(tmp_path / "a-project")
    child = first / "child"
    child.mkdir()
    registry = PortfolioRegistry(tmp_path / "portfolio.json")
    registry.add(first)
    registry.add(second)

    assert registry.list() == (second, first)
    assert registry.remove(child) is True
    second.rename(tmp_path / "moved")
    assert registry.remove(second) is True
    assert registry.remove(first) is False
    assert registry.list() == ()


@pytest.mark.parametrize(
    "payload",
    [
        "{not-json}",
        '{"schema_version":99,"repositories":[]}',
        '{"schema_version":1,"repositories":["relative"]}',
        '{"schema_version":1,"repositories":["/tmp/a","/tmp/a"]}',
        '{"schema_version":1,"repositories":["/tmp/z","/tmp/a"]}',
    ],
)
def test_corrupt_registry_fails_closed_without_overwrite(tmp_path, payload):
    path = tmp_path / "portfolio.json"
    path.write_text(payload)
    repository = _git_repository(tmp_path / "project")
    registry = PortfolioRegistry(path)

    with pytest.raises(CorruptPortfolioError):
        registry.list()
    with pytest.raises(CorruptPortfolioError):
        registry.add(repository)
    assert path.read_text() == payload


def test_failed_atomic_replace_preserves_previous_registry(tmp_path, monkeypatch):
    first = _git_repository(tmp_path / "first")
    second = _git_repository(tmp_path / "second")
    path = tmp_path / "portfolio.json"
    registry = PortfolioRegistry(path)
    registry.add(first)
    before = path.read_bytes()

    def fail_replace(source, target):
        raise OSError("disk unavailable")

    monkeypatch.setattr("machinist.portfolio.os.replace", fail_replace)
    with pytest.raises(PortfolioError, match="disk unavailable"):
        registry.add(second)

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".portfolio.json.*.tmp"))


def test_concurrent_registry_additions_do_not_lose_updates(tmp_path):
    roots = []
    for index in range(12):
        root = tmp_path / f"project-{index:02d}"
        root.mkdir()
        roots.append(root.resolve())
    registry = PortfolioRegistry(
        tmp_path / "portfolio.json",
        git_root_loader=lambda path: path,
    )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(registry.add, roots))

    assert registry.list() == tuple(sorted(roots, key=str))


def test_default_status_reads_lifecycle_without_mutating_repository(tmp_path):
    repository = _git_repository(tmp_path / "project")
    lifecycle = TaskLifecycle(repository / ".machinist" / "runs")
    lifecycle.run(7, Phase.SPEC, lambda claim: None)
    registry = PortfolioRegistry(tmp_path / "portfolio.json")
    registry.add(repository)
    before = {
        path.relative_to(repository): path.stat().st_mtime_ns
        for path in repository.rglob("*")
        if path.is_file()
    }

    (status,) = collect_local_status(registry)
    payload = status.to_dict()

    assert status.ok
    assert payload["report"]["current"][0]["issue"] == 7
    after = {
        path.relative_to(repository): path.stat().st_mtime_ns
        for path in repository.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_injected_status_loader_isolates_repository_failures(tmp_path):
    first = (tmp_path / "first").resolve()
    second = (tmp_path / "second").resolve()
    first.mkdir()
    second.mkdir()
    roots = {first, second}
    registry = PortfolioRegistry(
        tmp_path / "portfolio.json",
        git_root_loader=lambda path: next(
            root for root in roots if root == path.resolve()
        ),
    )
    registry.add(second)
    registry.add(first)

    def loader(path):
        if path == first:
            return {"tasks": 2}
        raise RuntimeError("local state unavailable")

    statuses = collect_local_status(registry, loader=loader)

    assert [status.path for status in statuses] == [first, second]
    assert statuses[0].to_dict()["report"] == {"tasks": 2}
    assert statuses[1].to_dict()["error"] == {
        "type": "RuntimeError",
        "message": "local state unavailable",
    }


def test_default_status_reports_missing_registered_repository_without_creating_it(
    tmp_path,
):
    missing = (tmp_path / "missing").resolve()
    path = tmp_path / "portfolio.json"
    path.write_text(
        json.dumps({"schema_version": 1, "repositories": [str(missing)]}) + "\n"
    )

    (status,) = collect_local_status(PortfolioRegistry(path))

    assert not status.ok
    assert status.error_type == "PortfolioError"
    assert not missing.exists()


def test_git_root_probe_is_bounded(tmp_path):
    repository = tmp_path / "project"
    repository.mkdir()
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, f"{repository}\n", "")

    registry = PortfolioRegistry(tmp_path / "portfolio.json", runner=runner)

    assert registry.add(repository) == repository.resolve()
    assert calls[0][1]["timeout"] == 10
