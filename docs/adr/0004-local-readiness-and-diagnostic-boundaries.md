# ADR 0004: Local readiness and diagnostic boundaries

Date: 2026-09-07

Status: Accepted

Deciders: Vinny Carpenter, through approval of local readiness followed by
remote-base validation and diagnostics.

Implementation status: unreleased; available in the source checkout. The
published 0.14.0 local workflow is described in [ADR 0003](0003-local-workflow-and-optional-publication.md).

## Context

A solo developer needs to discover local setup problems without adopting a
repository, creating a Task, or setting up a forge. Maintaining a separate
readiness configuration would let diagnostics disagree with first start.
Legacy Git provisioning can accept a stale tracking ref after its remote base
is deleted. Subprocess errors can include credentials or terminal controls.

## Decision

Add optional `doctor --local`, sharing read-only configuration resolution with
`ensure_local_config`. The resolver preserves saved local settings and uses the
same first-start discovery and validation when they are absent. Only setup
persists settings and exclusions. Reuse the existing doctor report, Harness
probes, and Verification engine. Keep plain doctor's GitHub behavior.

Follow the controller's effective commit-identity policy and allow supported
Workshop locations. Check existing runtime exclusions and the safety of any
path setup must modify. Report an unestablished exclusion as a warning: start
must still apply and verify it, including repository ignore-rule precedence.

Default local readiness creates no runtime state, Task, Claim, or Workshop and
makes no forge, update, or model call. Safe adapter version/help/authentication
probes can run. Command availability is separate from execution: `--run-gates`
opts into project commands in the controller checkout, which can write files or
fetch dependencies. This is not proof that a fresh Workshop is ready.

For a new legacy Task without a remote Task branch, fetch the exact origin base
ref and capture its commit before Workshop construction. Do not fall back to a
stale ref or local branch. Preserve the existing remote Task branch as the
authority for revision/recovery. Local Workshop provisioning stays local.

Own text sanitization in one pure helper: remove recognized credentials and
unsafe terminal controls, then bound the complete diagnostic. Apply it to
Workspace exceptions, forge invocation failures, and doctor details. Preserve
successful Git/forge data and exception categories.

## Consequences

- No new required setup step, dependency, configuration field, or service.
- Local readiness can run before first start or against saved local settings.
- An unavailable remote base stops legacy work before a Workshop is created;
  later remote movement cannot silently change the captured checkout SHA.
- Shared diagnostic policy reduces drift, but is not a detector for arbitrary
  secrets and does not sanitize raw Harness or Verification logs.
- An optional local health check does not imply offline model inference or
  guarantee provider access. Task baseline Verification remains necessary.

## Alternatives rejected

Creating configuration or a disposable Task for diagnostics would make a
health check change adoption state. Requiring forge readiness would defeat the
local entry point. Wildcard fetch without pruning leaves stale bases usable;
fetching the requested ref provides direct failure and a pinned commit.
