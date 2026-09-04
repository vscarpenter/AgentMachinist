# Spec → Execute simplification pass (COMPLETE, awaiting push/PR)

Spec: `tasks/spec.md` (approved 2026-09-03). Plan:
`docs/superpowers/plans/2026-09-03-spec-to-execute-simplification.md`.
Branch: `refactor/spec-to-execute-simplification` from `9d27cfe`.

- [x] Task 1: Gate 1 trusts only workflow-authored markers (B-1, B-4)
- [x] Task 2: one Harness pass-through helper (E-3)
- [x] Task 3: delete the preview-ownership sidecar (E-5)
- [x] Task 4: Verification Gate owns its Evidence and messages (E-2, C-7, C-8, C-11)
- [x] Task 5: stop writing unread Evidence (D-1, C-9, A-3)
- [x] Task 6: collapse Execute's custody layer into the Workshop (C-1, E-1, C-4, C-10, E-7)
- [x] Task 7: delete the test-double-shaped Phase interface (C-2, D-4, A-4, D-5, B-5, A-6, C-6)
- [x] Task 8: Spec custody handoff (A-1, A-2, A-7, A-8, A-9)
- [x] Task 9: transitions and watch results (D-2, D-3)
- [x] Task 10: one recovery entry and one renderer at the CLI seam (F-1, F-2, F-5, F-6, F-7, F-9)
- [x] Task 11: docs, changelog, invariant 2/7 wording, `scripts/verify.sh`

### Resuming From Here

Done: Tasks 1–10 committed on `refactor/spec-to-execute-simplification`
(b093a32 … e7a878f), each red → green with the full suite passing. Task 11
docs written (CHANGELOG Unreleased, CLAUDE.md invariants 2 and 7, module map,
Current state); spec updated to defer F-5.

Next: push the branch and open the PR (needs the operator's go-ahead).
Whole-branch review verdict: "ready with fixes"; the three Important findings
(two operator-message strings, the dispatcher's real Workshop cancellation
test, the retry-Review notification note) and the minors are fixed in the
final commit.

Blockers: none.

Assumptions: inline execution (tasks share `execute.py`, `spec.py`,
`cli.py`); card 13, F-5, and the Worth-exploring cards stay out of scope.
Review keeps `getattr(github, "repo", None)` because it never binds the
client itself.

# AgentMachinist 0.12.1 release (COMPLETE)

- [x] Confirm 0.12.0 is already published: PyPI lists 0.12.0, release run
      33628066217 (build, publish, verify-published, release-assets) is green,
      and tag `v0.12.0` sits at `78ad4c7`.
- [x] Confirm the only unreleased change is PR #36 (three `fix(review):`
      commits), so the next release is the patch 0.12.1.
- [x] Align package metadata, changelog, current-release documentation, and
      rendered guides to 0.12.1.
- [x] Refresh the lockfile; managed workflows do not embed the version and
      still match their projection.
- [x] Run `bash scripts/verify.sh` from the release candidate.
- [x] Push `release/0.12.1`, open PR #37, merge, publish GitHub Release
      `v0.12.1`, and verify PyPI.

### Resuming From H### Resuming From Here

Done: 0.12.1 is released. Candidate `438863f` passed the release-grade gate
(1004 tests at 86.00% coverage, ruff format and lint, mypy, managed workflow
projection, and isolated wheel and sdist smoke installs). PR #37 merged as
`9d27cfe`, GitHub Release `v0.12.1` targets that commit, release run
33785402753 (build, publish, verify-published, release-assets) succeeded, and
PyPI serves 0.12.1 as the latest version with the wheel and sdist attached to
the release.

