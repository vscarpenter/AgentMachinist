# Documentation reconciliation for comprehensive review (COMPLETE)

- [x] Make initialization review, commit, and push explicit in canonical docs.
- [x] Capture the original `.machinist/runs/` ignore risk during the audit.
- [x] Archive the superseded visual handbook in favor of the first-run guide.
- [x] Correct harness authentication commands and advanced config coverage.
- [x] Document successful-Spec revision and abandonment with the final CLI.
- [x] Replace retained-workspace inspection-only guidance with explicit resume
      and fresh recovery modes.
- [x] Replace the manual runtime-ignore step after `machinist init` began
      managing it idempotently.
- [x] Document launchd service lifecycle, queue admission, cancellation,
      amendment, preview, and fresh/resume operator workflows.
- [x] Document config tooling, phase harnesses, instruction overlays, named
      gates, notifications, budgets, limits, JSON/offline evidence, and the
      repository portfolio.

# AgentMachinist — build system hardening (COMPLETE)

Plan: `docs/superpowers/plans/2026-08-17-build-system-hardening.md`

- [x] Task 1: contract test — checked-in workflows match config projection
- [x] Task 2: release/docs tests follow pyproject version (no frozen 0.2.0)
- [x] Task 3: shared `scripts/smoke-wheel.sh` used by CI and release
- [x] Task 4: sdist excludes dogfood and design trees
- [x] Task 5: `github.spec_install` (`pypi` | `checkout`) projection
- [x] Task 6: this repo's Spec workflow runs from the checkout
- [x] Task 7: CI matrix 3.12/3.13, uv pinned 0.12.5, unmuted pytest
- [x] Task 8: getting-started/architecture/runbook/CONTRIBUTING/changelog
- [x] Task 9: full gate — 175 tests, drift check clean, build + wheel
      smoke exit 0, sdist spot check shows only the package template
- [x] Task 11 (optional): monthly Dependabot for Actions and uv.lock
- [x] First-run guide surfaces `spec_install` (CI steps, config sheet,
      spec-owner annotation); doc tests green

Completed 2026-08-18 on `worktree-build-system-hardening` (Tasks 1–8+11
landed in a prior session; this session verified the gate and updated
the guide). Task 10 (TestPyPI rehearsal) intentionally skipped. Version
stays 0.2.0; changes ride the changelog's pending section until the
next release. Pushed as PR #9; human review + merge remain.

# AgentMachinist — reliability and usability hardening (COMPLETE)

Spec: `docs/superpowers/specs/2026-08-17-reliability-and-usability-hardening.md`

- [x] Bind Approval to the exact Spec commit and surface stale Approval.
- [x] Persist exclusive local Task Runs with explicit retry/recovery.
- [x] Make Spec dispatch ownership and generated workflows config-driven.
- [x] Add `approve`, `doctor`, `sync-workflows`, and `retry` commands.
- [x] Add Spec/Execute custody checks and typed quality-gate failures.
- [x] Strengthen CI, release identity, wheel smoke, and versioning.
- [x] Reconcile all onboarding, operations, trust, support, and maintainer docs.
- [x] Run full tests, package verification, and handbook checks.

Completed 2026-08-17 on `codex/reliability-and-docs-hardening`: 167 tests
passed; `agentmachinist-0.2.0` sdist and wheel built; isolated wheel install,
CLI version, packaged templates, doctor, and workflow drift check all passed.
Publishing and pushing remain explicitly out of scope.

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
agent/issue-1 deleted post-merge. Historical backlog at that point included
exercising clone strategy in anger, the CI Spec provider secret, publishing,
and failure notifications. Notification delivery is now implemented with
desktop, argv-command, and webhook backends; this note is retained only as the
M3 snapshot.

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

Historical next step (subsequently completed): M3 added the `machinist watch`
polling dispatcher and local claim/de-duplication. See the completed M3 section
above for the shipped behavior.

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

## Historical M1 handoff (superseded)

At the end of M1, Spec was wired but Execute and Watch were still the stated
next milestones. Both were subsequently delivered and live-dogfooded; the M2
and M3 completion sections above are authoritative. The original assumption
that the Spec PR carries `Closes #<n>` remains part of the shipped lifecycle.

