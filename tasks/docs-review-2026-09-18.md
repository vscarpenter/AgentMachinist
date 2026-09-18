# Documentation review: September 18, 2026

Reviewed all 28 files under `docs/`, root documentation, task records, approved
dogfood Specs, packaged templates, and configuration/workflow comments against
the implementation on `codex/verification-reporting-repair`. The implementation
and its verification record were committed through `9e4365a` before this review.

The published baseline remains 0.17.1. Bounded Execute repair, combined
local/legacy reporting, and the expanded dogfood Gates are unreleased checkout
changes. This review updates documentation and comments; it changes no controller
logic, workflow steps, configuration values, or package version.

## Corrections

- Remove legacy-only reporting claims and distinguish terminal Phase success,
  first-pass Execute success, repair outcomes, usage completeness, and stored
  local delivery snapshots. Explain update-time windows, null rates, final Gate
  failure counts, and explicit endpoint consent for local/all exports.
- Explain repair as one optional extra Harness invocation within active Execute.
  Document eligible exit statuses, abnormal advisory outcomes, independent
  Harness/Gate timeouts, the shared repair deadline, explicit fresh recovery,
  and original instruction/feedback provenance on resume.
- Distinguish enforced budget, custody, and Verification controls from advisory
  instructions about scope and test strength. Retain raw-log limitations.
- Identify new features as unreleased throughout operating and visual guides.
  Keep the published beginner workflow valid and explain that saved local
  settings do not automatically adopt root configuration changes.
- Update contributor/module guidance for the shared repair coordinator,
  namespace-aware reporting, actual Python quality tools, and full verification
  script. Remove the obsolete five-second test-suite estimate.
- Correct the managed Spec workflow's mode-switch instructions in its template
  and regenerate the managed file. Update config comments to describe controller
  repository binding and per-invocation Harness timeouts accurately.
- Preserve historical decisions and approved Specs. Add current-applicability
  notes where old guidance could be mistaken for current behavior.

## Complete inventory

