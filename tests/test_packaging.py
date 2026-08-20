import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "smoke-wheel.sh"
_VERIFY_SCRIPT = _ROOT / "scripts" / "verify.sh"

_SDIST_INCLUDES = {
    "/src/machinist",
    "/tests",
    "/scripts",
    "/pyproject.toml",
    "/uv.lock",
    "/README.md",
    "/LICENSE",
    "/CHANGELOG.md",
    "/CONTRIBUTING.md",
    "/SECURITY.md",
    "/.gitignore",
}


def _verification_status_digest(repo: Path) -> str:
    text = _VERIFY_SCRIPT.read_text()
    start = text.index("status_digest()")
    end = text.index("\n}\n\ncheck_workflows()", start) + 2
    result = subprocess.run(
        ["bash", "-c", f"{text[start:end]}\nstatus_digest"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_smoke_wheel_script_selects_versioned_wheel_and_templates():
    text = _SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert "tomllib" in text
    assert "agentmachinist-${VERSION}-py3-none-any.whl" in text
    assert "machinist --version" in text
    assert "spec-prompt.md" in text
    assert "machinist-approve.yml" in text
    assert "uv pip check" in text
    assert "uv run python" not in text
    assert "find dist" not in text


def test_verify_script_is_the_canonical_frozen_build_gate():
    text = _VERIFY_SCRIPT.read_text()
    verify_all = text[
        text.index("verify_all()") : text.index(
            "\n}\n\ncase", text.index("verify_all()")
        )
    ]
    required_order = [
        "uv lock --check",
        "uv sync --frozen",
        "check_workflows",
        "check_format",
        "check_lint",
        "check_types",
        "check_coverage",
        "build_and_smoke",
    ]
    positions = [verify_all.index(command) for command in required_order]
    assert positions == sorted(positions)
    assert "set -euo pipefail" in text
    assert "git hash-object --stdin" in text
    assert "python -c" not in text
    assert "uv build --no-sources --no-build-isolation" in text

    config = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert "hatchling==1.32.0" in config["dependency-groups"]["dev"]


def test_verify_script_exposes_distinct_quality_and_coverage_gates():
    text = _VERIFY_SCRIPT.read_text()

    assert "ruff format --check src tests" in text
    assert "ruff check src tests" in text
    assert "uv run --frozen mypy" in text
    assert "--cov=machinist --cov-report=term-missing --cov-fail-under=80" in text
    for mode in ("format", "lint", "types", "coverage"):
        assert f"{mode})" in text


def test_verify_status_digest_detects_content_changes_in_already_dirty_files(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    untracked = tmp_path / "untracked.txt"
    tracked.write_text("committed\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)

    tracked.write_text("dirty one\n")
    untracked.write_text("untracked one\n")
    first = _verification_status_digest(tmp_path)

    tracked.write_text("dirty two\n")
    second = _verification_status_digest(tmp_path)
    untracked.write_text("untracked two\n")
    third = _verification_status_digest(tmp_path)

    assert first != second
    assert second != third


def test_static_type_gate_is_zero_error_core_with_explicit_expansion_debt():
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    mypy = config["tool"]["mypy"]
    expected_core = {
        "src/machinist/config.py",
        "src/machinist/lifecycle.py",
        "src/machinist/workspace.py",
        "src/machinist/process.py",
        "src/machinist/verification.py",
    }

    assert set(mypy["files"]) == expected_core
    assert (
        "TODO(static-types): expand this zero-error gate"
        in (_ROOT / "pyproject.toml").read_text()
    )
    assert "ignore_errors" not in mypy
    assert "disable_error_code" not in mypy


@pytest.mark.skipif(
    not (_ROOT / ".github").exists(),
    reason="repository-only test (paths absent from sdist)",
)
def test_sdist_uses_an_explicit_safe_allowlist():
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    sdist_config = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "exclude" not in sdist_config
    assert set(sdist_config["include"]) == _SDIST_INCLUDES

    subprocess.run(["uv", "build", "--sdist", "--no-sources"], cwd=_ROOT, check=True)
    version = config["project"]["version"]
    sdist = _ROOT / "dist" / f"agentmachinist-{version}.tar.gz"
    assert sdist.is_file()
    with tarfile.open(sdist) as tf:
        names = tf.getnames()

    package_root = f"agentmachinist-{version}/"
    relative_names = [name.removeprefix(package_root) for name in names]
    allowed_top_level = {
        path.removeprefix("/").split("/", 1)[0] for path in _SDIST_INCLUDES
    } | {"PKG-INFO"}
    assert {name.split("/", 1)[0] for name in relative_names} <= allowed_top_level

    assert f"agentmachinist-{version}/src/machinist/workflows.py" in names
    assert f"agentmachinist-{version}/tests/test_lifecycle.py" in names
    assert f"agentmachinist-{version}/uv.lock" in names
    assert not any(name.startswith(".claude/") for name in relative_names)
    assert not any("/.git/" in f"/{name}/" for name in relative_names)
    assert not any("worktrees/" in name for name in relative_names)