## Lifecycle #1 complete (2026-08-16)

Issue #1 → spec (PR #3 draft) → approved via label → implemented by
machinist run → pytest gate → ready-for-review → human merge (ff526e1)
→ issue auto-closed, remote branch auto-deleted. Merged main: 92 tests
green including the agent's 6 drift tests. The system built its own
getting-started guide as its first shipped deliverable.

## Interactive init wizard (2026-08-20, complete)

Approved design: `machinist init` asks first-run questions in a TTY
(dispatch mode, managed workflows, harness, test gate, notifications),
each with a one-line explanation; flags pre-answer, `--no-input` and
non-TTY keep today's silent behavior. New flags `--spec-source`,
`--notifications`, `--no-input`. Also fixes the stale tests.command
comment tail.

- [x] RED: CLI tests for wizard flows, new flags, no-input, comment tail
- [x] GREEN: init_wizard.py + cli.py wiring + _render_init_config params
- [x] Docs: README, docs/getting-started.md, CHANGELOG.md
- [x] Full suite green, commit

## Release 0.5.0 (2026-08-20)

Interactive `machinist init` wizard shipped as 0.5.0. Version bumped in
pyproject/uv.lock, changelog dated, release-pinned references updated in
README, CLAUDE.md, first-run-guide.html, and explainer.html. Pushed to
main and published the v0.5.0 GitHub Release; the Trusted Publishing
workflow re-runs the suite, smoke-tests the wheel, and publishes to PyPI.

## Release 0.6.0 prep (2026-08-20)

Retry loop added around the release workflow's published-package smoke
test (10x15s; version mismatch still fails hard) with a contract test in
test_release_workflows.py. First-run guide updated to document the
interactive init questions, pre-answer flags, and --no-input; version
pins bumped to 0.6.0 everywhere test_docs.py checks.

## Release 0.7.0 (2026-08-20)

`machinist update-check` (plus the advisory `doctor` updates row) shipped
as 0.7.0, alongside the job-card TL;DR docs page. Version bumped in
pyproject/uv.lock, changelog dated, release-pinned references updated in
README, CLAUDE.md, first-run-guide.html, explainer.html, and
job-card.html. Pushed to main and published the v0.7.0 GitHub Release;
the Trusted Publishing workflow re-runs the suite, smoke-tests the wheel,
and publishes to PyPI.

## AI-native SDLC playbook adoptions (2026-08-23)

Design approved in-session: three changes pulled from the AI-native SDLC
playbook review. One conventional commit each, TDD, changelog entries
under a new Unreleased heading.

- [x] A `feat(spec)`: spec-prompt.md gains a `## Risks` section and the
      "implementable by a stranger" quality bar; test pins the section.
- [x] B `feat(harness)`: verification feedback loop — implement prompt
      lists resolved gates with run-and-iterate instructions;
      `harness.allowed_commands` attribute (base default `()`); claude-code
      implement argv appends `--allowedTools Bash(<cmd>)` / `Bash(<cmd>:*)`;
      config opt-out `verification.harness_may_run_gates` (default true);
      docs: harnesses.md, trust-model.md, config docs.
- [x] C `feat(execute)`: test-deletion custody check — `_ChangeSummary`
      gains deleted_files; `_enforce_change_limits` aborts when a deleted
      path matches test patterns unless `limits.allow_test_deletions`
      (default false).

All three shipped as commits on main (unreleased; changelog under
Unreleased, roll into the next release). Deferred by decision: the
agentic review phase (playbook Stage 5) — revisit after a few lifecycles
run with the feedback loop; the unique piece to build first is a
spec-compliance review pass, since the approved spec commit is already an
exact review contract.

## Release 0.8.0 (2026-08-23)

