# Documentation review — September 12, 2026

Reviewed all 26 files under `docs/`, including hidden site configuration,
against the current source on `codex/cli-next-step-guidance`. Package metadata
remains 0.15.0; the expanded CLI completion guidance is recorded as Unreleased.
This review checks source behavior, not public-site deployment or a new release.

## Inventory and results

| Document | Result |
| --- | --- |
| `docs/README.md` | Updated release framing and source-only distinction. |
| `docs/tldr.md` | Corrected status inspection and asynchronous GitHub Approval readiness. |
| `docs/getting-started.md` | Clarified rehearsal configuration and fixture Gates, direct Spec dispatch, and immediate Review retry. |
| `docs/local-workflow.md` | Added exact candidate-diff inspection and clarified local versus hosted GitHub dispatch. |
| `docs/architecture.md` | Corrected local resume conditions, workflow ownership, completion guidance, and service Claim scope. |
| `docs/operator-runbook.md` | Corrected setup/authentication checks, recovery, workflow version pins, and completion receipts. |
| `docs/trust-model.md` | Clarified Spec/Review controls, release framing, and the distinction between guidance and Approval. |
| `docs/harnesses.md` | Documented Phase profile inheritance, adapter changes, explicit clearing, and timeout resolution. |
| `docs/index.html` | Corrected Spec-writing ownership and labeled source-only completion guidance. |
| `docs/first-run-guide.html` | Corrected installation, five terminal prompts, optional legacy Review, status inspection, and completion guidance. |
| `docs/job-card.html` | Corrected terminal prompts, status inspection, and completion guidance. |
| `docs/explainer.html` | Corrected Harness selection language and amendment eligibility before integration. |
| `docs/onboarding.html` | Reviewed; current redirect and fallback retained. |
| `docs/adr/0001-review-plugin-telemetry-boundaries.md` | Reviewed; current Review, plugin, and telemetry boundaries retained. |
| `docs/adr/0002-deep-module-ownership.md` | Reviewed; current module ownership retained. |
| `docs/adr/0003-local-workflow-and-optional-publication.md` | Reviewed; current local workflow and publication decisions retained. |
| `docs/adr/0004-local-readiness-and-diagnostic-boundaries.md` | Reviewed; current readiness and diagnostic decisions retained. |
| `docs/superpowers/specs/2026-08-16-agentmachinist-design.md` | Added current-status note covering implemented milestones and superseded Approval/custody assumptions. |
| `docs/superpowers/specs/2026-08-17-reliability-and-usability-hardening.md` | Added current-status note covering Approval selectors and local readiness. |
| `docs/superpowers/specs/2026-09-03-resume-push-and-approve-flags.md` | Added current-status note for implemented recovery and Approval contracts. |
| `docs/superpowers/plans/2026-08-17-build-system-hardening.md` | Added current packaging, CI, and release applicability note. |
| `docs/superpowers/plans/2026-09-03-spec-to-execute-simplification.md` | Distinguished implemented work from the deferred `watch --dry-run` fold. |
| `docs/superpowers/plans/2026-09-03-resume-push-and-approve-flags.md` | Marked implementation cards complete; preserved historical checklists. |
| `docs/superpowers/plans/2026-09-07-local-workflow-gitlab.md` | Added current-status note and replaced the mutable Spec link with its verified original commit. |
| `docs/CNAME` | Reviewed; retained domain consistent with site canonical links. |
| `docs/.nojekyll` | Reviewed; retained empty GitHub Pages marker. |

Historical plans/specifications retain their original proposals and verification
snapshots. Their new dated notes explain current applicability. All four ADRs
already match the implementation.

## Implementation evidence

- `local_cli.py`, `local_workflow.py`, and `phases/local.py`: status fields,
  Spec-to-candidate Review diff, exact Approval, local retry, and integration.
- `cli.py`, `transitions.py`, and `phases/execute.py`: legacy issue dispatch,
  asynchronous Approval, retry execution, completion receipts, and push recovery.
- `local_setup.py`, `local_doctor.py`, and `rehearsal.py`: setup versus readiness,
  configuration precedence, and rehearsal's disposable fixture boundaries.
- `config.py` and `harness/`: Phase inheritance, timeouts, adapter arguments,
  authentication probes, and read-only Phase controls.
- `publication.py`, `workflows.py`, and current managed workflows: publication
  eligibility, ownership checks, workflow projection, and trusted Approval.
- `pyproject.toml`, `scripts/verify.sh`, and CI/release workflows: package version,
  distribution contents, Python matrix, quality gates, and release smoke checks.

The root README received the same managed-workflow version-pin correction as
the operator runbook. The documentation test now accepts labeled source-only
examples when the changelog contains Unreleased work before a version bump,
and checks those labels across all HTML guides. Production code was unchanged.

## Validation

- All 32 `tests/test_docs.py` checks passed, including documentation links,
  command examples, configuration, HTML controls, and release consistency.
- Additional read-only audit found no unknown command/option in current
  operating guides and validated their Markdown YAML examples against the
  current configuration model.
- Ruff lint and formatting checks passed for the changed documentation test;
  `git diff --check` passed.
- Local browser checks covered all five HTML pages: simulator navigation to
  optional publication; local/legacy route switching; expanded GitHub
  instructions; job-card content; explainer scene selection and play/pause; and
  the archived onboarding redirect. The revised SVG label and longer caption
  fit, and no browser console errors were reported.
- The preceding implementation commit passed the full canonical gate (1,528
  tests, 88.15% coverage). This documentation-only review reran focused checks;
  it did not repeat the full implementation suite or publish the website.
