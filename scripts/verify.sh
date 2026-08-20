#!/usr/bin/env bash
# Canonical local and CI verification gate for a releasable checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

status_digest() {
  {
    # Status captures path/type transitions; the binary diff captures the
    # exact staged and unstaged content of every tracked path.
    git -c core.fsmonitor=false -c diff.external= \
      status --porcelain=v1 -z --untracked-files=all
    git -c core.fsmonitor=false -c diff.external= \
      diff --binary --no-ext-diff --no-textconv HEAD --

    # Git diff omits untracked files. Hash each one without filters, retaining
    # its NUL-delimited path so pre-existing dirty files cannot change invisibly.
    while IFS= read -r -d '' path; do
      printf '%s\0' "$path"
      if [[ -L "$path" ]]; then
        printf 'symlink\0%s\0' "$(readlink "$path")"
      elif [[ -f "$path" ]]; then
        git hash-object --no-filters -- "$path"
      else
        printf 'non-regular\0'
      fi
    done < <(git -c core.fsmonitor=false ls-files --others --exclude-standard -z)
  } | git hash-object --stdin
}

check_workflows() {
  uv run --frozen machinist sync-workflows --check
}

check_format() {
  uv run --frozen ruff format --check src tests
}

check_lint() {
  uv run --frozen ruff check src tests
}

check_types() {
  uv run --frozen mypy
}

check_coverage() {
  local coverage_dir coverage_status
  coverage_dir="$(mktemp -d "${TMPDIR:-/tmp}/agentmachinist-coverage.XXXXXX")"
  if COVERAGE_FILE="$coverage_dir/.coverage" \
    uv run --frozen pytest -o addopts= --tb=short \
      --cov=machinist --cov-report=term-missing --cov-fail-under=80; then
    coverage_status=0
  else
    coverage_status=$?
  fi
  rm -f "$coverage_dir/.coverage"
  rmdir "$coverage_dir"
  return "$coverage_status"
}

build_and_smoke() {
  rm -rf "$ROOT/dist"
  # Build with the Hatchling version and transitive dependencies already
  # installed from the frozen lock, not a second network-resolved environment.
  uv build --no-sources --no-build-isolation
  bash scripts/smoke-wheel.sh
}

verify_all() {
  local before_status after_status
  before_status="$(status_digest)"

  uv lock --check
  uv sync --frozen
  check_workflows
  check_format
  check_lint
  check_types
  check_coverage

  after_status="$(status_digest)"
  if [[ "$before_status" != "$after_status" ]]; then
    echo "verification changed tracked or untracked source files; refusing to build" >&2
    git status --short >&2
    exit 1
  fi

  build_and_smoke
}

case "${1:-all}" in
  all) verify_all ;;
  workflows) check_workflows ;;
  format) check_format ;;
  lint) check_lint ;;
  types) check_types ;;
  coverage) check_coverage ;;
  package) build_and_smoke ;;
  *)
    echo "usage: $0 [all|workflows|format|lint|types|coverage|package]" >&2
    exit 2
    ;;
esac
