# Changelog

## Unreleased

- Add `github.spec_install` (`pypi` or `checkout`) so Spec CI can run the controller from the checkout.
- Require checked-in managed workflows to match config plus package version.
- Smoke-test the versioned wheel via `scripts/smoke-wheel.sh`; exclude dogfood trees from the sdist.
- CI tests Python 3.12 and 3.13, pins uv, and shows pytest output.

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
