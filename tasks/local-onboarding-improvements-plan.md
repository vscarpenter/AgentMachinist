# Local onboarding improvements: implementation plan

Approved scope: implement product-review recommendations 1–6 in one continuous
spec → plan → implementation pass. No release, publication, live model calls,
remote merge, or unrelated security remediation is included.

## Product contract

The primary journey is local: free guided rehearsal → inspect a plan → approve
its exact commit → inspect checked/reviewed changes → explicitly accept. The
existing GitHub automation flow remains supported and is introduced separately.

1. `machinist revise T1 --feedback "..."` refines an initial saved Spec before
   implementation. Keep the Task, prior Spec commits, and attempt history;
   persist feedback and invalidation before paid work; require fresh exact-SHA
   Approval. Failed revisions use explicit retry, and interrupted intent can
   continue. Completed candidates use existing `amend`, not initial revision.
2. `machinist inspect T1 [--json]` presents objective, saved plan, exact diff,
   checks, Review findings, attempt history, custody warnings, and next action.
   Inspection makes no model/forge calls and cannot mutate repository state.
3. `machinist config show/set --local` explicitly selects saved local settings.
   Show workflow, configuration source, resolved Harness/model/checks and when
   changes take effect. Retain plain/default and explicit `--path` compatibility.
4. `machinist rehearse --guided` is a free installed-package walkthrough using
   production local orchestration and a fake Harness. Pause at the actual plan
   and checked/reviewed result. Decline retains the disposable repository for
   inspection. Existing automatic rehearsal remains available; `--harness`
   explicitly labels real model quota use.
5. `machinist doctor --local --fresh-workshop` explicitly runs checks against a
   disposable checkout of committed HEAD. No Task/model is created; controller
   working tree, configuration, refs and runtime records remain intact. Clean
   up on success/failure. Existing `--run-gates` remains current-checkout mode.
6. Update existing introduction/first-task/operating guides and CLI help/copy:
   plain concepts first, local journey first, one actionable next step, advanced
   GitHub automation later. Preserve existing visual style, accessibility,
   domain precision, and truthful trust/cost boundaries. Add no competing guide.

## Implementation sequence

- Add failing lifecycle contracts for initial revision, stale Approval, saved
  history, failure/retry, interruption and wrong-stage refusal; implement through
  LocalWorkflow, TaskDispatcher and the existing local Spec Phase.
- Independently add inspection, local settings, guided rehearsal and fresh
  readiness with failing contract tests before implementation.
- Wire CLI commands/selectors and update local status next-action copy. Add
  routing/compatibility tests; update existing documentation drift expectations.
- Integrate focused documentation changes and record the unreleased behavior.
- Run focused tests while each feature develops; run canonical offline gate
  once the combined implementation is ready, fix failures, and rerun affected
  checks. Build and smoke-test installed wheel/source packages.
- Independently review the final diff for authority/recovery regressions. Commit
  coherent verified stages locally; report local evidence separately from any
  external CI or publication.

## Acceptance evidence

New behavior must preserve exact Spec Approval, controller-owned Git, required
Verification, completed exact Review, explicit acceptance, durable recovery,
bounded safe reads/writes and namespace separation. Tests use real disposable
Git repositories and injected Harness/forge/process transports; no paid model
or external service is necessary. A guided installed-package loop and a fresh
checkout dependency failure are meaningful end-to-end checks. Newcomer timing
targets require a later human pilot and are not claimed from automated tests.

## Progress

- All six improvements implemented, with local revision/recovery and onboarding
  implementation committed separately. The CLI, existing guides, and changelog
  describe the additions as unreleased.
- Independent review found and verified corrections for real Review finding
  rendering, fresh-mode failure guidance, failed Execute checks, interrupted
  Spec delivery, full-plan preview guidance, and revision history.
- Canonical checks passed workflow drift, format, lint, and mypy for 24 source
  files. The completed full test run recorded **1,856 passed**, **88.86% coverage**,
  and one stale packaging-test allowlist assertion. That test-only assertion was
  updated for the two new typed modules; **all 7 packaging tests then passed**.
  No runtime implementation changed after the full test run. The earlier stale
  help-copy assertion was corrected before this completed run.
- The offline package gate passed for wheel and source distribution in clean
  Python 3.13.15 environments: dependency checks, automatic/guided rehearsal,
  exact plan/result decisions, local integration, fresh readiness, and saved
  local settings inspection. Final packages are rebuilt after the test metadata
  correction. No real model, forge, or release publication was used.
- Automated implementation verification is complete. First-time-user timing and
  comprehension remain a later human pilot; no usability timing claim is made.
