# AGENTS.md

`CLAUDE.md` is the authoritative guide for this repository — architecture, core
invariants, domain language, and conventions. Read it first. This file exists so
harnesses that look for `AGENTS.md` by convention (Codex, OpenCode, Pi) find the
same entry point, and it must not restate rules that could drift from it.

## Commands

- `uv sync` — install (creates `.venv`)
- `uv run pytest` — full suite (~5s, no network)
- `uv run machinist --help` — run the CLI from source
- `bash scripts/verify.sh` — the gates CI runs (format, lint, types, coverage)

## Code map

- `src/machinist/` — controller source; see the module map in `CLAUDE.md`
- `tests/` — pytest suite; `test_docs.py` enforces documentation drift
- `docs/` — user documentation and the trust model
- `.github/workflows/` — CI plus the two managed `machinist-*` workflows,
  which are projected from `machinist.yaml` and never hand-edited

## Conventions

- Absolute imports from the `machinist` package; test files named `test_*.py`.
- TDD is the house style: behavior changes start with a failing contract test.
- Conventional-commit prefixes with optional scope (`feat(cli):`, `docs(spec):`).
- The controller — never the harness — owns commits, pushes, and PR transitions.
  See "Core invariants" in `CLAUDE.md` before changing any phase module.
