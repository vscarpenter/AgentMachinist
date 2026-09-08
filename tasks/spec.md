# Local readiness, remote-base validation, and diagnostics

Status: approved by the September 7, 2026 user request and preceding discussion.
Proceed through specification, plan, implementation, and verification continuously.
Previous completed workflow specification remains in Git at f1c9190:tasks/spec.md.

## Goal

Add optional local readiness diagnostics, then harden legacy remote-base selection
and controller error rendering. No additional onboarding steps or required config.

## Inputs and outputs

- `machinist doctor --local [--json] [--run-gates]` diagnoses the current local
  repository. Existing `doctor` retains its GitHub-oriented behavior.
- Diagnose before first local setup using the same discovery/validation as start,
  or reuse `.machinist/runs/local/config.yaml` when present. Preserve user choices.
- Report Git repository, committed named branch, commit identity, cleanliness,
  Harness support/availability, local configuration, and verification availability.
  Safe existing Harness version/compatibility/authentication probes may run;
  no model invocation, forge probe, update lookup, or Task creation is included.
- Without `--run-gates`, no verification commands run. With it, use the existing
  Verification engine and clearly distinguish command availability from passing
  verification. Reuse the doctor JSON shape and failure exit status.
- When no remote Task branch exists, legacy Workshop provisioning fetches the
  intended remote base exactly and rejects deleted/stale bases before
  constructing a Workshop. Existing remote Task heads remain recovery authority. Local Workshop
  provisioning continues to use committed local state without remote access.
- Render bounded diagnostic text with URL credentials, authorization values, and
  recognized secret assignments redacted and unsafe terminal controls removed.
  Share this behavior across Git, GitHub, GitLab, and doctor failure reporting.

## Constraints

- Read-only readiness must not save configuration, change Git exclusions/refs,
  create Tasks, Claims, Workshops or runtime files, or contact a forge.
- Share local configuration resolution with start; do not maintain duplicate
  defaults, Harness selection, or verification requirements.
- Diagnose the existing controller commit-identity fallback without imposing a
  new repository-local author configuration step on otherwise ready users.
- Preserve exact-SHA Approval, custody, retries, leases, and remote Task-head
  recovery. Do not replace pinned SHA checkout with mutable tracking refs.
- No new dependency, required configuration field, wizard, package version,
  release, external mutation, or change to the alternate checkout.
- Diagnostic sanitization is defense in depth, not proof arbitrary output is
  secret-free. Preserve useful failure context and existing exception categories.

## Acceptance criteria and edge cases

1. A no-origin repository can receive local readiness and actionable JSON/text
   without root config, gh/glab, model calls, or filesystem changes.
2. Existing local settings win over root settings; first-run discovery matches
   start. Missing gates/Harnesses, dirty/empty/detached repos, author problems,
   and malformed/unsafe config produce clear failures without writes.
3. Local readiness plus run-gates uses the configured Verification engine only
   when explicitly requested; command discovery does not claim tests passed.
4. Existing doctor CLI behavior and JSON contract remain compatible.
5. Deleted/renamed remote bases with stale tracking refs fail early; narrow
   clones, remote Task branches, retries/revisions, and local-only runs work.
6. Synthetic credentials, long stderr, terminal controls, multiline errors, and
   ordinary failures receive consistent safe bounded rendering.
7. Documentation and changelog identify unreleased additions and explain that
   readiness is optional, local checks do not require a forge, and gate execution
   remains explicit.

## Verification

Start behavior changes with failing contract tests. Run focused module/CLI tests,
independent review, then the canonical format/lint/type/coverage/workflow/package
checks. Exercise local doctor and the installed wheel in temporary repositories
with deterministic fake Harness probes, never paid model work. Commit coherent
changes locally; publication is a separate request.
