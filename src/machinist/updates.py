"""Advisory release-update checks against the Python Package Index.

Update checks are informational only.  The controller never upgrades itself
and never fails a pipeline command because an index probe failed: every
network, parsing, and metadata error degrades to
:attr:`UpdateStatus.UNKNOWN` with a printable reason.  Operators can suppress
the probe entirely with ``MACHINIST_NO_UPDATE_CHECK``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path, PurePath
from typing import Any, Callable, Mapping
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

PACKAGE_NAME = "agentmachinist"
PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
CHANGELOG_URL = "https://github.com/vscarpenter/AgentMachinist/blob/main/CHANGELOG.md"
SKIP_UPDATE_CHECK_ENV = "MACHINIST_NO_UPDATE_CHECK"

DEFAULT_TIMEOUT_SECONDS = 5
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_ERROR_CHARS = 300
_USER_AGENT = f"{PACKAGE_NAME}-update-check"
_FALSE_VALUES = {"", "0", "false", "no", "off"}

Opener = Callable[..., Any]
Locator = Callable[[], Any]


class UpdateStatus(str, Enum):
    """Outcome of one advisory update check."""

    AVAILABLE = "available"
    CURRENT = "current"
    AHEAD = "ahead"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


@dataclass(frozen=True)
class UpdateCheck:
    """Structured, stable result suitable for printing or scripting."""

    status: UpdateStatus
    installed: str
    latest: str | None
    upgrade_command: str
    error: str | None = None
    package: str = PACKAGE_NAME

    @property
    def update_available(self) -> bool:
        return self.status is UpdateStatus.AVAILABLE

    def as_dict(self) -> dict[str, object]:
        return {
            "package": self.package,
            "installed": self.installed,
            "latest": self.latest,
            "status": self.status.value,
            "update_available": self.update_available,
            "upgrade_command": self.upgrade_command,
            "error": self.error,
        }

    def summary(self) -> str:
        """One line, sized for a diagnostics row."""
        if self.status is UpdateStatus.AVAILABLE:
            return (
                f"{self.installed} is installed; {self.latest} is available — "
                f"upgrade with `{self.upgrade_command}`"
            )
        if self.status is UpdateStatus.CURRENT:
            return f"{self.installed} is the latest release on PyPI"
        if self.status is UpdateStatus.AHEAD:
            return (
                f"{self.installed} is newer than the latest PyPI release {self.latest}"
            )
        if self.status is UpdateStatus.DISABLED:
            return f"update check disabled by {SKIP_UPDATE_CHECK_ENV}"
        return f"could not check PyPI for updates: {self.error}"

    def report_lines(self) -> tuple[str, ...]:
        """Full operator-facing report for ``machinist update-check``."""
        installed = f"Installed:      AgentMachinist {self.installed}"
        if self.status is UpdateStatus.AVAILABLE:
            return (
                installed,
                f"Latest on PyPI: AgentMachinist {self.latest}",
                "",
                "An update is available. Upgrade this installation with:",
                f"  {self.upgrade_command}",
                f"Release notes:  {CHANGELOG_URL}",
            )
        if self.status is UpdateStatus.CURRENT:
            return (
                installed,
                f"AgentMachinist {self.installed} is the latest release on PyPI.",
            )
        if self.status is UpdateStatus.AHEAD:
            return (
                installed,
                f"Latest on PyPI: AgentMachinist {self.latest}",
                "",
                "This installation is newer than the published release.",
            )
        if self.status is UpdateStatus.DISABLED:
            return (
                installed,
                f"Update checks are disabled by {SKIP_UPDATE_CHECK_ENV}.",
                "Upgrade this installation at any time with:",
                f"  {self.upgrade_command}",
            )
        return (
            installed,
            f"Could not check PyPI for updates: {self.error}",
            "Upgrade this installation at any time with:",
            f"  {self.upgrade_command}",
        )


@dataclass(frozen=True)
class _ParsedVersion:
    """The PEP 440 fields that decide ordering; local segments are ignored."""

    release: tuple[int, ...]
    pre: tuple[int, int] | None
    post: int | None
    dev: int | None

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    def sort_key(self) -> tuple:
        release = list(self.release)
        while len(release) > 1 and release[-1] == 0:
            release.pop()
        if self.pre is not None:
            pre_key = (0, *self.pre)
        elif self.post is None and self.dev is not None:
            # A dev release of an otherwise final version precedes every
            # pre-release of that version (PEP 440).
            pre_key = (-1, 0, 0)
        else:
            pre_key = (1, 0, 0)
        post_key = -1 if self.post is None else self.post
        dev_key = (1, 0) if self.dev is None else (0, self.dev)
        return (tuple(release), pre_key, post_key, dev_key)


_PRE_RANK = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "c": 2,
    "rc": 2,
    "pre": 2,
    "preview": 2,
}

_VERSION_PATTERN = re.compile(
    r"""
    ^\s*v?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_label>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_number>\d+)?)?
    (?:[-_.]?(?:post|rev|r)[-_.]?(?P<post_number>\d+)?)?
    (?:[-_.]?dev[-_.]?(?P<dev_number>\d+)?)?
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_version(text: object) -> _ParsedVersion | None:
    """Parse the PEP 440 subset the controller publishes; ``None`` if unusable."""
    if not isinstance(text, str):
        return None
    match = _VERSION_PATTERN.match(text)
    if match is None:
        return None
    try:
        release = tuple(int(part) for part in match.group("release").split("."))
    except ValueError:  # pragma: no cover - the pattern only admits digits
        return None
    pre_label = match.group("pre_label")
    pre = None
    if pre_label is not None:
        pre = (
            _PRE_RANK[pre_label.lower()],
            int(match.group("pre_number") or 0),
        )
    post = None
    if match.group("post_number") is not None or "post" in text.lower():
        post_number = match.group("post_number")
        post = int(post_number) if post_number is not None else 0
    dev = None
    if match.group("dev_number") is not None or "dev" in text.lower():
        dev_number = match.group("dev_number")
        dev = int(dev_number) if dev_number is not None else 0
    return _ParsedVersion(release=release, pre=pre, post=post, dev=dev)


