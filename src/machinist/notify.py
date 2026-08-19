"""Best-effort desktop, command, and webhook notification delivery.

Notifications are advisory only: delivery failures are returned to callers,
never raised.  The legacy :func:`notify` desktop helper keeps its original
fire-and-forget behavior for existing integrations.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from .config import NotificationBackend, NotificationConfig, NotificationEvent

Runner = Callable[..., subprocess.CompletedProcess]
Opener = Callable[..., object]

_MAX_MESSAGE_CHARS = 200
_MAX_ERROR_CHARS = 500
_TIMEOUT_SECONDS = 5


class NotificationStatus(str, Enum):
    """Outcome of one configured delivery attempt."""

    DELIVERED = "delivered"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class NotificationResult:
    """Structured, stable result suitable for retry and dedupe bookkeeping."""

    status: NotificationStatus
    backend: str
    event: str
    dedupe_key: str
    error: str | None = None
    reason: str | None = None

    @property
    def delivered(self) -> bool:
        return self.status is NotificationStatus.DELIVERED

    @property
    def skipped(self) -> bool:
        return self.status is NotificationStatus.SKIPPED


def _applescript_string(text: str) -> str:
    # Newlines are illegal inside a one-line AppleScript string literal.
    collapsed = " ".join(text.split())
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def notify(title: str, message: str, runner: Runner = subprocess.run) -> None:
    """Display a desktop notification; silently do nothing where that fails."""
    _deliver_desktop(title, message, runner)


def _deliver_desktop(title: str, message: str, runner: Runner) -> str | None:
    """Return ``None`` on delivery, otherwise a bounded diagnostic string."""
    short_message = " ".join(message.split())[:_MAX_MESSAGE_CHARS]
    if shutil.which("osascript"):
        script = (
            f"display notification {_applescript_string(short_message)}"
            f" with title {_applescript_string(title)}"
        )
        try:
            completed = runner(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
            )
            if completed.returncode == 0:
                return None
            return _process_error("osascript", completed)
        except Exception as exc:
            desktop_error = _exception_error("osascript failed", exc)
    else:
        desktop_error = "desktop notifier unavailable"

    if shutil.which("notify-send"):
        try:
            completed = runner(
                ["notify-send", title, short_message],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
            )
            if completed.returncode == 0:
                return None
            return _process_error("notify-send", completed)
        except Exception as exc:
            return _exception_error("notify-send failed", exc)
    return desktop_error


def notify_event(
    config: NotificationConfig,
    event: NotificationEvent | str,
    title: str,
    message: str,
    *,
    context: Mapping[str, object] | None = None,
    dedupe_key: str | None = None,
    runner: Runner = subprocess.run,
    opener: Opener = urllib_request.urlopen,
    environ: Mapping[str, str] | None = None,
) -> NotificationResult:
    """Route one event according to ``notifications`` configuration.

    Command and webhook backends receive the same JSON object. Commands read
    it from stdin and are always invoked as an argv with ``shell=False``.
    Webhook URLs and optional authorization values are resolved from the
    configured environment variables at delivery time.
    """
    event_value = event.value if isinstance(event, NotificationEvent) else str(event)
    backend = config.backend.value
    supplied_context = dict(context or {})
    key_material = {
        "context": supplied_context,
        "event": event_value,
        "message": message,
        "title": title,
    }
    key = dedupe_key or _generated_dedupe_key(key_material, event_value)

    if config.backend is NotificationBackend.DISABLED:
        return NotificationResult(
            NotificationStatus.SKIPPED,
            backend,
            event_value,
            key,
            reason="disabled",
        )
    if event_value not in {configured.value for configured in config.events}:
        return NotificationResult(
            NotificationStatus.SKIPPED,
            backend,
            event_value,
            key,
            reason="event_filtered",
        )

    if config.backend is NotificationBackend.DESKTOP:
        error = _deliver_desktop(title, message, runner)
        return _delivery_result(backend, event_value, key, error)

    payload = {**key_material, "dedupe_key": key}
    try:
        payload_json = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        return NotificationResult(
            NotificationStatus.FAILED,
            backend,
            event_value,
            key,
            error=_exception_error(
                "notification payload is not JSON serializable", exc
            ),
        )

    if config.backend is NotificationBackend.COMMAND:
        command = config.command
        if command is None:  # Defensive for callers constructing models unsafely.
            error = "command notification backend is not configured"
        else:
            error = _deliver_command(
                command.argv,
                command.timeout_seconds,
                payload_json,
                runner,
            )
        return _delivery_result(backend, event_value, key, error)

    webhook = config.webhook
    if webhook is None:  # Defensive for callers constructing models unsafely.
        error = "webhook notification backend is not configured"
    else:
        error = _deliver_webhook(
            webhook.url_env,
            webhook.authorization_env,
            webhook.timeout_seconds,
            payload_json,
            opener,
            os.environ if environ is None else environ,
        )
    return _delivery_result(backend, event_value, key, error)


def _generated_dedupe_key(material: Mapping[str, object], event: str) -> str:
    try:
        canonical = json.dumps(
            material,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        # Keep a usable key even when later payload validation reports a bad
        # context value. Avoid repr(context), which may include memory addresses.
        canonical = json.dumps(
            {
                "event": event,
                "message": str(material.get("message", "")),
                "title": str(material.get("title", "")),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"machinist:{event}:{digest}"


def _deliver_command(
    argv: list[str],
    timeout_seconds: int,
    payload_json: str,
    runner: Runner,
) -> str | None:
    try:
        completed = runner(
            list(argv),
            input=payload_json,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
        if completed.returncode != 0:
            return _process_error("command", completed)
    except Exception as exc:  # Delivery is advisory; callers receive the error.
        return _exception_error("command failed", exc)
    return None


def _deliver_webhook(
    url_env: str,
    authorization_env: str | None,
    timeout_seconds: int,
    payload_json: str,
    opener: Opener,
    environ: Mapping[str, str],
) -> str | None:
    url = environ.get(url_env)
    if not url:
        return f"webhook URL environment variable {url_env} is not set"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return f"webhook URL environment variable {url_env} is not a valid URL"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return f"webhook URL environment variable {url_env} must contain an HTTP(S) URL"

    headers = {"Content-Type": "application/json"}
    if authorization_env is not None:
        authorization = environ.get(authorization_env)
        if not authorization:
            return (
                "webhook authorization environment variable "
                f"{authorization_env} is not set"
            )
        headers["Authorization"] = authorization

    try:
        request = urllib_request.Request(
            url,
            data=payload_json.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        response = opener(request, timeout=timeout_seconds)
    except urllib_error.HTTPError as exc:
        return f"webhook returned HTTP {exc.code}"
    except Exception as exc:  # Delivery is advisory; do not expose secret URLs.
        return f"webhook request failed: {type(exc).__name__}"

    response_error = None
    try:
        try:
            status = getattr(response, "status", None)
            if status is None:
                getcode = getattr(response, "getcode", None)
                status = getcode() if callable(getcode) else 200
        except Exception as exc:
            response_error = f"webhook response failed: {type(exc).__name__}"
            status = None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    if response_error is not None:
        return response_error
    if not isinstance(status, int) or not 200 <= status < 300:
        return f"webhook returned HTTP {status}"
    return None


def _delivery_result(
    backend: str,
    event: str,
    dedupe_key: str,
    error: str | None,
) -> NotificationResult:
    if error is None:
        return NotificationResult(
            NotificationStatus.DELIVERED,
            backend,
            event,
            dedupe_key,
        )
    return NotificationResult(
        NotificationStatus.FAILED,
        backend,
        event,
        dedupe_key,
        error=_bounded(error),
    )


def _process_error(name: str, completed: subprocess.CompletedProcess) -> str:
    detail = " ".join(str(completed.stderr or completed.stdout or "").split())
    message = f"{name} exited with status {completed.returncode}"
    if detail:
        message += f": {detail}"
    return _bounded(message)


def _exception_error(prefix: str, exc: BaseException) -> str:
    detail = " ".join(str(exc).split())
    message = f"{prefix}: {type(exc).__name__}"
    if detail:
        message += f": {detail}"
    return _bounded(message)


def _bounded(message: str) -> str:
    return message[:_MAX_ERROR_CHARS]
