# Local workflow and GitLab implementation plan

> [!IMPORTANT]
> **Historical design record.** Preserved implementation context, not current
> operating instructions. See [Getting Started](../../getting-started.md),
> [Architecture and lifecycle](../../architecture.md), and
> [Local workflow](../../local-workflow.md) for current behavior.
> This completed plan records source implementation and local verification.
> It does not establish a published release, deployed documentation, or live GitLab validation.

## Current status (2026-09-12)

The local Task workflow and GitLab issue intake/MR publication are implemented
and recorded under 0.14.0 in the [changelog](../../../CHANGELOG.md). `start`
stops at the saved Spec; local Approval continues Execute and mandatory
independent Review. Integration and publication remain separate explicit
commands. Version 0.15.0 adds optional local readiness through `doctor --local`;
see [ADR 0004](../../adr/0004-local-readiness-and-diagnostic-boundaries.md).
The verification totals below are the September 7 implementation snapshot.
GitLab hosted Spec CI and remote Approval are outside the implemented scope.

Approved direction: [Task specification at f1c9190](https://github.com/vscarpenter/AgentMachinist/blob/f1c91900e12158bb7d9ca5ff126fcdb53e8277f1/tasks/spec.md),
September 7, 2026. The working `tasks/spec.md` now describes later readiness work.

Completed September 7, 2026. All seven steps below are implemented and verified.
The canonical gate passed 1,411 tests with 87.52% coverage, twenty typed modules,
and isolated distribution smoke tests including the production local rehearsal.
See the [implementation ledger](../../../tasks/todo.md) for the final evidence
and external-validation boundary.

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
