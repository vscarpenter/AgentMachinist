from pathlib import Path

import pytest

from machinist.cancellation import CancellationError, CancellationStore


def test_request_is_durable_callable_and_clearable(tmp_path: Path):
    store = CancellationStore(tmp_path / "runs")

    request = store.request(42, "operator stopped a runaway task")

    assert request.issue == 42
    assert store.get(42) == request
    assert store.check(42)()
    assert store.clear(42)
    assert not store.requested(42)
    assert not store.clear(42)


def test_corrupt_marker_fails_closed(tmp_path: Path):
    store = CancellationStore(tmp_path / "runs")
    store.root.mkdir(parents=True)
    (store.root / "issue-42.json").write_text("not json")

    with pytest.raises(CancellationError, match="corrupt"):
        store.requested(42)


def test_oversized_marker_fails_closed_without_reading_its_payload(tmp_path: Path):
    store = CancellationStore(tmp_path / "runs")
    store.root.mkdir(parents=True)
    marker = store.root / "issue-42.json"
    marker.touch()
    with marker.open("r+b") as stream:
        stream.truncate(64 * 1024 + 1)

    with pytest.raises(CancellationError, match="too large"):
        store.requested(42)


@pytest.mark.parametrize("issue", [0, -1, True])
def test_issue_must_be_positive_integer(tmp_path: Path, issue):
    with pytest.raises(CancellationError):
        CancellationStore(tmp_path / "runs").request(issue, "stop")
