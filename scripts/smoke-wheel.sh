#!/usr/bin/env bash
# Exercise both distributions and a first-run project using only installed files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VERSION="$("$PYTHON_BIN" -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")"
WHEEL="dist/agentmachinist-${VERSION}-py3-none-any.whl"
SDIST="dist/agentmachinist-${VERSION}.tar.gz"
if [[ ! -f "$WHEEL" ]]; then
  echo "expected $WHEEL after uv build" >&2
  exit 1
fi
if [[ ! -f "$SDIST" ]]; then
  echo "expected $SDIST after uv build" >&2
  exit 1
fi
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
ENV_DIR="$TMP_DIR/venv"
uv venv "$ENV_DIR"
uv pip install --python "$ENV_DIR/bin/python" "$WHEEL"
uv pip check --python "$ENV_DIR/bin/python"
PATH="$ENV_DIR/bin:$PATH" machinist --version
"$ENV_DIR/bin/python" -c "from importlib.resources import files; t = files('machinist') / 'templates'; assert (t / 'github' / 'machinist-approve.yml').is_file(); assert (t / 'spec-prompt.md').is_file(); assert (t / 'implement-prompt.md').is_file()"

PROJECT_DIR="$TMP_DIR/project"
mkdir "$PROJECT_DIR"
git -C "$PROJECT_DIR" init -q -b main
(
  cd "$PROJECT_DIR"
  PATH="$ENV_DIR/bin:$PATH" machinist init --no-input --no-workflows \
    --harness codex --spec-source local --notifications disabled
  PATH="$ENV_DIR/bin:$PATH" machinist config validate
  PATH="$ENV_DIR/bin:$PATH" machinist status --local --json > "$TMP_DIR/status.json"
  PATH="$ENV_DIR/bin:$PATH" machinist rehearse > "$TMP_DIR/rehearsal.txt"
)
"$ENV_DIR/bin/python" -c "import json; p = json.load(open('$TMP_DIR/status.json')); assert p['schema_version'] == 1; assert p['current'] == []"
test -f "$PROJECT_DIR/machinist.yaml"
test -f "$PROJECT_DIR/.machinist/specs/.gitkeep"
grep -Fxq '/.machinist/runs/' "$PROJECT_DIR/.gitignore"
grep -Fq 'execute verified' "$TMP_DIR/rehearsal.txt"
grep -Fq 'review complete' "$TMP_DIR/rehearsal.txt"
grep -Fq 'local integration complete' "$TMP_DIR/rehearsal.txt"

# A no-origin project exercises the new optional check from the installed wheel.
# Every Harness invocation is a deterministic probe; generation is an error.
LOCAL_DIR="$TMP_DIR/local-project"
mkdir "$LOCAL_DIR"
cat > "$TMP_DIR/readiness-harness" <<'SH'
#!/bin/sh
case "$*" in
  --version) echo 'codex smoke fixture' ;;
  'login status') echo 'logged in' ;;
  *--help*) echo 'usage: codex exec --sandbox --ephemeral -c' ;;
  *) echo 'unexpected Harness invocation' >&2; exit 99 ;;
esac
SH
chmod +x "$TMP_DIR/readiness-harness"
"$ENV_DIR/bin/python" - "$LOCAL_DIR" "$TMP_DIR/readiness-harness" <<'PYCONFIG'
import sys
from pathlib import Path
import yaml
root = Path(sys.argv[1])
(root / "machinist.yaml").write_text(yaml.safe_dump({
    "harness": {"name": "codex", "command": sys.argv[2]},
    "tests": {"command": "true"},
    "workspace": {"root": str(root.parent / "workshops")},
}))
PYCONFIG
git -C "$LOCAL_DIR" init -q -b main
git -C "$LOCAL_DIR" -c core.hooksPath=/dev/null add machinist.yaml
git -C "$LOCAL_DIR" -c core.hooksPath=/dev/null -c commit.gpgSign=false \
  -c user.name=Smoke -c user.email=smoke@example.invalid commit -qm baseline
"$ENV_DIR/bin/python" - "$LOCAL_DIR" "$ENV_DIR/bin/machinist" <<'PYLOCAL'
import json
import subprocess
import sys
from pathlib import Path
root = Path(sys.argv[1])
def snapshot():
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
before = snapshot()
result = subprocess.run([sys.argv[2], "doctor", "--local", "--json"],
                        cwd=root, text=True, capture_output=True, check=True)
report = json.loads(result.stdout)
assert report["ok"], report
assert any(c["name"] == "verification execution" and "not run" in c["detail"]
           for c in report["checks"]), report
assert snapshot() == before, "default local readiness changed the repository"
assert not (root / ".machinist").exists()
result = subprocess.run([sys.argv[2], "doctor", "--local", "--run-gates", "--json"],
                        cwd=root, text=True, capture_output=True, check=True)
report = json.loads(result.stdout)
assert report["ok"], report
assert any(c["name"] == "verification execution" and c["level"] == "PASS"
           for c in report["checks"]), report
assert not (root / ".machinist").exists()
print("installed local readiness: no-origin/read-only and explicit gates passed")
PYLOCAL

SDIST_ENV="$TMP_DIR/sdist-venv"
uv venv "$SDIST_ENV"
uv pip install --python "$SDIST_ENV/bin/python" "$SDIST"
uv pip check --python "$SDIST_ENV/bin/python"
PATH="$SDIST_ENV/bin:$PATH" machinist --version | grep -F "$VERSION"
