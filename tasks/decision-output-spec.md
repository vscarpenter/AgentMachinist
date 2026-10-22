# Local decision output and Spec revision differences

Status: approved by the October 10, 2026 request to implement the two items
marked "Add first" in the critical review of `tasks/next.md`. Continue through
specification, plan, implementation, and verification without another gate.

## Goal

Make an existing local decision easier to assess without another required
command, configuration setting, model call, or durable state mechanism.

## Requirements

1. `inspect T1` starts with a bounded decision summary: current state, exact
   candidate identity when available, accumulated Task changed paths, ordered
   Gate outcomes, and high-severity independent Review findings.
2. Only a verified, reviewed candidate with consistent exact Evidence receives
   acceptance and amendment commands. Other states retain existing recovery
   or Approval guidance. Repeat the relevant actions after the detailed output.
3. Accumulated changed paths compare the original Task base with the candidate;
   the existing detailed diff stays Spec-to-candidate and is labeled as latest
   Execute. Count Gates and findings before preview truncation, show omitted
   counts, and never equate unavailable Evidence with a successful check.
4. When an initial Spec revision is displayed, show the bounded textual
   difference between its saved prior and current exact Spec commits, followed
   by the saved replacement and its fresh Approval command. Use the existing
   `spec_base_sha` pointer so completed revision recovery shows the same diff.
5. Identical Spec text says it is unchanged while making clear that the new
   commit still requires fresh Approval. Missing or inconsistent comparison
   Evidence is reported as unavailable; never substitute a working-tree file.
6. Large differences are bounded, sanitized by the existing diagnostic policy,
   and accompanied by a safely quoted exact Git comparison command. Inspection
   and comparison remain read-only, with no Harness or forge call.

## Constraints and scope

- Preserve existing CLI options, JSON fields, Approval/custody checks, Phase
  dispatch, persisted schemas, advisory findings, and explicit integration.
- Additive read-model fields are allowed; no new dependency or runtime cache.
- Do not implement failing-check output changes, Spec completeness warnings,
  cross-Task warnings, notifications, automatic instructions, activity trails,
  unexpected-path parsing, or export bundles in this pass.
- Keep the existing untracked proposal untouched and outside implementation
  commits. No push, PR publication, release, or deployment is included.

## Verification

Start with failing contracts for summary order, safe actions, amendment scope,
overflow counts/high findings, changed/unchanged revision text, recovered
revision display, bounded diagnostics, and missing comparison data. Run focused
tests and an independent review, then `bash scripts/verify.sh` for workflow
drift, formatting, lint, types, full coverage suite, package builds, and installed
package smoke checks. Use fake Harnesses only. Commit coherent changes locally.
