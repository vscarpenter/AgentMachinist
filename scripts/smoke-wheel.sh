#!/usr/bin/env bash
# Exercise both distributions and a first-run project using only installed files.
set -euo pipefail
export MACHINIST_NO_UPDATE_CHECK=1
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

smoke_guided_rehearsal() {
  local installed_env="$1"
  local distribution="$2"
  local transcript="$TMP_DIR/${distribution}-guided-rehearsal.txt"
  (
    cd "$TMP_DIR"
    printf 'y\ny\n' | PATH="$installed_env/bin:$PATH" machinist rehearse --guided > "$transcript"
  )
  "$installed_env/bin/python" - "$transcript" "$distribution" <<'PYGUIDED'
import re
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
assert "Free controller rehearsal. No model calls or API usage." in text, text
specs = re.findall(r"Task: T1; plan commit: ([0-9a-f]{40})", text)
assert len(specs) == 2 and specs[0] == specs[1], text
candidate = re.search(r"Change ready for your review: ([0-9a-f]{40})", text)
assert candidate and candidate.group(1) != specs[0], text
plan = (
    "Change answer() in feature.py to return 2. "
    "Update tests/test_feature.py so its existing test asserts 2. "
    "Run the configured unittest gate. Do not add dependencies."
)
approval = "Approve this exact Spec to implement the disposable Task?"
acceptance = "Accept this reviewed change into the disposable repository?"
assert plan in text and "Saved plan:" in text, text
assert "-    return 1" in text and "+    return 2" in text, text
assert "tests/test_feature.py" in text and "Saved diff:" in text, text
assert "Checks passed." in text, text
assert "Review: Candidate matches the rehearsal Spec" in text, text
assert "Saved review:" in text, text
assert "Acceptance changes only this disposable repository." in text, text
assert text.index(plan) < text.index(approval) < text.index("Change ready for your review:"), text
assert text.index("Checks passed.") < text.index(acceptance), text
assert text.index("Review: Candidate matches the rehearsal Spec") < text.index(acceptance), text
assert text.index(acceptance) < text.index("local integration complete"), text
assert "Rehearsal passed" in text and "sample retained" not in text, text
print(f"installed {sys.argv[2]} guided rehearsal: exact plan, checked/reviewed acceptance, and integration passed")
PYGUIDED
}

smoke_guided_rehearsal "$ENV_DIR" wheel

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
git -C "$LOCAL_DIR" config user.name Smoke
git -C "$LOCAL_DIR" config user.email smoke@example.invalid
git -C "$LOCAL_DIR" config maintenance.auto false
git -C "$LOCAL_DIR" -c core.hooksPath=/dev/null add machinist.yaml
git -C "$LOCAL_DIR" -c core.hooksPath=/dev/null -c commit.gpgSign=false \
  -c user.name=Smoke -c user.email=smoke@example.invalid commit -qm baseline
"$ENV_DIR/bin/python" - "$LOCAL_DIR" "$ENV_DIR/bin/machinist" <<'PYLOCAL'
import json
import subprocess
import sys
from pathlib import Path
import yaml
root = Path(sys.argv[1]).resolve()
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
assert snapshot() == before, "explicit readiness gates changed the repository"
result = subprocess.run([sys.argv[2], "doctor", "--local", "--fresh-workshop", "--json"],
                        cwd=root, text=True, capture_output=True, check=True)
report = json.loads(result.stdout)
assert report["ok"], report
assert any(c["name"] == "fresh Workshop" and c["level"] == "PASS"
           and "disposable local clone" in c["detail"] for c in report["checks"]), report
assert any(c["name"] == "verification execution" and c["level"] == "PASS"
           and "fresh Workshop" in c["detail"] for c in report["checks"]), report
assert snapshot() == before, "fresh Workshop readiness changed the repository"
assert not (root / ".machinist").exists()
assert not subprocess.run(["git", "remote"], cwd=root, text=True,
                          capture_output=True, check=True).stdout.strip()

# Save fixture settings explicitly, then prove inspection creates no other state.
saved = root / ".machinist/runs/local/config.yaml"
saved.parent.mkdir(parents=True)
settings = yaml.safe_load((root / "machinist.yaml").read_text())
settings.update({"review": {"enabled": True},
                 "github": {"spec_source": "local", "manage_workflows": False}})
settings["harness"]["model"] = "saved-smoke-model"
saved.write_text(yaml.safe_dump(settings))
before_local = snapshot()
result = subprocess.run([sys.argv[2], "config", "show", "--local", "--json"],
                        cwd=root, text=True, capture_output=True, check=True)
shown = json.loads(result.stdout)
assert shown["configuration"]["workflow"] == "local", shown
assert shown["configuration"]["source"] == "saved", shown
assert shown["configuration"]["path"] == str(saved), shown
assert shown["harness"]["spec"]["model"] == "saved-smoke-model", shown
assert shown["verification"]["gates"][0]["command"] == "true", shown
assert snapshot() == before_local, "config show --local changed the repository"
assert not (saved.parent / "tasks").exists()
print("installed local readiness: no-origin/read-only, explicit/fresh gates, and saved settings passed")
PYLOCAL

SDIST_ENV="$TMP_DIR/sdist-venv"
uv venv "$SDIST_ENV"
uv pip install --python "$SDIST_ENV/bin/python" "$SDIST"
uv pip check --python "$SDIST_ENV/bin/python"
PATH="$SDIST_ENV/bin:$PATH" machinist --version | grep -F "$VERSION"
(
  cd "$TMP_DIR"
  PATH="$SDIST_ENV/bin:$PATH" machinist rehearse > "$TMP_DIR/sdist-rehearsal.txt"
)
grep -Fq 'execute verified' "$TMP_DIR/sdist-rehearsal.txt"
grep -Fq 'review complete' "$TMP_DIR/sdist-rehearsal.txt"
grep -Fq 'local integration complete' "$TMP_DIR/sdist-rehearsal.txt"
smoke_guided_rehearsal "$SDIST_ENV" sdist
