# Changelog

## Unreleased

- Close approval and lifecycle correctness gaps: both managed approval paths
  require write access, delivered PR cleanup can only add warning Evidence,
  cancellation markers are generation-safe, stale running projections are
  reported as interrupted, and Git operations honor cooperative cancellation.
- Make first-run readiness explicit. Non-interactive init no longer silently
  enables a guessed test command; setup prints dispatch, verification, label,
  workflow, commit/push, and next-step status. `sync-labels --check/--apply`
  and the expanded `doctor --run-gates` preflight expose missing setup before a
  Task is labeled.
- Add durable named progress stages, verification gate N/M status, attempt
  history, elapsed time, exact recovery commands, watcher heartbeats, and
  active-Claim safeguards for service lifecycle commands.
- Harden Harness and distribution adoption: current Codex headless flags,
  fail-closed OpenCode/Pi/Claude auth probes, ephemeral Claude/Pi sessions,
  bounded adapter diagnostics, explicit Python/platform metadata, and installed
  wheel/sdist first-run smoke coverage.

## 0.8.3 — 2026-08-26

- Warn about managed-workflow drift on the paths operators already walk.
  A workflow fix ships in a projected file rather than in library code, so
  upgrading the package alone left the old workflow in place, which is how the
  0.8.2 approval fix could be installed without being in effect. `machinist
  watch` now reports drift at startup and `machinist update-check` reports it
  alongside the release comparison, both pointing at `machinist
  sync-workflows`. The advisory never blocks a command and never appears in
  `update-check --json`. `machinist doctor` continues to fail on drift.

## 0.8.2 — 2026-08-26

- Gate the label-based approval path on the actor's repository permission.
  The managed approve workflow bound the PR head and minted trusted approval
  evidence whenever the approval label appeared, without checking who applied
  it. GitHub grants label permission at triage level, and triage cannot push
  code, so label authority could become implementation-approval authority.
  Applying the approval label now requires write or admin access, and the
  check fails closed when the permission cannot be read. Reported by an
  external reviewer.
- Record the approver on the approval comment. Both paths now post
  `Approved by @<login> for <sha>.` alongside the machine-readable marker, so
  the PR itself shows who authorized the run. The marker format is unchanged
  and older approval comments still parse.
- Existing repositories must run `machinist sync-workflows` to adopt the
  actor check. `machinist doctor` reports the drift until they do.

## 0.8.1 — 2026-08-26

