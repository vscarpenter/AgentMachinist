"""Release and CI workflow safety contracts."""

from pathlib import Path

import tomllib

_ROOT = Path(__file__).resolve().parent.parent


def test_package_version_is_pep440_and_not_the_initial_release():
    version = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert version != "0.1.0"
    assert version[0].isdigit()
    assert "+" not in version


def test_release_verifies_identity_tests_and_wheel_before_publish():
    text = (_ROOT / ".github/workflows/release.yml").read_text()
    required_order = [
        "Verify tag matches package version",
        "Run tests",
        "Build sdist and wheel",
        "Smoke-test installed wheel",
        "Publish to PyPI",
    ]
    positions = [text.index(item) for item in required_order]
    assert positions == sorted(positions)
    assert "github.event.release.tag_name" in text
    assert "importlib.resources" in text
    assert "machinist --version" in text


def test_ci_covers_linux_macos_and_package_build():
    text = (_ROOT / ".github/workflows/ci.yml").read_text()
    assert "ubuntu-latest" in text
    assert "macos-latest" in text
    assert "uv build" in text
    assert "Smoke-test installed wheel" in text
