#!/usr/bin/env bash
# Install the wheel that matches pyproject.toml and assert packaged templates.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(uv run python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")"
WHEEL="dist/agentmachinist-${VERSION}-py3-none-any.whl"
if [[ ! -f "$WHEEL" ]]; then
  echo "expected $WHEEL after uv build" >&2
  exit 1
fi
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
ENV_DIR="$TMP_DIR/venv"
uv venv "$ENV_DIR"
uv pip install --python "$ENV_DIR/bin/python" "$WHEEL"
PATH="$ENV_DIR/bin:$PATH" machinist --version
"$ENV_DIR/bin/python" -c "from importlib.resources import files; t = files('machinist') / 'templates'; assert (t / 'github' / 'machinist-approve.yml').is_file(); assert (t / 'spec-prompt.md').is_file(); assert (t / 'implement-prompt.md').is_file()"
