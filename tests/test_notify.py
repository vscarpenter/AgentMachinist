"""Tests for the best-effort macOS notifier."""

import subprocess

from machinist.notify import notify


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


def test_notify_runs_osascript_display_notification():
    runner = FakeRunner(("", 0, ""))

    notify("machinist watch", "spec for issue #7 failed: boom", runner=runner)

    args, kwargs = runner.calls[0]
    assert args == [
        "osascript", "-e",
        'display notification "spec for issue #7 failed: boom" with title "machinist watch"',
    ]
    assert kwargs["timeout"] == 5
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_notify_swallows_missing_osascript():
    notify("machinist watch", "boom", runner=FakeRunner(FileNotFoundError("osascript")))


def test_notify_swallows_nonzero_exit_and_timeout():
    notify("machinist watch", "boom", runner=FakeRunner(("", 1, "AppleScript error")))
    notify("machinist watch", "boom",
           runner=FakeRunner(subprocess.TimeoutExpired(cmd=["osascript"], timeout=5)))


def test_notify_escapes_quotes_and_flattens_newlines():
    runner = FakeRunner(("", 0, ""))

    notify("machinist watch", 'spec failed:\n"unexpected \\ error"', runner=runner)

    args, _ = runner.calls[0]
    assert args[2] == (
        'display notification "spec failed: \\"unexpected \\\\ error\\"" '
        'with title "machinist watch"'
    )


def test_notify_truncates_long_messages():
    runner = FakeRunner(("", 0, ""))

    notify("machinist watch", "x" * 1000, runner=runner)

    args, _ = runner.calls[0]
    embedded_message = args[2].split('"')[1]
    assert embedded_message == "x" * 200
