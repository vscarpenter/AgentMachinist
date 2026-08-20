# Changelog

## Unreleased

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
