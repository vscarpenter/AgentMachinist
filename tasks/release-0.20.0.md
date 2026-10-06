# Release 0.20.0

## Scope and authorization

Vinny requested creation and push of 0.20.0 after the onboarding and landing
page PRs merged. Prepare a minor release containing all changes since 0.19.0,
preserve the full verification gates, and push a reviewable release PR.
`CLAUDE.md` keeps GitHub Release creation and PyPI publication separate from a
version bump and push. Do not bypass protected-main review or required checks.
Website deployment is outside this release-preparation request.

Vinny subsequently approved including the documentation design-hook fixes in
0.20.0. This adds a scoped readability and layout pass over the documentation
index and first-run guide, preserving their brand, content, controls, and links.

## Plan

1. Start from the merged main commit and confirm the published version.
2. Increase bounded CI/release test budgets based on observed 30-minute
   timeouts; retain every platform, Python version, check, and publish gate.
   Restore the historical required check names with strict full-matrix
   aggregate checks rather than changing branch protection.
3. Bump package metadata and lockfile to 0.20.0, date the complete Unreleased
   changelog, and align current guides with package-based onboarding.
4. Regenerate/check managed workflows, run focused release contracts, then run
   the canonical release gate on the final committed candidate.
5. Push the release branch, create/attach its PR, and verify remote CI.
6. After required review/merge and explicit publication authorization, create
   `v0.20.0` at the verified merge commit. Verify all release jobs, PyPI wheel
   and sdist, checksums, and an isolated installation of the new commands.

### Approved documentation design follow-up

- Retain the established light/dark colors and orange human/blue machine
  ownership cues. Preserve the complete onboarding journey, commands,
  illustrations, anchors, theme switch, copying, simulation, and checklist.
- Improve contrast for active buttons, increase undersized functional labels,
  relax tight headline tracking, and remove duplicate hero labels.
- Replace heavy colored card borders and actual nested cards with neutral
  dividers, spacing, and clearer type. Keep code panels identifiable.
- Persist only evidence-backed detector exceptions through Impeccable.
- Verify both pages on desktop/mobile and in both themes, exercise controls,
  rerun documentation/package contracts, then commit and push to PR #77.

## Initial evidence

- Main starts at `f3e67b9`, the merge of PR #76. The landing page companion
  PR #1 is merged independently.
- PyPI currently serves 0.19.0. The 0.20.0 GitHub release does not exist.
- PR #76's Linux/Python 3.13 and macOS test jobs hit the 30-minute job limit.
  Quality, coverage, package, minimum-dependency, and other Linux tests passed.
- Current branch protection requires review and names historical test checks.
  Do not change protection or use an admin merge to work around it.

## Verification and delivery

- Version metadata and the complete 0.20.0 changelog are prepared. Dependency
  versions are unchanged; the refreshed lockfile passes CI's uv 0.12.5 check.
- Timeout TDD: seven cases failed against the old budgets. All 16 focused
  release/CI contracts and 154 configuration/workflow checks then passed.
- The release-ready documentation describes version applicability, uses
  package installation for guided onboarding, and preserves historical Goose
  references. This initial alignment did not change HTML styles, scripts,
  or SVGs; the subsequently approved design follow-up is recorded below.
- The obsolete source-install TLDR assertion was updated for package-based
  0.20.0 onboarding. Combined release/docs/packaging checks: 67 passed.
- Required-check compatibility TDD failed nine cases before the aggregate
  jobs were added; all 24 release/CI contracts now pass. Both historical OS
  check names strictly require the complete matrix and are themselves required
  by the CI gate. They have no permissions and a five-minute cutoff.
- The complete canonical gate passed on `b9495ae`: 1,872 tests passed with
  88.85% coverage on macOS/Python 3.12.13. Workflow consistency, format, lint,
  and mypy for 24 source files passed, with no source mutation before builds.
  The 0.20.0 wheel and sdist passed isolated installed-package checks on
  Python 3.13.15, including automatic/guided rehearsal and local decisions.
- Release PR #77 is open and ready for review. Hosted Linux/Python 3.12,
  3.13, and 3.14, minimum dependencies, coverage, complete packaging, quality,
  and CodeQL passed on `b9495ae`.
- All three hosted macOS/Python lanes exhausted 60 minutes after steady progress
  to 73%, with no test failure markers in the completed logs. GitHub's check
  annotations confirm the execution cutoff. Large real-Git lifecycle groups
  took roughly three times their passing Linux duration. Raise the bounded
  matrix budget to 90 minutes and complete-build budgets to 120 minutes;
  retain every check and verify the updated candidate independently.
- Timeout follow-up TDD failed three budget cases before the final adjustment.
  All 24 release/CI contracts and 75 combined release/docs/packaging tests then
  passed. Format and managed-workflow consistency checks passed. Independent
  review confirmed that the follow-up changes only limits and release evidence;
  no runtime source, platform lanes, or publication safeguards changed.
- Hosted CI on `9e885c4` completed: Linux/Python 3.12–3.14, macOS/Python
  3.13–3.14, minimum dependencies, coverage, quality, and the complete package
  gate passed. macOS/Python 3.12 finished in 67 minutes with one failure:
  the signal-cleanup fixture's five-second finalization wait expired. Strict
  required aggregates correctly failed. The exact hosted stall is unproven.
- The process fixture now permits 15 seconds for cleanup and durable terminal
  records, below its 30-second supervised-command deadline. Two controlled
  six-second persistence cases reproduced the old timeout before the fix.
  All exit, raw-log, descendant-cleanup, cancelled projection, and journal
  assertions remain; no runtime code changed. The process file passed all
  23 tests, and 20 repeated normal TERM/HUP cases passed.
- The approved documentation pass improved contrast and functional label
  sizes, relaxed headline tracking, removed duplicate kickers, and replaced
  heavy frames with dividers. Seven nested code panels were resolved by
  flattening the legacy disclosure frame while preserving its commands.
  Static comparison preserved commands, anchors, links, scripts, controls,
  and diagrams apart from four SVG subtitle styling classes.
- Final Impeccable scan of both pages returned no findings. A scoped index
  `wide-tracking` exception records the short uppercase role label, which the
  detector misclassified as prose; no broad detector rule was disabled.
- Browser checks covered desktop and phone layouts in both themes, no
  horizontal overflow, keyboard focus, route selection, exact command copy,
  checklist persistence/reset, disclosure expansion, and walkthrough
  next/back/jump/replay. No browser errors were reported.
- Final combined release/docs/packaging/process checks: 98 passed. The full
  canonical gate result above belongs to `b9495ae`, not these final follow-ups.
  Hosted CI for the final pushed candidate, protected-main review/merge, and
  publication remain pending. Release-ready prose is not publication proof.