| Document or group | Result |
| --- | --- |
| `docs/README.md` | Updated release framing, reporting scope, and source-checkout overview. |
| `docs/tldr.md` | Corrected reporting scope and added concise repair guidance; retained 99-line length. |
| `docs/getting-started.md` | Corrected repair eligibility, timeout/recovery rules, dogfood scope, metrics, and reporting examples. |
| `docs/local-workflow.md` | Clarified saved settings, instruction provenance, recovery, and reporting namespaces. |
| `docs/operator-runbook.md` | Updated operating/recovery details, timeout and metric semantics, and export migration. |
| `docs/harnesses.md` | Made repair/profile boundaries explicit and clarified valid structured usage. |
| `docs/architecture.md` | Corrected release attribution and resumed repair provenance. |
| `docs/trust-model.md` | Clarified provenance recovery and distinct endpoint/header validation. |
| `docs/approval-policy.md` | Separated enforced controls from advisory repair restrictions; marked availability. |
| `docs/index.html` | Corrected immediate-retry wording and added source-only repair/reporting guidance. |
| `docs/first-run-guide.html` | Added repair recovery disclosure and aggregate-report example; clarified failed Task Run recovery. |
| `docs/job-card.html` | Added compact source-only reporting and repair guidance. |
| `docs/how-it-works.html` | Clarified local retry defaults and repair within Execute. |
| `docs/explainer.html` | Reviewed; released/default workflow remains accurate. |
| `docs/onboarding.html` | Reviewed; archived redirect retained. |
| `docs/adr/0001-review-plugin-telemetry-boundaries.md` | Updated current applicability for local reporting and export consent; historical decision retained. |
| `docs/adr/0002-deep-module-ownership.md` | Added current shared repair ownership; original decision retained. |
| `docs/adr/0003-local-workflow-and-optional-publication.md` | Reviewed; current boundaries retained. |
| `docs/adr/0004-local-readiness-and-diagnostic-boundaries.md` | Added repair sanitizer applicability without extending raw-log promises. |
| `docs/superpowers/plans/2026-08-17-build-system-hardening.md` | Noted that current dogfood Gates supersede its original choice to keep lint outside Execute. |
| `docs/superpowers/plans/2026-09-03-resume-push-and-approve-flags.md` | Reviewed; historical banner and current-guide links remain sufficient. |
| `docs/superpowers/plans/2026-09-03-spec-to-execute-simplification.md` | Reviewed; historical banner and current-guide links remain sufficient. |
| `docs/superpowers/plans/2026-09-07-local-workflow-gitlab.md` | Reviewed; historical banner and current-guide links remain sufficient. |
| `docs/superpowers/specs/2026-08-16-agentmachinist-design.md` | Reviewed; historical proposal retained. |
| `docs/superpowers/specs/2026-08-17-reliability-and-usability-hardening.md` | Reviewed; historical proposal retained. |
| `docs/superpowers/specs/2026-09-03-resume-push-and-approve-flags.md` | Reviewed; historical proposal retained. |
| `docs/CNAME`, `docs/.nojekyll` | Reviewed; site domain and empty Pages marker retained. |
| `README.md` | Clarified repository-specific Gates, unreleased report option, and configured Verification wording. |
| `CLAUDE.md` | Added reporting/telemetry/local Phase ownership and current unreleased checkout summary. |
| `AGENTS.md` | Corrected verification command description and removed obsolete timing estimate. |
| `CONTRIBUTING.md` | Documented actual gates, repair ownership, report identity/usage, and telemetry consent. |
| `CONTEXT.md` | Defined repair as work inside an Execute Task Run, not a new Phase or failed-run retry. |
| `SECURITY.md` | Updated local/GitLab scope, Approval, raw-log limitations, telemetry, and repair boundaries. |
| `coding-standards.md` | Added repository applicability note distinguishing the shared reference from installed Python tooling. |
| `CHANGELOG.md` | Recorded this documentation follow-up under Unreleased. |
| `AgentMachinist-Prompt.md` | Added a historical/current-guide pointer; original kickoff text retained. |
| `tasks/spec.md`, `tasks/local-readiness-plan.md`, `tasks/cli-guidance-plan.md` | Reviewed completed historical plans; original contracts retained. |
| `tasks/lessons.md`, `tasks/docs-review-2026-09-12.md` | Reviewed; lessons and dated prior review retained. |
| `tasks/verification-reporting-repair-spec.md`, `tasks/verification-reporting-repair-plan.md` | Reviewed; approved requirements and implementation sequence remain accurate. |
| `tasks/todo.md` | Added a historical-ledger note and current review/verification record. |
| `.machinist/specs/issue-1-spec.md`, `.machinist/specs/issue-4-spec.md` | Reviewed as historical approved dogfood Evidence; kept byte-for-byte unchanged. |
| `src/machinist/templates/implement-prompt.md`, `src/machinist/templates/spec-prompt.md` | Reviewed; repair-specific context is appended by the coordinator, so no template change is needed. |
| `src/machinist/templates/machinist.yaml`, `machinist.yaml` | Clarified comments; configuration values unchanged. |
| `src/machinist/templates/github/machinist-spec.yml`, `.github/workflows/machinist-spec.yml` | Corrected mode-switch comments in the template and regenerated its managed projection. |
| `src/machinist/templates/github/machinist-approve.yml`, `src/machinist/templates/github/agentmachinist-task.yml` | Reviewed; no change needed. |
| `.github/ISSUE_TEMPLATE/agentmachinist-task.yml`, `.github/dependabot.yml`, `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `.github/workflows/machinist-approve.yml` | Reviewed; no change needed. |

## Validation

- 189 documentation, configuration, workflow, and packaging tests passed,
  including all 36 documentation drift tests. The first packaging attempt was
  blocked by sandbox access to the existing uv cache; the complete rerun passed
  with that access restored.
- Validated all 13 YAML configuration examples in current Markdown guides
  against the current strict configuration model.
- Managed workflow projection matches. Parsed YAML before/after the generated
  Spec workflow is identical; only comments and its managed digest changed.
- Rendered all four edited HTML pages locally. The new recovery disclosure opens,
  source-checkout notices and commands fit their containers, and browser console
  inspection reported no errors. Existing styles, scripts, and SVGs are unchanged.
- `git diff --check` passed. Approved dogfood Specs and runtime configuration
  values remain unchanged.
- The preceding implementation's canonical run passed 1,663 tests at 88.41%
  coverage, builds, and clean installation checks. This documentation follow-up
  ran focused checks; it did not repeat that full suite or publish the website.

Resuming from here: review complete. Documentation is ready for the branch's
eventual publication. No product-code work, version bump, push, or release is
included in this follow-up.
