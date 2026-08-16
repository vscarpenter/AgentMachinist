# AgentMachinist — M0 bootstrap

Design: docs/superpowers/specs/2026-08-16-agentmachinist-design.md (approved 2026-08-16)

## Plan (M0) — COMPLETE

- [x] Commit design doc
- [x] pyproject.toml + uv sync (click, pydantic, pyyaml; pytest dev)
- [x] TDD config: tests/test_config.py red → src/machinist/config.py green
- [x] TDD github: tests/test_github.py red → src/machinist/github.py green
- [x] TDD harness: tests/test_harness.py red → harness/base.py + adapters + registry green
- [x] TDD cli init: tests/test_cli.py red → cli.py + packaged templates green
- [x] Templates: machinist.yaml, machinist-spec.yml, machinist-approve.yml
- [x] README.md
- [x] Full suite green (38 passed); live `machinist init` verified in scratch dir

## Resuming From Here

- Done: M0 complete. Config schema/loader, gh wrapper (draft PRs, labels,
  issue reads), harness registry (4 adapters), working `machinist init`,
  workflow + config templates, README. All committed on main (not pushed).
- Next: M1 — `phases/spec.py` + real `machinist spec <n>`: get_issue →
  harness.generate_spec → write .machinist/specs/issue-<n>-spec.md →
  branch → commit → create_draft_pr. Needs workspace.py (branch handling)
  and a spec-prompt template. Then M2 (execute) and M3 (watch), per the
  design doc's milestones.
- Blockers: none.
- Assumptions: opencode/pi/codex headless flags are best-effort defaults
  (overridable via harness.command); verify against real CLIs during M1.
  Claude Code flags (-p, --output-format text, --permission-mode
  acceptEdits) are current as of 2026-08.