- Fix a false custody failure under `workspace.strategy: worktree`
  (issue #16). A worktree shares `config`, `hooks/`, and `info/` with its
  parent, so the Git metadata custody guard was byte-comparing the
  developer's own `.git/config`. Editing your Git config, adding a remote, or
  installing a hook while a Task ran aborted the phase with
  `controller-owned Git metadata changed during an untrusted phase`.
  Watched config files are now compared by the keys that can execute a
  program, name a path Git will trust, or redirect the network, so benign
  edits pass and planted `core.fsmonitor`, `core.hooksPath`, filter, alias,
  credential, `url.*`, `include.path`, and `remote.origin.url` values still
  fail closed. Hooks, `info/`, and `objects/` stay byte-compared. The
  rejection now names the exact keys that moved and, under `worktree`, points
  at `workspace.strategy: clone`.
- Task Run records now store hashes of sensitive Git config values rather than
  a digest of the whole file, so a credentialed origin or an
  `http.*.extraheader` cannot reach a run record or an error message.
- The Git-custody checkpoint version is now 2. A Workshop retained across the
  upgrade fails its custody check and needs `machinist retry` to re-provision.

## 0.8.0 — 2026-08-23

- Refuse to commit an implementation that deleted a test file (heuristic path
  patterns; a rename counts as a deletion), closing the "green the gate by
  deleting the failing test" loophole deterministically. Change summaries now
  record deleted files. Opt out per repository with
  `limits.allow_test_deletions: true` when an approved Spec legitimately
  removes tests.
- Give the implementation harness a verification feedback loop: the implement
  prompt now lists the configured gate commands and asks the harness to run
  required gates and iterate until they pass before finishing (fixing the
  code, never weakening a test), and the `claude-code` adapter allowlists
  exactly those commands, whose execution its headless edit mode otherwise
  denies. The controller's own gate run afterwards remains the authoritative
  check. Opt out with `verification.harness_may_run_gates: false`.
- Ask the Spec harness for a `## Risks` section (between the approach and the
  testing plan) and require the spec to be implementable by a developer who
  never saw the originating conversation, so the human approval gate reviews
  risk explicitly.

## 0.7.0 — 2026-08-20

- Add `machinist update-check`: compare the installed release against PyPI and
  print the upgrade command matching how this copy was installed (`uv tool`,
  `pipx`, `pip`, or an editable source checkout). `--json` emits a stable
  scriptable result; the command exits non-zero only when the index could not
  be read. `machinist doctor` reports the same comparison as an advisory
  `updates` row that warns but never fails. Set `MACHINIST_NO_UPDATE_CHECK=1`
  to suppress both probes.
- Add a job-card TL;DR page to the docs comparing local dispatch with
  GitHub Actions dispatch.

## 0.6.0 — 2026-08-20

- Retry the release workflow's published-package smoke test while PyPI's
  simple index propagates; the JSON API can show a release seconds before
  the resolver does, which failed the v0.5.0 run's first attempt.
- Refresh the first-run field guide to document the interactive
  `machinist init` questions and the flags that pre-answer them.

## 0.5.0 — 2026-08-20

- Ask first-run setup questions in `machinist init` when run in a terminal —
  dispatch mode, managed workflows, harness, test gate, and notifications —
  each with a one-line explanation and a safe default. New flags
  `--spec-source`, `--notifications`, and `--no-input`; existing flags
  pre-answer their questions. Configured test commands no longer keep the
  template's example comment on the same line.

## 0.4.0 — 2026-08-19

- Serve the first-run guide rendered via GitHub Pages and link it from the README; refresh the guide's content, navigation, and dark-mode contrast.
- Make retry and cleanup safe: preserve dirty Workshops unless forced, add explicit fresh/resume recovery, prevent rejected-commit reuse, bind execution to the approved SHA, and persist append-only attempt evidence.
- Add first-class Spec revision/abandonment, implementation amendments, cooperative cancellation, queue controls and budgets, named verification gates, configurable notifications, phase-specific harnesses, and repository instruction overlays.
- Add offline/JSON run inspection, corrupt/orphan inventory, multi-repository status, effective-config/schema commands, dry-run planning, and a managed macOS launchd service.
- Harden initialization, diagnostics, GitHub pagination, process credential isolation, release privilege separation, immutable Action/tool pins, minimum-dependency and Python 3.14 coverage, Ruff/mypy/coverage gates, reproducible artifacts, and post-publish verification.

## 0.3.0 — 2026-08-18

- Add `github.spec_install` (`pypi` or `checkout`) so Spec CI can run the controller from the checkout.
- Require checked-in managed workflows to match config plus package version.
- Smoke-test the versioned wheel via `scripts/smoke-wheel.sh`; exclude dogfood trees from the sdist.
- CI tests Python 3.12 and 3.13, pins uv, and shows pytest output.
- Bump `actions/checkout` to v7 and `astral-sh/setup-uv` to v7 in CI, release, and the packaged Spec workflow template.

## 0.2.0 — 2026-08-17

- Bind approvals to the exact draft-PR head SHA and surface pending/stale states.
- Add atomic Task Run records, explicit retry, checkpoints, and partial-push recovery.
- Add config-derived workflow projection, drift checks, and single spec-dispatcher ownership.
- Add leased pushes and harness Git/remote/pipeline postconditions.
- Reduce controller credentials in harness subprocesses and publish honest capability metadata.
- Add `approve`, `doctor`, `retry`, and `sync-workflows` commands.
- Expand Linux/macOS CI and gate releases on tests, tag identity, and installed-wheel smoke tests.
- Add architecture, operations, trust, harness, contribution, and release documentation.

## 0.1.0 — 2026-08-16

- Initial local-first spec, approval, execution, status, and watcher pipeline.
