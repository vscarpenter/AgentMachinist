"""Advisory release-update checks against the package index."""

import json
from types import SimpleNamespace

import pytest

from machinist.updates import (
    CHANGELOG_URL,
    PACKAGE_NAME,
    SKIP_UPDATE_CHECK_ENV,
    UpdateStatus,
    check_for_update,
    is_newer,
    parse_version,
    upgrade_command,
)


class _Response:
    """Minimal urlopen stand-in: no network, bounded read, closable."""

    def __init__(self, payload: bytes, *, status: int = 200):
        self._payload = payload
        self.status = status
        self.closed = False

    def read(self, amount: int | None = None) -> bytes:
        payload = self._payload
        self._payload = b""
        return payload if amount is None else payload[:amount]

    def close(self) -> None:
        self.closed = True


def _index_opener(latest: str, *, yanked: bool = False, seen: list | None = None):
    payload = json.dumps({"info": {"version": latest, "yanked": yanked}}).encode()

    def opener(request, timeout=None):
        if seen is not None:
            seen.append((request, timeout))
        return _Response(payload)

    return opener


def _check(installed: str, latest: str, **kwargs):
    return check_for_update(
        installed,
        opener=_index_opener(latest),
        environ={},
        executable="/usr/bin/python3",
        **kwargs,
    )


@pytest.mark.parametrize(
    "left,right",
    [
        ("0.6.1", "0.6.0"),
        ("0.7.0", "0.6.9"),
        ("1.0.0", "0.99.99"),
        ("0.6.0", "0.6.0rc1"),
        ("0.6.0rc2", "0.6.0rc1"),
        ("0.6.0", "0.6.0.dev3"),
        ("0.6.0.post1", "0.6.0"),
        ("0.6.10", "0.6.9"),
    ],
)
def test_release_ordering_follows_pep440(left, right):
    assert is_newer(left, right)
    assert not is_newer(right, left)


def test_equivalent_versions_are_not_newer():
    assert not is_newer("0.6.0", "0.6.0")
    assert not is_newer("0.6", "0.6.0")
    assert not is_newer("0.6.0", "0.6.0+local")


def test_unparsable_versions_never_claim_an_update():
    assert parse_version("not-a-version") is None
    assert not is_newer("not-a-version", "0.6.0")
    assert not is_newer("0.6.0", "not-a-version")


def test_update_available_reports_latest_and_upgrade_command():
    result = _check("0.6.0", "0.7.1")

    assert result.status is UpdateStatus.AVAILABLE
    assert result.update_available
    assert (result.installed, result.latest) == ("0.6.0", "0.7.1")
    assert result.error is None
    report = "\n".join(result.report_lines())
    assert "0.7.1" in report
    assert result.upgrade_command in report
    assert CHANGELOG_URL in report


def test_current_release_is_reported_without_an_upgrade_prompt():
    result = _check("0.6.0", "0.6.0")

    assert result.status is UpdateStatus.CURRENT
    assert not result.update_available
    assert "latest" in "\n".join(result.report_lines()).lower()


def test_local_build_ahead_of_the_index_is_not_an_update():
    result = _check("0.7.0.dev1", "0.6.0")

    assert result.status is UpdateStatus.AHEAD
    assert not result.update_available


def test_prerelease_on_the_index_does_not_nag_stable_installs():
    result = _check("0.6.0", "0.7.0rc1")

    assert result.status is UpdateStatus.CURRENT
    assert not result.update_available


def test_yanked_release_is_never_offered_as_an_upgrade():
    result = check_for_update(
        "0.6.0",
        opener=_index_opener("0.7.0", yanked=True),
        environ={},
        executable="/usr/bin/python3",
    )

    assert result.status is UpdateStatus.CURRENT
    assert not result.update_available


def test_index_failure_degrades_to_unknown_with_manual_instructions():
    def opener(request, timeout=None):
        raise OSError("name resolution failed")

    result = check_for_update(
        "0.6.0", opener=opener, environ={}, executable="/usr/bin/python3"
    )

    assert result.status is UpdateStatus.UNKNOWN
    assert not result.update_available
    assert result.latest is None
    assert result.error and "name resolution failed" in result.error
    assert result.upgrade_command in "\n".join(result.report_lines())


def test_malformed_index_payload_is_reported_not_raised():
    def opener(request, timeout=None):
        return _Response(b"{not json")

    result = check_for_update(
        "0.6.0", opener=opener, environ={}, executable="/usr/bin/python3"
    )

    assert result.status is UpdateStatus.UNKNOWN
    assert result.error


