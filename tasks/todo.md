# AgentMachinist — reliability and usability hardening (IN PROGRESS)

Spec: `docs/superpowers/specs/2026-08-17-reliability-and-usability-hardening.md`

- [ ] Bind Approval to the exact Spec commit and surface stale Approval.
- [ ] Persist exclusive local Task Runs with explicit retry/recovery.
- [ ] Make Spec dispatch ownership and generated workflows config-driven.
- [ ] Add `approve`, `doctor`, `sync-workflows`, and `retry` commands.
- [ ] Add Spec/Execute custody checks and typed quality-gate failures.
- [ ] Strengthen CI, release identity, wheel smoke, and versioning.
- [ ] Reconcile all onboarding, operations, trust, support, and maintainer docs.
- [ ] Run full tests, package verification, and handbook checks.

# AgentMachinist — beta-readiness sweep (COMPLETE)

## Plan

- [x] A: heartbeat — harness base emits on_progress during long runs;
      CLI prints "still working (Xm elapsed)" every 30s
- [x] B: init ensures trigger + approved labels (warn-only on failure)
- [x] C: verify opencode/pi/codex headless flags against real docs; fix argv
- [x] D: run refuses a non-draft (already implemented) PR without --force
- [x] Push A-D, then dogfood E through the FULL watch loop: file issue
      "watch: macOS notification on dispatch failure" with agent-task label,
      run machinist watch (real daemon), Vinny approves via
      /machinist-execute comment (also first live test of approve workflow)

Sweep complete 2026-08-16. Lifecycle #2 (issue #4 -> PR #5, merged ee97673)
ran fully daemon-driven: watch specced, waited at the gate, implemented,
test-gated, flipped ready; human only approved and merged. Live findings
fixed same-day: comment trigger now whitespace-tolerant (contains, not
startsWith); spec PR body asks to leave the PR as draft (instinct fired
2-for-2). C note: spec phase is now provably read-only on all four
harnesses (plan agent / -xt edit,write / --sandbox read-only). Merged
main: 117 tests. Onboarding handbook updated for PyPI + new features.
Beta-ready: hand testers the handbook; known limits are macOS-tested-only
and claude-code as the proven harness.

# AgentMachinist — M3: watch daemon (COMPLETE)

## Plan (M3)

- [x] TDD status: labeled+non-draft PR classifies "in review" (dogfood nit);
      StatusRow gains issue_number (parsed from branch for PR rows)
- [x] TDD spec: PR body says explicitly GitHub's review-Approve button is
      not the approval mechanism (dogfood UX finding 2)
- [x] TDD phases/watch: WatchState + watch_once — dispatch awaiting-spec →
      run_spec, approved(draft) → run_execute; record failures, never
      re-dispatch a failed issue; errors become events, daemon survives
- [x] TDD cli: watch with --once (single pass) and poll loop; stub test dies
- [x] Docs: README + getting-started "what's next" reflect all commands live
- [x] Suite green (101); live smoke passed: machinist watch --once on empty pipeline

M3 complete 2026-08-16. All designed commands ship. Local branch
agent/issue-1 deleted post-merge. Remaining ideas (backlog, not
milestones): exercise the clone strategy in anger; CI spec workflow
needs ANTHROPIC_API_KEY to be tried; PyPI publish; failure-notification
(watch currently just logs).

# AgentMachinist — M2: execute phase (COMPLETE)

## Plan (M2)

- [x] TDD github: mark_ready(number) via gh pr ready
- [x] TDD workspace: provision from remote-only branch; ff local branch to
      origin when behind; has_changes(path)
- [x] TDD phases/execute: run_execute_phase — approval-label guard, spec file
      read, harness.implement (acceptEdits), no-changes guard, tests.command
      gate (fail = keep workspace, no push), commit/push, mark PR ready
- [x] implement-prompt.md template
- [x] TDD cli: wire `machinist run <n>`; stubs test shrinks to watch only
- [x] Suite green (86); pushed; live dogfood SUCCEEDED 2026-08-16:
      machinist run 1 implemented the guide per spec (257-line
      docs/getting-started.md + 91-line drift tests + README link),
      passed the pytest gate, pushed ba0b3cd, marked PR #3 ready.
      Branch suite independently verified (65 passed on fresh checkout).
      Full issue→spec→approve→execute loop has now run end-to-end.
      Remaining human step: review + merge PR #3 (closes #1).

Next milestone: M3 — `machinist watch` polling daemon (pipeline_status
reads already provide the polling backbone; add dispatch + de-dup so an
in-flight issue isn't re-run each poll).

# AgentMachinist — M1: spec phase end-to-end (complete)

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
- UX finding 2: "approve" wording collides with GitHub's PR-review Approve
  button, which GitHub blocks on your own PRs ("Pull request authors can't
  approve their own pull requests") — vindicates the label design (solo devs
  are always the author) but the PR body / docs should say explicitly:
  "GitHub's review Approve button is NOT the mechanism; add the label or
  comment /machinist-execute." Phase 2 for issue #1 completed via label
  2026-08-16; machinist status shows 'approved'.

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

## Lifecycle #1 complete (2026-08-16)

Issue #1 → spec (PR #3 draft) → approved via label → implemented by
machinist run → pytest gate → ready-for-review → human merge (ff526e1)
→ issue auto-closed, remote branch auto-deleted. Merged main: 92 tests
green including the agent's 6 drift tests. The system built its own
getting-started guide as its first shipped deliverable.