The three AI-native SDLC playbook adoptions shipped as 0.8.0: the harness
verification feedback loop (`verification.harness_may_run_gates`), the
test-deletion guard (`limits.allow_test_deletions`), and the spec
template's Risks section with the standalone-readable quality bar.
Version bumped in pyproject/uv.lock, changelog dated, release-pinned
references updated in README, CLAUDE.md, first-run-guide.html,
explainer.html, and job-card.html (including the "0.8 release" hero
kicker test_docs.py pins). Managed workflows needed no reprojection
(dogfood config uses spec_install: checkout). Pushed to main, CI green
on 7d71138, published the v0.8.0 GitHub Release; the Trusted Publishing
workflow's build, publish, verify-published, and release-assets jobs all
succeeded, PyPI serves 0.8.0, and the wheel, sdist, and SHA256SUMS are
attached to the release.

## Issue #16: worktree custody guard trips on the developer's shared .git/config

Reported by @anandvmp-pintlab. Root cause: `capture_git_custody` builds its
watch list from `common_dir`, which under `workspace.strategy: worktree`
resolves to the *parent repository's* `.git/`. The guard therefore byte-hashes
the developer's own `.git/config`, `.git/hooks/`, and `.git/info/`. Any benign
edit during the harness window aborts the phase. `clone` strategy is unaffected
because the workspace owns its `.git/`.

Approved scope: operator-facing fix plus semantic narrowing of the config
comparison. Hooks, `info/`, and `objects/` stay byte-strict.

- [x] Failing tests: benign config keys must not trip; execution/network keys must
- [x] Failing test: error names the changed keys and the strategy remediation
- [x] Git config parser (no subprocess; fail closed to byte comparison)
- [x] Narrow config comparison to changed-key analysis
- [x] Strategy-aware error message
- [x] Docs: trust-model section, operator-runbook entry, getting-started note
- [x] CHANGELOG
- [x] Full suite + ruff + mypy

Shipped as aa15c00 (guard) and b8a93db (docs) on
`fix/worktree-shared-config-custody`.

### Resuming From Here

Done: config files compare by sensitive key instead of bytes; hooks, `info/`,
and `objects/` stay byte-strict; Task Run records hold per-key value hashes
instead of a whole-file digest; custody version bumped to 2; trust-model,
runbook, getting-started, and changelog updated.

Next: push the branch and open the PR, then close issue #16 once merged.

Blockers: none. The suite is green (805 tests), ruff and mypy clean.

Assumptions worth revisiting:
- Classification is a denylist over sections and key leaf names. Git adds
  config keys every release, so a new execution-capable key would read as
  inert until the set is extended. An allowlist of provably inert keys would
  fail closed instead, at the cost of tripping on unfamiliar benign keys.
- `remote.origin.url` is pinned sensitive; other remotes' URLs are inert
  because the controller never fetches or pushes by remote name.
- The reporter's actual config writer was never identified. Nothing in the
  controller, the `claude-code` spec argv, or `gh` writes the shared config on
  git 2.55.0 and Claude Code 2.1.246, so it was likely their own tooling.
  Their answer on #16 may still be worth reading.

## Release 0.8.2 (2026-08-26)

Shipped the label-approval authorization fix (#20) and the test-determinism
work (#19). Both findings came from an external reviewer who traced the
approval flow end to end and ran the suite as root.

- #20: applying the approval label now requires write or admin access,
  resolved through the collaborator permission API and failing closed. Both
  approval paths record the approver's login on the approval comment. The
  marker format is unchanged; `test_github.py` pins that contract.
- #19: an autouse fixture pins a non-root UID in `test_service.py` (the UID
  check runs before every path check, so root saw the wrong error), and the
  cancellation-evidence test's cancel check now blocks until the child's
  marker appears so it cannot race its own fixture.
- PyPI serves 0.8.2; build, publish, verify-published, and release-assets all
  succeeded on 3e9889a.

### Open items

- Existing installs need `machinist sync-workflows` to adopt the actor check.
- The reviewer's third suggestion, an explicit approver allowlist, is
  deliberately deferred: it adds `machinist.yaml` surface and the write-access
  floor may be sufficient. Revisit if the floor proves too coarse.
- Custody key classification is still a denylist. See the assumptions logged
  under issue #16 above.
- Branch protection on `main` requires one approving review with
  `require_last_push_approval`, which a solo maintainer cannot satisfy: GitHub
  forbids self-approval. Three consecutive merges/pushes used the admin
  override. Either drop the required-review count to 0 and keep required
  status checks, or add a reviewer.
