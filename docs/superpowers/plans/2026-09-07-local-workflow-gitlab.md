# Local workflow and GitLab implementation plan

Approved direction: tasks/spec.md, September 7, 2026. Proceed continuously.

Completed September 7, 2026. All seven steps below are implemented and verified.
The canonical gate passed 1,411 tests with 87.52% coverage, twenty typed modules,
and isolated distribution smoke tests including the production local rehearsal.
See tasks/todo.md for the final evidence and external-validation boundary.

1. Record contracts and ADR on a feature branch. Repair adoption, intake, budgets,
   amendment/Review lifecycle with failing regression tests.
2. Add safe local Task storage and local Workshop capability, reusing runtime
   paths, custody, Claims, cancellation and durable Task Runs.
3. Add local Spec/Execute/Review through TaskDispatcher, with exact Approval,
   verification, candidate retention, retry/amendment, status and FF integration.
4. Add guided foreground CLI and local setup. Preserve legacy selectors. Rehearsal
   exercises production local Phases with a deterministic Harness.
5. Add optional recoverable publication with leases and GitHub/GitLab adapters.
6. Update glossary, architecture/trust guides, CLI/config docs and changelog.
7. Run canonical verification, installed CLI rehearsal and independent review.
   Commit verified logical batches. Do not push or publish a release.

Parallel ownership: adoption agent owns onboarding/doctor/wizard; lifecycle agent
owns amendment Review; intake agent owns lint/budget; local Git agent owns local
Workshop; forge agent owns adapter contracts; controller owns local store, Phases,
dispatch, guided CLI and integration. Shared-file edits are coordinated.
