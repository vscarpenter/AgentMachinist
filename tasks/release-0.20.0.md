# Release 0.20.0

## Scope and authorization

Vinny requested creation and push of 0.20.0 after the onboarding and landing
page PRs merged. Prepare a minor release containing all changes since 0.19.0,
preserve the full verification gates, and push a reviewable release PR.
`CLAUDE.md` keeps GitHub Release creation and PyPI publication separate from a
version bump and push. Do not bypass protected-main review or required checks.
Website deployment is outside this release-preparation request.

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
  references. No HTML styles, scripts, or SVGs changed.
- The obsolete source-install TLDR assertion was updated for package-based
  0.20.0 onboarding. Combined release/docs/packaging checks: 67 passed.
- Final canonical verification, hosted CI, PR delivery, and publication remain
  pending. Release-ready prose in the draft branch is not PyPI publication proof.
