# AgentMachinist — M0 bootstrap

Design: docs/superpowers/specs/2026-08-16-agentmachinist-design.md (approved 2026-08-16)

## Plan (M0)

- [ ] Commit design doc
- [ ] pyproject.toml + uv sync (click, pydantic, pyyaml; pytest dev)
- [ ] TDD config: tests/test_config.py red → src/machinist/config.py green
- [ ] TDD github: tests/test_github.py red → src/machinist/github.py green
- [ ] TDD harness: tests/test_harness.py red → harness/base.py + adapters + registry green
- [ ] TDD cli init: tests/test_cli.py red → cli.py + packaged templates green
- [ ] Templates: machinist.yaml, machinist-spec.yml, machinist-approve.yml
- [ ] README.md
- [ ] Full suite green; commits per logical unit

## Resuming From Here

- Done: design approved (Python+Click; both spec-gen locales), spec doc written
- Next: commit spec doc, then TDD implementation per plan above
- Blockers: none
- Assumptions: harness headless flags (opencode/pi/codex) are best-effort
  defaults, overridable via `harness.command`; adjust when verified against
  real CLIs in M1
