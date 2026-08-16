# AgentMachinist — M1: spec phase end-to-end

Design: docs/superpowers/specs/2026-08-16-agentmachinist-design.md
Decisions confirmed 2026-08-16: approval stays label-based; GitHub layer
stays on the gh wrapper (auth portability beats a PyGithub dependency).

## Plan (M1) — COMPLETE

- [x] TDD github: default_branch() via gh repo view
- [x] TDD workspace: provision (worktree + clone), commit_all, push,
      cleanup policies — tested against real git repos in tmp_path
- [x] TDD phases/spec: render_spec_prompt + run_spec_phase orchestration;
      cleanup-on-failure; empty-spec guard
- [x] TDD cli: `machinist spec <n>` wired; error types render as one-liners
- [x] Spec prompt template (templates/spec-prompt.md, string.Template)
- [x] Full suite green (59 passed); offline smoke of the real command verified

## Resuming From Here

- Done: M0 + M1. `machinist spec <n>` is fully wired: issue → harness spec
  (read-only print mode, isolated worktree) → spec file → branch → push →
  approval label ensured → draft PR with Closes #<n>. Not yet exercised
  against a real GitHub issue (needs one to exist; creates a real PR).
- Next: M2 — phases/execute.py + `machinist run <n>`: provision workspace on
  the existing agent/issue-<n> branch, read the spec file, harness.implement
  (acceptEdits), run tests.command gate, commit/push, mark PR ready
  (gh pr ready). Then M3: watch daemon polling trigger label + approved PRs.
- Blockers: none.
- Assumptions: spec-phase PR body uses 'Closes #<n>' so merging the
  implementation closes the issue. opencode/pi/codex headless flags remain
  best-effort defaults (harness.command overrides).
