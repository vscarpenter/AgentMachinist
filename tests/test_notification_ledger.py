"""Durable notification dedupe across short-lived watcher processes."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from machinist.notification_ledger import NotificationLedger
from machinist.notify import NotificationResult, NotificationStatus


def _result(
    status: NotificationStatus = NotificationStatus.DELIVERED,
    *,
    key: str = "same-event",
) -> NotificationResult:
    return NotificationResult(status, "desktop", "failure", key)


def test_success_is_suppressed_until_reminder_ttl_then_delivered_again(tmp_path):
    moment = [datetime(2026, 8, 19, 12, tzinfo=UTC)]
    calls = []
    ledger = NotificationLedger(tmp_path / "runs", clock=lambda: moment[0])

    def deliver():
        calls.append(moment[0])
        return _result()

    first = ledger.deliver_once("same-event", deliver)
    duplicate = ledger.deliver_once("same-event", deliver)
    moment[0] += timedelta(hours=24)
    reminder = ledger.deliver_once("same-event", deliver)

    assert first.notification is not None
    assert duplicate.suppressed is True
    assert duplicate.notification is None
    assert reminder.notification is not None
    assert calls == [
        datetime(2026, 8, 19, 12, tzinfo=UTC),
        datetime(2026, 8, 20, 12, tzinfo=UTC),
    ]


def test_failed_and_skipped_deliveries_are_never_recorded(tmp_path):
    ledger = NotificationLedger(tmp_path / "runs")
    calls = []

    for status in (NotificationStatus.FAILED, NotificationStatus.SKIPPED):
        key = status.value

        def deliver(status=status, key=key):
            calls.append(status)
            return _result(status, key=key)

        first = ledger.deliver_once(key, deliver)
        second = ledger.deliver_once(key, deliver)
        assert first.suppressed is False
        assert second.suppressed is False

    assert calls == [
        NotificationStatus.FAILED,
        NotificationStatus.FAILED,
        NotificationStatus.SKIPPED,
        NotificationStatus.SKIPPED,
    ]


def test_corrupt_ledger_fails_open_then_successfully_repairs_it(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    state = runs / "notification-ledger.json"
    state.write_text("{not-json", encoding="utf-8")
    calls = []
    ledger = NotificationLedger(runs)

    def deliver():
        calls.append("delivered")
        return _result()

    recovered = ledger.deliver_once("same-event", deliver)
    duplicate = ledger.deliver_once("same-event", deliver)

    assert recovered.notification is not None
    assert "dedupe bypassed" in (recovered.warning or "")
    assert duplicate.suppressed is True
    assert calls == ["delivered"]
    assert json.loads(state.read_text())["schema_version"] == 1


def test_concurrent_ledgers_serialize_delivery_for_the_same_key(tmp_path):
    runs = tmp_path / "runs"
    counter = 0
    counter_lock = threading.Lock()

    def attempt():
        ledger = NotificationLedger(runs)

        def deliver():
            nonlocal counter
            with counter_lock:
                counter += 1
            time.sleep(0.02)
            return _result()

        return ledger.deliver_once("same-event", deliver)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(8)))

    assert counter == 1
    assert sum(outcome.suppressed for outcome in outcomes) == 7


def test_concurrent_processes_serialize_delivery_for_the_same_key(tmp_path):
    runs = tmp_path / "runs"
    marker = tmp_path / "deliveries.txt"
    program = """
import sys
import time
from pathlib import Path
from machinist.notification_ledger import NotificationLedger
from machinist.notify import NotificationResult, NotificationStatus

runs = Path(sys.argv[1])
marker = Path(sys.argv[2])

def deliver():
    with marker.open("a", encoding="utf-8") as stream:
        stream.write("delivered\\n")
        stream.flush()
    time.sleep(0.05)
    return NotificationResult(
        NotificationStatus.DELIVERED,
        "desktop",
        "failure",
        "same-event",
    )

NotificationLedger(runs).deliver_once("same-event", deliver)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(runs), str(marker)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]

    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
    assert marker.read_text(encoding="utf-8").splitlines() == ["delivered"]


def test_ledger_is_bounded_private_and_leaves_no_temporary_files(tmp_path):
    runs = tmp_path / "runs"
    moment = [datetime(2026, 8, 19, 12, tzinfo=UTC)]
    ledger = NotificationLedger(
        runs,
        max_entries=3,
        clock=lambda: moment[0],
    )

    for index in range(10):
        ledger.deliver_once(f"event-{index}", lambda: _result())
        moment[0] += timedelta(minutes=1)

    state = runs / "notification-ledger.json"
    payload = json.loads(state.read_text())
    assert set(payload["entries"]) == {"event-7", "event-8", "event-9"}
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert list(runs.glob(".notification-ledger.*.tmp")) == []


def test_failed_atomic_replace_preserves_prior_state_without_double_delivery(
    tmp_path: Path, monkeypatch
):
    runs = tmp_path / "runs"
    ledger = NotificationLedger(runs)
    ledger.deliver_once("existing", lambda: _result())
    original = (runs / "notification-ledger.json").read_bytes()
    calls = []

    def fail_replace(source, destination, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("machinist.runtime_paths.os.replace", fail_replace)
    outcome = ledger.deliver_once(
        "new-event",
        lambda: calls.append("delivered") or _result(),
    )

    assert outcome.notification is not None
    assert "ledger update failed" in (outcome.warning or "")
    assert calls == ["delivered"]
    assert (runs / "notification-ledger.json").read_bytes() == original
    assert list(runs.glob(".notification-ledger.*.tmp")) == []