def test_oversized_index_payload_is_rejected():
    def opener(request, timeout=None):
        return _Response(b"x" * (5 * 1024 * 1024))

    result = check_for_update(
        "0.6.0", opener=opener, environ={}, executable="/usr/bin/python3"
    )

    assert result.status is UpdateStatus.UNKNOWN
    assert result.error and "too large" in result.error


def test_environment_variable_disables_the_check_without_any_request():
    def opener(request, timeout=None):  # pragma: no cover - must never run
        raise AssertionError("disabled checks must not reach the index")

    result = check_for_update(
        "0.6.0",
        opener=opener,
        environ={SKIP_UPDATE_CHECK_ENV: "1"},
        executable="/usr/bin/python3",
    )

    assert result.status is UpdateStatus.DISABLED
    assert not result.update_available
    assert SKIP_UPDATE_CHECK_ENV in "\n".join(result.report_lines())


def test_disable_variable_accepts_falsey_values_as_enabled():
    result = check_for_update(
        "0.6.0",
        opener=_index_opener("0.6.0"),
        environ={SKIP_UPDATE_CHECK_ENV: "0"},
        executable="/usr/bin/python3",
    )

    assert result.status is UpdateStatus.CURRENT


def test_request_is_an_https_get_with_the_configured_timeout():
    seen: list = []
    check_for_update(
        "0.6.0",
        opener=_index_opener("0.6.0", seen=seen),
        environ={},
        executable="/usr/bin/python3",
        timeout_seconds=3,
    )

    ((request, timeout),) = seen
    assert timeout == 3
    assert request.get_method() == "GET"
    assert request.full_url.startswith("https://pypi.org/pypi/")
    assert PACKAGE_NAME in request.full_url


def test_non_https_index_is_refused_before_any_request():
    def opener(request, timeout=None):  # pragma: no cover - must never run
        raise AssertionError("plaintext index must not be contacted")

    result = check_for_update(
        "0.6.0",
        opener=opener,
        environ={},
        executable="/usr/bin/python3",
        index_url="http://pypi.example/pypi/{package}/json",
    )

    assert result.status is UpdateStatus.UNKNOWN
    assert result.error and "https" in result.error.lower()


def test_json_payload_is_stable_for_scripting():
    payload = _check("0.6.0", "0.7.1").as_dict()

    assert payload == {
        "package": PACKAGE_NAME,
        "installed": "0.6.0",
        "latest": "0.7.1",
        "status": "available",
        "update_available": True,
        "upgrade_command": payload["upgrade_command"],
        "error": None,
    }
    assert json.dumps(payload, sort_keys=True)


def test_upgrade_command_matches_a_uv_tool_installation():
    command = upgrade_command(
        executable="/home/dev/.local/share/uv/tools/agentmachinist/bin/python",
        environ={},
        locator=lambda: None,
    )

    assert command == f"uv tool upgrade {PACKAGE_NAME}"


def test_upgrade_command_matches_a_relocated_uv_tool_directory():
    command = upgrade_command(
        executable="/opt/tools/agentmachinist/bin/python",
        environ={"UV_TOOL_DIR": "/opt/tools"},
        locator=lambda: None,
    )

    assert command == f"uv tool upgrade {PACKAGE_NAME}"


def test_upgrade_command_matches_a_pipx_installation():
    command = upgrade_command(
        executable="/home/dev/.local/pipx/venvs/agentmachinist/bin/python",
        environ={},
        locator=lambda: None,
    )

    assert command == f"pipx upgrade {PACKAGE_NAME}"


def test_editable_checkout_is_told_to_update_its_source():
    distribution = SimpleNamespace(
        read_text=lambda name: (
            json.dumps({"dir_info": {"editable": True}})
            if name == "direct_url.json"
            else None
        )
    )

    command = upgrade_command(
        executable="/repo/.venv/bin/python",
        environ={},
        locator=lambda: distribution,
    )

    assert command == "git pull && uv sync"


def test_upgrade_command_falls_back_to_the_running_interpreter():
    command = upgrade_command(
        executable="/usr/bin/python3", environ={}, locator=lambda: None
    )

    assert command == f"/usr/bin/python3 -m pip install --upgrade {PACKAGE_NAME}"


def test_upgrade_command_quotes_an_awkward_interpreter_path():
    command = upgrade_command(
        executable="/home/my dev/bin/python", environ={}, locator=lambda: None
    )

    assert (
        command == f"'/home/my dev/bin/python' -m pip install --upgrade {PACKAGE_NAME}"
    )


def test_missing_distribution_metadata_does_not_break_the_hint():
    def locator():
        raise LookupError("agentmachinist is not installed")

    command = upgrade_command(
        executable="/usr/bin/python3", environ={}, locator=locator
    )

    assert command.endswith(f"pip install --upgrade {PACKAGE_NAME}")
