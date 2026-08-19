"""Best-effort notifications (macOS osascript, Linux notify-send).

Notifications are advisory only: a missing notifier (or headless environment),
a hung process, or a non-zero exit must never disturb the caller, so failures
are deliberately swallowed — nothing is raised, printed, or logged. The
stdout event stream remains the record of what happened.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess]

_MAX_MESSAGE_CHARS = 200
_TIMEOUT_SECONDS = 5


def _applescript_string(text: str) -> str:
    # Newlines are illegal inside a one-line AppleScript string literal.
    collapsed = " ".join(text.split())
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def notify(title: str, message: str, runner: Runner = subprocess.run) -> None:
    """Display a desktop notification; silently do nothing where that fails."""
    short_message = " ".join(message.split())[:_MAX_MESSAGE_CHARS]
    if shutil.which("osascript"):
        script = (
            f"display notification {_applescript_string(short_message)}"
            f" with title {_applescript_string(title)}"
        )
        try:
            runner(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass

    if shutil.which("notify-send"):
        try:
            runner(
                ["notify-send", title, short_message],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
