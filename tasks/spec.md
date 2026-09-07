# Guided local workflow and GitLab specification

Status: approved direction in the September 7, 2026 user request. Spec, plan,
implementation and verification proceed continuously under the standing correction.
The prior completed specification is archived in docs/superpowers/specs/.

Implementation completed September 7, 2026; the canonical gate passed 1,411
tests with 87.52% coverage and both distribution smoke tests. See tasks/todo.md
for the final implementation and validation record.

## Goal

Repair adoption and amendment defects, provide a guided foreground Task journey,
complete the local Git workflow, make publication optional, and support GitLab
issue intake and merge-request publication. Preserve legacy GitHub commands and
version-1 configuration/history.

## Inputs and outputs

- Start from intent, a Markdown body, or an explicitly selected GitHub/GitLab issue.
- Local IDs T1, T2, etc. use a separate runtime namespace from legacy issue numbers.
- Produce Spec commit, exact human Approval, isolated Execute, Verification,
  independent Review, durable candidate ref, local report/diff, and explicit human
  integration. Remote publication is optional after the candidate is complete.
- GitLab support covers gitlab.com and explicit self-managed hosts/nested projects
  through glab authentication. It imports Tasks and publishes/updates exact MRs.
- Guided local setup detects a Harness and asks for a verification command; it
  requires no origin, forge authentication, label or hosted workflow. Preserve
  existing repository configuration and enable Review for new local setup.

## Constraints

- Every claimed Spec/Execute/Review Task Run is constructed through dispatch.py,
  using the existing lifecycle, Evidence, cancellation and Verification modules.
- Controller owns Git, records, Approval and remote changes. Reject Harness edits
  to HEAD, protected metadata or .machinist. Keep exact-Spec Approval and custody.
- Local Approval binds repository, Task, Spec SHA, actor and time. It is workflow
  evidence, not protection against hostile processes running as the same OS user.
  Legacy GitHub Approval retains the trusted workflow issuer.
- Failed runs need explicit retry. Resume validates retained Workshop custody.
  Keep a durable candidate before cleanup. Reconcile completed work without
  repeating paid implementation or successful verification.
- Integration requires explicit human action, a clean base checkout, matching
  base/candidate identities and fast-forward ancestry. Record intent and observed
  result for crash recovery. No automatic or remote merge or production deployment.
- Publication binds host/repository/branch/base/state/head, uses an exact expected
  SHA lease, records intent, and reconciles an existing branch/change request.
- Existing numeric issue CLI forms stay compatible. Local Task selection is explicit.
- No new database, broker, service, broad plugin framework or additional Harness.
- Local orchestration is distinct from offline inference; do not claim a tested
  local-model profile without a full network-denied run with cached prerequisites.

## Adoption and amendment corrections

1. Setup PR validates local setup before publishing. Full doctor checks deployed
   workflows after setup is merged. Receipts order these steps correctly.
2. Onboard resumes recognized config/setup branch without overwriting preferences.
   Harness discovery includes installed built-ins/plugins.
3. New successful Execute SHA permits a new Review under the existing Claim.
   Same-head duplicates remain blocked; old Evidence cannot contaminate new Review.
4. Rehearsal runs production local Phases with real Git, verification and fake
   Harness. A real paid Harness run stays explicit.
5. Review reports completion and findings, never an unimplemented passing policy.
6. Intake accepts native heading shapes and file/stdin bodies, preserves drafts on
   error, and rejects empty acceptance criteria.
7. Daily Phase-attempt budget is max_runs_per_day, with readable legacy alias
   max_tasks_per_day and conflicting values rejected.

## Design

Local runtime lives below .machinist/runs/local using safe atomic files. Local
Task identity is controller-owned and external issues are optional provenance.
One guided path saves the Task, produces Spec, stops for Approval, then continues
all enabled machine Phases in the foreground. Status is local and shows one next
valid action. An amendment produces a new Spec and invalidates Approval while
retaining prior candidate/history. Existing GitHub issue workflows remain intact.

Repository operations support exact local commits without manufacturing a remote.
Task/Approval, Git, execution and publication have separate responsibilities.
Publication adapters normalize GitHub PRs and GitLab MRs; they do not fake trusted
Approval comments. Publishing failure does not invalidate the reviewed local result.

## Acceptance criteria and edge cases

1. Fresh setup PR reaches publication without pre-existing deployed workflows;
   doctor still detects undeployed integration afterward. Setup resumes safely.
2. Amendment receives a second Review; concurrent/same-head repeats add no attempt.
3. A no-origin repo completes Task, Spec, Approval, Execute, checks, Review, report
   and explicit FF integration without gh/glab calls.
4. Stale Approval, changed base/candidate, dirty checkout, unsafe paths, Harness
   commits/metadata edits and test deletion fail before delivery.
5. Explicit retry/resume preserves failure Evidence and validates retained state;
   completed post-commit work recovers without rerunning the Harness or gates.
6. Local IDs cannot collide with external issue IDs; copied/corrupt records do not
   acquire authority in a different repository.
7. Independent publication retries use leases and verify exact resulting PR/MR;
   nested GitLab paths and self-managed hosts have recorded/stubbed contracts.
8. Rehearsal exercises production modules and fails when a real check fails.
9. Existing public contracts, old history, managed workflows and tests remain
   compatible. Documentation accurately distinguishes local and hosted proof.

## Test stubs and validation

Begin with failing setup/amendment/intake/budget regressions. Use real temporary Git
for local Workshop/custody/Approval/verification/amendment/retry/integration/lease
contracts. Inject fake Harnesses and forge runners; tests do not spend model usage
or create real remote artifacts. Run targeted gates after each slice, canonical
scripts/verify.sh and packaging smoke, installed CLI rehearsal, and independent
adversarial review. Report live GitLab validation separately from contract tests.

## Out of scope

Automatic/remote merges, production deployment, extra Harnesses/forges, shared
multi-host execution/quotas, a GUI rebuild, offline-model quality guarantees,
GitLab-hosted Spec CI, release publication, and unrequested remote pushes.
