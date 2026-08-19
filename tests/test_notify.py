"""Tests for best-effort desktop and configured notification delivery."""

import json
import subprocess
from urllib.error import HTTPError

from machinist.config import (
    CommandNotificationConfig,
    NotificationBackend,
    NotificationConfig,
    NotificationEvent,
    WebhookNotificationConfig,
)
from machinist.notify import NotificationStatus, notify, notify_event


class FakeRunner:
    def __init__(self, *results):
        self.calls = []
        self._results = list(results)

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        stdout, returncode, stderr = result
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _which(*available):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def test_notify_runs_osascript_display_notification(monkeypatch):
    monkeypatch.setattr("machinist.notify.shutil.which", _which("osascript"))
    runner = FakeRunner(("", 0, ""))

    notify("machinist watch", "spec for issue #7 failed: boom", runner=runner)

    args, kwargs = runner.calls[0]
    assert args == [
        "osascript",
        "-e",
        'display notification "spec for issue #7 failed: boom" with title "machinist watch"',
    ]
    assert kwargs["timeout"] == 5
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_notify_swallows_missing_osascript(monkeypatch):
    monkeypatch.setattr("machinist.notify.shutil.which", _which("osascript"))
    notify("machinist watch", "boom", runner=FakeRunner(FileNotFoundError("osascript")))


def test_notify_swallows_nonzero_exit_and_timeout(monkeypatch):
    monkeypatch.setattr("machinist.notify.shutil.which", _which("osascript"))
    notify("machinist watch", "boom", runner=FakeRunner(("", 1, "AppleScript error")))
    notify(
        "machinist watch",
        "boom",
        runner=FakeRunner(subprocess.TimeoutExpired(cmd=["osascript"], timeout=5)),
    )


def test_notify_escapes_quotes_and_flattens_newlines(monkeypatch):
    monkeypatch.setattr("machinist.notify.shutil.which", _which("osascript"))
    runner = FakeRunner(("", 0, ""))

    notify("machinist watch", 'spec failed:\n"unexpected \\ error"', runner=runner)

    args, _ = runner.calls[0]
    assert args[2] == (
        'display notification "spec failed: \\"unexpected \\\\ error\\"" '
        'with title "machinist watch"'
    )


def test_notify_truncates_long_messages(monkeypatch):
    monkeypatch.setattr("machinist.notify.shutil.which", _which("osascript"))
    runner = FakeRunner(("", 0, ""))

    notify("machinist watch", "x" * 1000, runner=runner)

    args, _ = runner.calls[0]
    embedded_message = args[2].split('"')[1]
    assert embedded_message == "x" * 200


def test_notify_uses_notify_send_when_osascript_is_unavailable(monkeypatch):
    monkeypatch.setattr("machinist.notify.shutil.which", _which("notify-send"))
    runner = FakeRunner(("", 0, ""))

    notify("machinist watch", "spec finished", runner=runner)

    args, kwargs = runner.calls[0]
    assert args == ["notify-send", "machinist watch", "spec finished"]
    assert kwargs == {"capture_output": True, "text": True, "timeout": 5}


def test_notify_event_preserves_default_failure_desktop_delivery(monkeypatch):
    monkeypatch.setattr("machinist.notify.shutil.which", _which("osascript"))
    runner = FakeRunner(("", 0, ""))

    result = notify_event(
        NotificationConfig(),
        NotificationEvent.FAILURE,
        "machinist watch",
        "spec for issue #7 failed: boom",
        context={"issue_number": 7, "phase": "spec"},
        runner=runner,
    )

    assert result.status is NotificationStatus.DELIVERED
    assert result.backend == "desktop"
    assert result.event == "failure"
    assert result.error is None
    assert result.dedupe_key.startswith("machinist:failure:")
    assert len(runner.calls) == 1


def test_notify_event_filters_events_before_delivery(monkeypatch):
    monkeypatch.setattr("machinist.notify.shutil.which", _which("osascript"))
    runner = FakeRunner(("", 0, ""))

    result = notify_event(
        NotificationConfig(),
        NotificationEvent.PR_READY,
        "machinist watch",
        "PR ready",
        runner=runner,
    )

    assert result.status is NotificationStatus.SKIPPED
    assert result.reason == "event_filtered"
    assert runner.calls == []


def test_notify_event_disabled_backend_is_an_explicit_skip():
    result = notify_event(
        NotificationConfig(
            backend=NotificationBackend.DISABLED,
            events=[NotificationEvent.FAILURE],
        ),
        NotificationEvent.FAILURE,
        "machinist watch",
        "boom",
    )

    assert result.status is NotificationStatus.SKIPPED
    assert result.reason == "disabled"
    assert result.error is None