Next: nothing for this release. The simplification pass above (PR #38) is
the first unreleased change after it.

Blockers: none.

Assumptions: PR #36 contained only fixes to independent Review, so the change
was a patch bump rather than a minor one.

Completed 2026-09-03.

12.0 release preparation (COMPLETE)

- [x] Confirm the release version and inspect origin, branch protection, required
      checks, release automation, and secret metadata.
- [x] Align package metadata, changelog, current-release documentation, and
      rendered guides to 0.12.0.
- [x] Refresh the lockfile and managed workflow projections.
- [x] Run the complete local release gate from the release candidate commit.
- [x] Record the verified release candidate for protected-branch validation.

### Resuming From Here

Done: the release target is 0.12.0. Origin was unchanged when preparation
started, all required release surfaces are aligned, the lockfile is refreshed,
and managed workflows match their projection. Candidate `41cd33b` passed the
release-grade gate: 993 tests at 86.00% coverage, both distributions built, and
isolated wheel and sdist smoke installs reported 0.12.0. Repository protection
and publication automation were inspected without reading any secret value.

Next: push the final candidate and verify protected PR checks, merge, tag,
GitHub Release, PyPI, and live documentation as separate external states.

Blockers: none.

Assumptions: the deep-module refactor preserves public behavior and merits the
next minor version because it changes the documented architecture contract.

Completed 2026-09-02. No external state was changed during preparation.

# Documentation and TLDR reconciliation (COMPLETE)

- [x] Verify every ADR is tracked and follows the repository ADR format.
- [x] Audit every current Markdown and HTML document against the shipped CLI,
      configuration, lifecycle, trust model, and deep module ownership changes.
- [x] Mark historical design records clearly without rewriting their historical
      decisions as current behavior.
- [x] Simplify `docs/tldr.md` into one short setup path and two concise Task flows.
- [x] Extend documentation drift checks for ADR completeness and TLDR contracts.
- [x] Run focused documentation, config projection, and custody tests.
- [x] Run `bash scripts/verify.sh`.
- [x] Commit the complete documentation pass and record the final handoff state.

### Resuming From Here

Done: every file under `docs/` has been audited. Both ADRs are tracked,
format-complete, and linked from the documentation index. Current guides and
rendered pages are reconciled; historical records remain explicitly archived.
The TLDR is reduced to one setup path and two explicit Task flows. Focused tests
and browser rendering checks pass. The documentation unit is committed as
`d762f3e`; the full release-grade gate passed 993 tests with 85.98% coverage,
built both distributions, and smoke-tested wheel and sdist installs on Python
3.13.

Next: user review and the outstanding architecture comprehension gate. Push, PR
creation, merge, publication, and deployment remain outside this pass unless
explicitly requested.

Blockers: none.

Assumptions: this pass changes documentation and documentation tests only; shipped
CLI, configuration, persistence, trust, and external behavior remain unchanged.

Completed 2026-09-02. No external state was changed.

# Architecture deepening (COMPLETE)

Spec: `tasks/spec.md`

- [x] Record the approved architecture contract and ADR.
- [x] Deepen Task Run Evidence and migrate production readers.
- [x] Concentrate Task Run journal inventory in lifecycle.
- [x] Concentrate pipeline transition vocabulary and decisions.
- [x] Concentrate repository and PR custody.
- [x] Remove the duplicate Verification Gate fallback.
- [x] Move Phase Task Run construction out of `cli.py`.
- [x] Make validated configuration the source of starter/effective behavior.
- [x] Reconcile domain/module documentation and changelog.
- [x] Run focused tests and `bash scripts/verify.sh`.
- [x] Produce the change report and comprehension quiz.

### Resuming From Here

Done: all seven approved architecture recommendations are implemented and
committed on `codex/deepen-architecture`. The release-grade gate passes; exact
final verification evidence is recorded in the handoff.

Next: user review and the comprehension gate. Push, PR creation, merge,
publication, and deployment remain outside this pass unless explicitly requested.

Blockers: none for implementation. The comprehension gate remains required before
merge.

Assumptions: observable CLI, configuration, persistence, and trust behavior remain
unchanged; this pass deepens internal ownership rather than adding features.

Completed 2026-09-02. No external state was changed.

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

## Release 0.8.3 (2026-08-26)

Closed the delivery gap that let the 0.8.2 approval fix be installed without
taking effect, plus the documentation corrections that shipped alongside it.

- #22 / PR #27: `machinist watch` reports managed-workflow drift at startup and
  `machinist update-check` reports it alongside the release comparison. The
  advisory never blocks a command, degrades to silence on any probe failure,
  and stays out of `update-check --json`. `doctor` was already failing on
  drift; the problem was reach, not severity.
- #21 / PR #26: `architecture.md` said "Manual label events also stamp the
  head" with no permission caveat, describing the gap 0.8.2 closed. Both paths
  are now documented, and the architecture reference gained the Git metadata
  custody section it never had.
- #25 closed without action. The five required status checks stay; the
  approving-review requirement is unsatisfiable for a solo maintainer and is
  documented as a known bypass.
- PyPI serves 0.8.3. All four release jobs succeeded on cf73923.

### Resuming From Here

Done: issues #21, #22, #25 closed. Releases 0.8.1, 0.8.2, and 0.8.3 shipped.

Next: nothing scheduled. Two issues remain open and both are deliberately
parked.

Blockers: none. Suite green at 816 tests, ruff and mypy clean, no workflow
drift.

Open issues and why they are waiting:
- #23 explicit approver allowlist. Open design questions (config surface
  versus workflow-only, controller re-verification, team handles). The
  external reviewer who raised it may follow up, which is worth waiting for.
