import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "smoke-wheel.sh"


def test_smoke_wheel_script_selects_versioned_wheel_and_templates():
    text = _SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert "tomllib" in text
    assert "agentmachinist-${VERSION}-py3-none-any.whl" in text or 'agentmachinist-${VERSION}-py3-none-any.whl' in text
    assert "machinist --version" in text
    assert "spec-prompt.md" in text
    assert "machinist-approve.yml" in text
    assert "find dist" not in text


@pytest.mark.skipif(
    not (_ROOT / ".github").exists(),
    reason="repository-only test (paths absent from sdist)",
)
def test_sdist_omits_dogfood_and_design_tree(tmp_path):
    subprocess.run(["uv", "build", "--sdist"], cwd=_ROOT, check=True)
    version = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["version"]
    sdist = _ROOT / "dist" / f"agentmachinist-{version}.tar.gz"
    assert sdist.is_file()
    names = tarfile.open(sdist).getnames()
    joined = "\n".join(names)
    assert f"agentmachinist-{version}/src/machinist/workflows.py" in names
    assert f"agentmachinist-{version}/tests/test_lifecycle.py" in names
    assert f"agentmachinist-{version}/uv.lock" in names
    assert ".machinist/specs/" not in joined
    assert "docs/superpowers/" not in joined
    assert "docs/onboarding.html" not in joined
    assert "tasks/todo.md" not in joined
    assert ".github/workflows/" not in joined
    assert "AgentMachinist-Prompt.md" not in joined
