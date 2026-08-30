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
)
"$ENV_DIR/bin/python" -c "import json; p = json.load(open('$TMP_DIR/status.json')); assert p['schema_version'] == 1; assert p['current'] == []"
test -f "$PROJECT_DIR/machinist.yaml"
test -f "$PROJECT_DIR/.machinist/specs/.gitkeep"
grep -Fxq '/.machinist/runs/' "$PROJECT_DIR/.gitignore"

SDIST_ENV="$TMP_DIR/sdist-venv"
uv venv "$SDIST_ENV"
uv pip install --python "$SDIST_ENV/bin/python" "$SDIST"
uv pip check --python "$SDIST_ENV/bin/python"
PATH="$SDIST_ENV/bin:$PATH" machinist --version | grep -F "$VERSION"