- #24 custody config classification is a denylist. Blocked on real-world data
  to estimate the false-positive rate of a section allowlist. Narrowing
  already applies only to shared config files after #18, so the blast radius
  is limited to worktree Workshops.

Note for future sessions: the PyPI JSON index at /pypi/<name>/json can lag a
release by several minutes. Trust the release workflow's verify-published job
and the /pypi/<name>/<version>/json endpoint over the cached index.
## Toolkit expansion: review, adoption, plugins, explain, intake, and reporting

Approved scope: roadmap items 1, 2, 4, 5, 7, and 8, implemented against
`coding-standards.md` in one continuous spec-to-plan-to-implementation pass.

- [x] Current architecture and product-boundary discovery
- [x] Full specification and architecture decision record
- [x] Review Phase: lifecycle, prompt/report, delivery, status, retry, watch
- [x] Guided onboarding and disposable local rehearsal
- [x] Versioned Harness plugin contract and provider-aware managed Spec CI
- [x] `machinist explain` and `status --watch`
- [x] Managed issue form, `task new`, and `task lint`
- [x] Local aggregate reports and optional redacted OTLP/HTTP export
- [x] User/operator/adapter documentation and changelog
- [x] Full verification suite, packaging smoke, and dogfood checks: 933 passed,
      84.88% coverage, clean workflow/format/lint/type gates, wheel + sdist smoke
- [x] Comprehension report delivered and user confirmed the safety boundaries

### Resuming From Here

Done: discovery, specification, Review Phase, guided onboarding, disposable
rehearsal, Harness plugins, provider-aware CI, explain/live status, managed
Task intake with readiness linting, reporting/telemetry, and the complete user,
operator, adapter, architecture, trust, and visual documentation pass.

Next: the branch is ready for the user's normal push, pull-request review, and
merge process. No push, pull request, or merge has been performed.

Blockers: none.

Assumptions worth revisiting:
- Review is opt-in for existing version-1 configurations and enabled by the new
  starter template.
- Review findings are advisory in the first schema version; malformed output or
  a changed PR head fails the Phase and leaves the PR draft.
- Third-party Harness entry points are trusted local code, while discovery
  collisions and import failures fail closed and remain diagnosable.
- OTLP export contains aggregate allowlisted attributes only and is disabled by
  default.

## Pydantic 2.7 compatibility and 0.10.0 release preparation

- [x] Reproduce the merged-main minimum-dependencies failure under Pydantic 2.7
- [x] Replace the PEP 695 Harness identifier alias with a Pydantic 2.7-compatible alias
- [x] Run the complete suite under minimum and current dependency sets
- [x] Align version, changelog, release copy, and visual documentation to 0.10.0
- [x] Run the canonical release gate and review the final diff
- [x] Commit the compatibility and release-preparation change

### Resuming From Here

Done: failure reproduced and fixed; all 933 tests pass under Pydantic 2.7; the
current dependency gate passes 933 tests at 84.86% coverage; workflow, format,
lint, type, wheel, and sdist checks are green; and every 0.10.0 release surface
is aligned.

Next: push this branch and open its pull request. After merge, publish the
GitHub Release tagged `v0.10.0`; Trusted Publishing will rerun the gate and
publish the prepared package to PyPI.

Blockers: none.

## First-run onboarding and 0.11.0 release preparation

- [x] Review the uncommitted onboarding change and report findings
- [x] Add the missing `task template` doctor check rather than weaken the docs
- [x] Key doctor fix hints on a canonical name and test that coverage is total
- [x] Restore Click's hidden-command filter and width in the grouped help
- [x] Add contract tests for `--yes`, the minimal config, and the grouped help
- [x] Fix the Ruff lint and format failures blocking CI
- [x] Ignore the 396 MB local `videos/` authoring tree
- [x] Install this repository's own sealed task issue form
- [x] Align version, changelog, release copy, and visual documentation to 0.11.0

### Resuming From Here

Done: PR #32 merged to main with all 15 CI checks green (Ubuntu + macOS across
Python 3.12/3.13/3.14, minimum-dependency resolution, quality, coverage,
package, CodeQL). 950 tests pass at 85.05% coverage. Every 0.11.0 release
surface is aligned and the canonical gate is green.

Next: push this branch and open its pull request. After merge, publish the
GitHub Release tagged `v0.11.0`; Trusted Publishing will rerun the gate and
publish the prepared package to PyPI.

Blockers: branch protection on `main` still requires two status contexts the CI
matrix no longer emits (`test (ubuntu-latest)`, `test (macos-latest)`), so every
merge needs `--admin` until the required-checks list is replaced with `CI gate`.
