from pathlib import Path

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