def is_newer(candidate: object, baseline: object) -> bool:
    """Return whether ``candidate`` is a strictly later release than ``baseline``.

    Unparsable input is never treated as newer: an index that starts
    publishing versions this parser cannot read must not produce upgrade
    prompts.
    """
    left = parse_version(candidate)
    right = parse_version(baseline)
    if left is None or right is None:
        return False
    return left.sort_key() > right.sort_key()


def _truncate(detail: str) -> str:
    collapsed = " ".join(str(detail).split())
    if len(collapsed) <= _MAX_ERROR_CHARS:
        return collapsed
    return collapsed[: _MAX_ERROR_CHARS - 1] + "…"


def _distribution() -> Any | None:
    try:
        return distribution(PACKAGE_NAME)
    except PackageNotFoundError:
        return None


def _is_editable(locator: Locator) -> bool:
    """Detect a ``pip install -e``/``uv sync`` checkout from its direct URL."""
    try:
        dist = locator()
        if dist is None:
            return False
        raw = dist.read_text("direct_url.json")
        if not raw:
            return False
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - metadata is advisory, never fatal
        return False
    return bool(
        isinstance(payload, dict)
        and isinstance(payload.get("dir_info"), dict)
        and payload["dir_info"].get("editable")
    )


def upgrade_command(
    *,
    executable: str | None = None,
    environ: Mapping[str, str] | None = None,
    locator: Locator | None = None,
) -> str:
    """Return the upgrade command that matches how this copy was installed."""
    interpreter = sys.executable if executable is None else executable
    environment = os.environ if environ is None else environ
    resolve = _distribution if locator is None else locator

    if _is_editable(resolve):
        return "git pull && uv sync"

    parts = tuple(PurePath(interpreter).parts) if interpreter else ()
    lowered = tuple(part.lower() for part in parts)
    tool_dir = environment.get("UV_TOOL_DIR")
    under_tool_dir = bool(
        tool_dir
        and interpreter
        and PurePath(interpreter).is_relative_to(Path(tool_dir).expanduser())
    )
    if under_tool_dir or ("uv" in lowered and "tools" in lowered):
        return f"uv tool upgrade {PACKAGE_NAME}"
    if "pipx" in lowered and "venvs" in lowered:
        return f"pipx upgrade {PACKAGE_NAME}"
    if not interpreter:
        return f"pip install --upgrade {PACKAGE_NAME}"
    return f"{shlex.quote(interpreter)} -m pip install --upgrade {PACKAGE_NAME}"


