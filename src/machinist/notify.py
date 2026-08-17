"""Best-effort macOS notifications via osascript.

Notifications are advisory only: a missing osascript (non-macOS), a hung
process, or a non-zero exit must never disturb the caller, so every failure
is deliberately swallowed — nothing is raised, printed, or logged. The
stdout event stream remains the record of what happened.
"""

from __future__ import annotations

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
    """Display a macOS notification; silently do nothing where that fails."""
    short_message = " ".join(message.split())[:_MAX_MESSAGE_CHARS]
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
    except (OSError, subprocess.SubprocessError):
        pass
