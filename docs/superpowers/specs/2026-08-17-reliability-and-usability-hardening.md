# AgentMachinist reliability and usability hardening

**Date:** 2026-08-17
**Status:** Approved

## Summary

Deepen AgentMachinist's Task lifecycle so Approval names an immutable Spec, local dispatch is exclusive and recoverable, generated workflows follow configuration, and successful or failed Task Runs leave durable Evidence. Bring release automation and all user documentation into agreement with the implemented interface.

## Requirements

1. Approval MUST record the exact Task branch head SHA and Execute MUST refuse missing or stale Approval.
2. Manual labels, `/machinist-execute`, and a local `machinist approve <issue>` path MUST all be able to create SHA-bound Approval.
3. Task Runs MUST use an atomic local Claim, persist running/succeeded/failed state under `.machinist/runs/`, survive watcher restarts, and require an explicit retry after failure.
4. Local and GitHub Actions Spec dispatch MUST be mutually selected by configuration; the local watcher MUST not dispatch Spec when CI owns it.
5. Generated workflows MUST project configured labels and the installed AgentMachinist version, and drift MUST be detectable and repairable.
6. `machinist doctor` MUST diagnose config, GitHub CLI, harness availability, workflow drift, dispatch ownership, and test-gate configuration without mutating the repository.
7. Spec Phase MUST reject repository mutations. Execute MUST verify that the harness did not change Git HEAD or `.machinist/` before AgentMachinist commits.
8. Expected GitHub JSON failures and quality-gate timeouts MUST render as typed, one-line CLI errors.
9. CI and release MUST test macOS and Linux, verify tag/version equality, build one package version, install-smoke the wheel, and test before publishing.
10. README, Getting Started, onboarding, architecture, operations, trust, harness support, contributing, and changelog documentation MUST describe current behavior without stale milestone claims or broken PyPI-relative links.

## Proposed approach

- Add a `lifecycle` module whose interface owns local Claim acquisition and atomic Task Run records.
- Extend the GitHub adapter with PR head SHA and Approval-marker operations.
- Add an `artifacts` module that renders, writes, and checks managed workflows from validated configuration.
- Add `approve`, `doctor`, `sync-workflows`, and `retry` CLI commands.
- Add custody checks to the Workshop module and phase orchestration.
- Keep the four Harness adapters, but expose capability facts and narrow Spec tools where supported; document advisory gaps honestly.
- Strengthen CI/release workflows and bump the unreleased package to `0.1.1`.

## Testing plan

- Write unit tests for stale/missing Approval, latest-marker parsing, lifecycle Claim contention, atomic persisted failures, retry, configured dispatch ownership, generated workflow drift, doctor output, Spec mutation rejection, custody violations, and timeout translation.
- Extend documentation drift tests for stale phrases, absolute public links, support documents, HTML semantics, and version/release invariants.
- Run the full pytest suite, build sdist/wheel, inspect package data, and invoke the built wheel in isolation.

## Out of scope

- Cross-host distributed Claims beyond the configured local-versus-CI Spec owner.
- Artifact deployment after a PR is ready for review.
- A provider-independent operating-system sandbox for Execute.
- Publishing `0.1.1` to PyPI or pushing the branch.

## Open questions

None. The approved review establishes the trust and usability direction; conservative defaults apply.