def _read_bounded(response: Any) -> bytes:
    payload = response.read(_MAX_RESPONSE_BYTES + 1)
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("index response was not bytes")
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise ValueError(
            f"index response is too large (over {_MAX_RESPONSE_BYTES} bytes)"
        )
    return bytes(payload)


def fetch_latest_release(
    *,
    package: str = PACKAGE_NAME,
    opener: Opener | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    index_url: str = PYPI_JSON_URL,
) -> tuple[str | None, bool, str | None]:
    """Read ``(version, yanked, error)`` from the index without raising."""
    url = index_url.format(package=package)
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return None, False, "package index URL is not a valid URL"
    if scheme != "https":
        return None, False, "package index URL must use https"

    request = urllib_request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        method="GET",
    )
    selected = urllib_request.urlopen if opener is None else opener
    try:
        response = selected(request, timeout=timeout_seconds)
    except urllib_error.HTTPError as exc:
        try:
            exc.close()
        except Exception:  # noqa: BLE001 - cleanup must not mask the result
            pass
        return None, False, f"package index returned HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - probes report, never raise
        return None, False, _truncate(f"{type(exc).__name__}: {exc}")

    try:
        payload = json.loads(_read_bounded(response).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - probes report, never raise
        return None, False, _truncate(f"{type(exc).__name__}: {exc}")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                pass

    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        return None, False, "package index response has no release metadata"
    version = info.get("version")
    if not isinstance(version, str) or not version.strip():
        return None, False, "package index response has no release version"
    return version.strip(), bool(info.get("yanked")), None


def update_checks_disabled(environ: Mapping[str, str] | None = None) -> bool:
    """Honor ``MACHINIST_NO_UPDATE_CHECK`` for offline and CI environments."""
    environment = os.environ if environ is None else environ
    return (
        environment.get(SKIP_UPDATE_CHECK_ENV, "").strip().lower() not in _FALSE_VALUES
    )


def check_for_update(
    installed_version: str,
    *,
    package: str = PACKAGE_NAME,
    opener: Opener | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    index_url: str = PYPI_JSON_URL,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
    locator: Locator | None = None,
) -> UpdateCheck:
    """Compare the installed release against the index; never raise."""
    environment = os.environ if environ is None else environ
    command = upgrade_command(
        executable=executable, environ=environment, locator=locator
    )

    if update_checks_disabled(environment):
        return UpdateCheck(
            status=UpdateStatus.DISABLED,
            installed=installed_version,
            latest=None,
            upgrade_command=command,
            package=package,
        )

    latest, yanked, error = fetch_latest_release(
        package=package,
        opener=opener,
        timeout_seconds=timeout_seconds,
        index_url=index_url,
    )
    if error is not None or latest is None:
        return UpdateCheck(
            status=UpdateStatus.UNKNOWN,
            installed=installed_version,
            latest=None,
            upgrade_command=command,
            error=error or "package index returned no release version",
            package=package,
        )

    parsed_latest = parse_version(latest)
    parsed_installed = parse_version(installed_version)
    if parsed_latest is None or parsed_installed is None:
        unreadable = latest if parsed_latest is None else installed_version
        return UpdateCheck(
            status=UpdateStatus.UNKNOWN,
            installed=installed_version,
            latest=latest,
            upgrade_command=command,
            error=f"cannot compare release versions: {_truncate(unreadable)!r}",
            package=package,
        )

    # A yanked release, or a pre-release reached by a stable installation, is
    # never an upgrade the controller recommends unattended.
    upgradeable = not yanked and (
        not parsed_latest.is_prerelease or parsed_installed.is_prerelease
    )
    if upgradeable and is_newer(latest, installed_version):
        status = UpdateStatus.AVAILABLE
    elif is_newer(installed_version, latest):
        status = UpdateStatus.AHEAD
    else:
        status = UpdateStatus.CURRENT
    return UpdateCheck(
        status=status,
        installed=installed_version,
        latest=latest,
        upgrade_command=command,
        package=package,
    )