def test_notify_event_command_sends_stable_json_on_stdin_without_a_shell():
    runner = FakeRunner(("accepted", 0, ""))
    config = NotificationConfig(
        backend=NotificationBackend.COMMAND,
        events=[NotificationEvent.PR_READY],
        command=CommandNotificationConfig(
            argv=["/usr/local/bin/notifier", "--json"],
            timeout_seconds=7,
        ),
    )

    result = notify_event(
        config,
        NotificationEvent.PR_READY,
        "Machinist",
        "PR #42 is ready",
        context={"issue_number": 7, "pull_request": 42},
        dedupe_key="issue-7:pr-ready:42",
        runner=runner,
    )

    args, kwargs = runner.calls[0]
    assert args == ["/usr/local/bin/notifier", "--json"]
    assert kwargs == {
        "input": kwargs["input"],
        "capture_output": True,
        "text": True,
        "timeout": 7,
        "shell": False,
    }
    assert json.loads(kwargs["input"]) == {
        "context": {"issue_number": 7, "pull_request": 42},
        "dedupe_key": "issue-7:pr-ready:42",
        "event": "pr_ready",
        "message": "PR #42 is ready",
        "title": "Machinist",
    }
    assert result.status is NotificationStatus.DELIVERED
    assert result.dedupe_key == "issue-7:pr-ready:42"


def test_notify_event_command_failure_is_returned_and_dedupe_stable():
    config = NotificationConfig(
        backend=NotificationBackend.COMMAND,
        command=CommandNotificationConfig(argv=["notifier"]),
    )

    first = notify_event(
        config,
        "failure",
        "Machinist",
        "boom",
        context={"issue_number": 7},
        runner=FakeRunner(("", 17, "receiver unavailable")),
    )
    second = notify_event(
        config,
        "failure",
        "Machinist",
        "boom",
        context={"issue_number": 7},
        runner=FakeRunner(FileNotFoundError("notifier")),
    )

    assert first.status is NotificationStatus.FAILED
    assert first.error == "command exited with status 17: receiver unavailable"
    assert second.status is NotificationStatus.FAILED
    assert second.error == "command failed: FileNotFoundError: notifier"
    assert first.dedupe_key == second.dedupe_key


class FakeResponse:
    def __init__(self, status=204):
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True


def test_notify_event_webhook_posts_json_with_env_authorization():
    calls = []
    response = FakeResponse()

    def opener(request, **kwargs):
        calls.append((request, kwargs))
        return response

    config = NotificationConfig(
        backend=NotificationBackend.WEBHOOK,
        events=[NotificationEvent.SPEC_READY],
        webhook=WebhookNotificationConfig(
            url_env="TEST_WEBHOOK_URL",
            authorization_env="TEST_WEBHOOK_AUTH",
            timeout_seconds=9,
        ),
    )

    result = notify_event(
        config,
        NotificationEvent.SPEC_READY,
        "Machinist",
        "Spec is ready",
        context={"issue_number": 7},
        opener=opener,
        environ={
            "TEST_WEBHOOK_URL": "https://hooks.example.test/machinist",
            "TEST_WEBHOOK_AUTH": "Bearer secret-token",
        },
    )

    request, kwargs = calls[0]
    assert request.full_url == "https://hooks.example.test/machinist"
    assert request.method == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert json.loads(request.data) == {
        "context": {"issue_number": 7},
        "dedupe_key": result.dedupe_key,
        "event": "spec_ready",
        "message": "Spec is ready",
        "title": "Machinist",
    }
    assert kwargs == {"timeout": 9}
    assert response.closed is True
    assert result.status is NotificationStatus.DELIVERED


def test_notify_event_webhook_missing_url_and_http_error_do_not_raise():
    config = NotificationConfig(
        backend=NotificationBackend.WEBHOOK,
        webhook=WebhookNotificationConfig(url_env="SECRET_WEBHOOK_URL"),
    )
    missing = notify_event(
        config,
        "failure",
        "Machinist",
        "boom",
        environ={},
    )

    def rejected(request, **kwargs):
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    rejected_result = notify_event(
        config,
        "failure",
        "Machinist",
        "boom",
        opener=rejected,
        environ={"SECRET_WEBHOOK_URL": "https://token@example.test/hook"},
    )

    assert missing.status is NotificationStatus.FAILED
    assert (
        missing.error
        == "webhook URL environment variable SECRET_WEBHOOK_URL is not set"
    )
    assert rejected_result.status is NotificationStatus.FAILED
    assert rejected_result.error == "webhook returned HTTP 503"
    assert "token" not in rejected_result.error
    assert missing.dedupe_key == rejected_result.dedupe_key
