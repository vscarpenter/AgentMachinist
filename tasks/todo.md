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

## Issue #2 — machinist status: COMPLETE

- [x] TDD github: issues_with_label + open_machinist_prs (+ PullRequest type)
- [x] TDD phases/status: pipeline_status classification (awaiting spec /
      awaiting approval / approved / in review; no double-listing)
- [x] TDD cli: render status; drop status from stub test
- [x] Suite green (71); live-verified against real repo (shows PR #3)

## Dogfood log (issue #1)

- Phase 1 ran end-to-end 2026-08-16: draft PR #3, 62-line spec grounded in
  real files/error strings; worktree auto-cleaned on success.
- Observation: PR #3 was marked ready-for-review by hand instead of labeled
  machinist:approved — the draft→ready gesture is what users reach for.
  Consider (post-M2): treat ready-without-label as needing a nudge, or
  revisit approval signal UX with Vinny.

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
