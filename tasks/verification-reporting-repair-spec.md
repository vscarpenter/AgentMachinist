# Verification, local reporting, and bounded repair

Status: approved by the September 18, 2026 request to implement the recommended
sequence from the Stripe Minions review. Specification, planning, implementation,
and verification proceed continuously under that approval.

## Goal

Reduce predictable failed handoffs without weakening exact-Spec Approval, Git
custody, independent Review, or explicit retry after a failed Task Run. Implement
three changes: improve this repository's Verification Gates, include foreground
local Tasks in aggregate reporting, and offer one bounded repair round inside an
active Execute Task Run.

## Requirements

1. Replace this repository's legacy pytest-only gate with ordered required,
   nonmutating workflow, format, lint, type, and coverage/test gates using the
   existing verification script. Full package validation stays in the canonical
   verification/release process. Existing saved local configuration is unchanged.
2. Aggregate legacy and local lifecycle histories with separate identity
   namespaces. Support local-only repositories without setup, configuration
   writes, model work, or forge access. Keep existing report fields compatible
   and distinguish Phase success from first-pass Execute success and current
   local delivery state. Missing token usage is unknown, never zero usage.
3. Reporting selects all, legacy, or local sources. Automatic configured legacy
   telemetry must not start exporting local data; local/all export requires an
   explicit destination. Export remains aggregate and contains no Task text.
4. Add `verification.repair.max_attempts`, limited to 0 (default) or 1, and
   `verification.repair.timeout_minutes`, default 10, range 1 through 240. This
   authorizes at most one extra Harness invocation and final Verification within
   the active Execute run. Failed runs still require explicit retry.
5. Repair is eligible only after ordinary required command failures. Advisory
   failures alone, cancellation, timeouts, missing commands, output limits,
   stragglers, custody violations, forbidden mutations, and snapshot errors must
   not start repair. Exit status is only a conservative heuristic for code
   failures, not proof of root cause.
6. Give repair the approved implementation prompt, bounded change summary, and
   bounded sanitized failure Evidence. Logs are untrusted input and cannot
   authorize scope, permission, test/gate weakening, or Git changes.
7. The Phase owns custody and change limits around every process. All configured
   gates run again after repair. Commit, publication, and independent Review
   consume only the final authoritative Verification result.
8. Persist consumed repair budget and an absolute deadline before paid work.
   The extra-work deadline covers repair and its final Verification, with
   cooperative process cancellation. Retain distinct initial/repair logs,
   reports, duration, and outcomes. Resuming never replenishes the budget or
   replays an interrupted paid repair. A fresh explicit retry starts a new
   approved Execute attempt. Post-commit recovery repeats no paid work.
9. Use one shared repair coordinator for local and legacy Execute. Keep the
   deterministic Verification engine and sole dispatcher authoritative.

## Scope exclusions

No remote CI repair, automatic publication/integration, cloud environments,
context retrieval, new rule formats, deterministic autofix stage, model pricing,
Harness usage capture, dashboard, package release, or remote publication.
Check-only gates preserve the local read-only baseline contract.

## Verification

Start behavior changes with failing contract tests. Cover namespace collisions,
local-only reporting, telemetry privacy, missing usage, repair success/exhaustion,
ineligible failures, custody and limits after repair, cancellation/deadlines,
separate logs, and crash/retry recovery without extra repair invocations. Run
focused tests, independent review, then the full canonical verification script,
including types, coverage, workflow drift, distributions, and installed-wheel
smoke checks. Validate real local Git behavior with fake Harnesses; do not spend
on model calls or claim live forge/CI evidence.
