# Toolkit expansion specification

Status: approved through the prior feature-review decision

This specification implements roadmap items 1, 2, 4, 5, 7, and 8 as one
coherent adoption and operations release. It preserves AgentMachinist's
local-first controller, SHA-bound approval, draft-PR safety gate, and explicit
human merge boundary.

## Outcomes

1. An implementation receives an independent, read-only Review Task Run before
   AgentMachinist marks its pull request ready.
2. A first-time adopter can generate setup changes as a reviewable draft pull
   request and can rehearse a lifecycle without creating a real issue or PR.
3. Harnesses are discoverable through a versioned Python entry-point contract,
   and managed Spec CI is rendered from the selected harness rather than being
   hard-coded to Claude Code.
4. Operators can explain one Task's effective policy and watch live status
   transitions without reverse-engineering configuration and runtime files.
5. Task authors get a managed issue form, structured issue creation, and
   readiness linting before expensive agent work begins.
6. Operators get local aggregate reliability reports and may explicitly export
   redacted metrics through OTLP/HTTP JSON.

## Product invariants

- GitHub remains the durable collaboration plane; local runtime records remain
  the durable execution plane.
- No command in this release merges a pull request.
- Setup-PR mode creates only a branch, commit, push, and draft PR after a clean
  preflight. It never edits the default branch directly.
- Review receives the approved Spec, repository diff, gate evidence, and task
  metadata. Its harness argv must use the adapter's read-only controls.
- A successful Execute run leaves the PR draft when Review is enabled. Only a
  successful Review run may mark it ready.
- Review findings are advisory in v1: high-severity findings are made visible
  but do not trigger an autonomous repair loop.
- Plugins are trusted local code, but they cannot silently replace a built-in
  adapter name. Discovery failures are isolated and explained.
- Reports never include issue bodies, prompts, source code, command arguments,
  environment values, or credentials. OTLP export is disabled by default.
- Existing version-1 configuration remains valid. New behaviors use safe
  defaults and managed templates opt in where behavior changes.

## Configuration

### Review

```yaml
review:
  enabled: true
```

`review.enabled` defaults to `false` for existing configurations. The starter
template enables it. Review uses `harness.review` when supplied and otherwise
inherits the base Harness fields. Its timeout defaults to the Spec timeout.

### Harness plugins and CI

`harness.name` and phase-profile names accept a validated adapter identifier
instead of a closed enum. Built-ins retain the names `claude-code`, `codex`,
`opencode`, and `pi`.

Third-party distributions register one Harness subclass per entry point:

```toml
[project.entry-points."agentmachinist.harnesses.v1"]
example = "example_harness:ExampleHarness"
```

The entry-point name and class `name` must match. The class must expose a
version-1 descriptor with install guidance, supported phases, structured-usage
support, and an optional CI Spec profile. Plugin names cannot collide with a
built-in or another loaded plugin.

Managed Spec CI reads the selected Spec adapter's descriptor. The descriptor
provides the install command and default secret environment name. A repository
may override only the validated secret name through
`github.spec_secret_env`; secrets remain GitHub expressions and are never
written to configuration. If an adapter has no CI profile, workflow projection
fails with an actionable message and local Spec remains available.

### Telemetry

```yaml
telemetry:
  otlp_endpoint: null
  timeout_seconds: 5
```

The endpoint is optional and must be HTTP(S). The exporter sends aggregate
metric data only. An authorization header may be read from
`MACHINIST_OTLP_AUTHORIZATION`; configuration never stores it.

## Review phase

### Lifecycle

`Phase.REVIEW` adds `issue-<n>-review.json` projections and normal JSONL
history events. Retry, cancellation, reporting, and queue admission use the
same primitives as Spec and Execute.

When Review is enabled:

1. Execute verifies, commits, pushes, and updates its bounded delivery comment.
2. Execute leaves the pull request draft and records its delivered head SHA.
3. Status projects `awaiting review` when Execute succeeded and the draft PR
   head still matches the delivered SHA.
4. Watch dispatches Review ahead of new Spec work.
5. Review prepares a clean read-only workspace for the exact remote head,
   renders the Review prompt, invokes the review Harness, parses the structured
   report, upserts a bounded PR comment, and marks the PR ready.

The report schema is versioned JSON:

```json
{
  "version": 1,
  "summary": "string",
  "findings": [
    {
      "severity": "low|medium|high",
      "confidence": "low|medium|high",
      "file": "repository/relative/path",
      "line": 1,
      "requirement": "spec or acceptance criterion",
      "message": "actionable explanation",
      "remediation": "bounded next step"
    }
  ]
}
```

Malformed or oversized output fails Review without marking the PR ready. File
paths must be repository-relative and lines positive. A report with findings
still succeeds because v1 is advisory.

The manual command is `machinist review ISSUE`. `machinist retry ISSUE --phase
review` reopens failed/cancelled/abandoned Review work under existing lifecycle
rules.

## Guided adoption

### `machinist onboard`

`machinist onboard` uses the existing setup renderer and defaults to an
interactive guided session. `--no-input` requires enough explicit inputs to
remain deterministic.

`--setup-pr` adds a transactional delivery step:

- require a Git repository with a configured GitHub origin and clean tracked
  worktree before setup;
- create a unique `chore/agentmachinist-setup` branch;
- write only AgentMachinist-managed setup files;
- run configuration, workflow, label-readiness, and doctor preflights that do
  not require the newly pushed branch to be default;
- commit only the generated allowlist, push that branch, and create a draft PR;
- print the PR URL and exact next action.

Failure before push restores the original branch and leaves generated changes
visible for recovery; it does not delete user data. Failure after push reports
the pushed branch and a recovery command.

### `machinist rehearse`

Rehearsal creates an ephemeral local Git repository, sample issue/Spec, and a
deterministic verification gate. By default it performs a no-cost controller
simulation and prints the lifecycle transitions. `--harness` explicitly invokes
the configured Harness against the disposable repository. It never binds a
GitHub client, pushes, or creates remote artifacts. Temporary data is removed
on success and retained with its path on failure.

## Explain and live status

`machinist explain ISSUE [--json]` is side-effect free and reports:

- current pipeline state and exact next command;
- resolved Spec, Execute, and Review harness/model/timeout profiles;
- instruction discovery policy and verification gates;
- workspace strategy, cleanup policy, denied paths, and mutation limits;
- configured credential names (never values) and reduced-environment policy;
- queue limits, cancellation state, attempts, duration, and retained workspace;
- managed CI ownership and Review readiness behavior.

`machinist status --watch [--interval SECONDS]` refreshes only when the read
model changes. Human output clears/redraws only on a TTY; non-TTY output emits
timestamped snapshots. `--json --watch` emits one compact JSON object per line.
Ctrl-C prints `status watch stopped.` and exits successfully.

## Task intake

The managed issue form lives at
`.github/ISSUE_TEMPLATE/agentmachinist-task.yml`. It captures objective,
acceptance criteria, constraints, verification, and context. Managed-file
ownership uses the same sealed-content protections as workflows.

Commands:

- `machinist task template --write|--check` projects or checks the issue form.
- `machinist task new --title TITLE` prompts for the five sections, creates an
  unlabeled issue by default, prints its URL, then prints the lint command.
  `--dispatch` applies the configured trigger label after a passing local lint.
- `machinist task lint ISSUE [--json]` validates the live GitHub issue. Missing
  sections, placeholder text, vague one-line objectives, absent acceptance
  checkboxes, and absent verification steps are actionable errors. Warnings do
  not block dispatch; errors do.

## Reporting and OTLP export

`machinist report --since DURATION [--json] [--otlp-endpoint URL]` reads local
JSONL history. Accepted durations are integer values with `h`, `d`, or `w`
suffixes.

The report includes:

- attempts and terminal outcomes by phase;
- success rate, retry count, cancellation count, and median/p95 duration;
- failure categories derived from exception type and controller checkpoint;
- verification-gate failure counts when recorded;
- Harness/model breakdown only when present in structured evidence;
- token totals only when a Harness declares structured usage and records it.

Export uses OTLP/HTTP JSON metric names prefixed `machinist.`. The payload has
repository identity, phase, status, harness, and model attributes only. The CLI
prints export success/failure separately from the local report. Export failure
returns non-zero but never modifies local history.

## Error and recovery states

- Unknown adapter: list installed adapters and the entry-point group.
- Broken plugin: identify the distribution/entry point without importing other
  plugins unsuccessfully.
- Review head changed: fail closed and require a fresh Execute approval/run.
- Review output invalid: leave PR draft and point to `machinist retry`.
- Setup dirty tree: list paths and stop before mutation.
- Rehearsal Harness failure: retain the temporary path and print the gate/result.
- Issue-form drift: refuse overwrite of unrecognized content.
- Task lint failure: print each field and its concrete correction.
- Invalid report window or endpoint: fail before reading or transmitting data.
- OTLP timeout/HTTP error: retain local report and redact response bodies.

## Verification strategy

- Unit tests pin configuration compatibility, plugin discovery isolation,
  adapter descriptors, workflow rendering, Review parsing, status/watch
  projection, issue-form ownership, lint rules, reports, and OTLP redaction.
- Phase tests prove Execute stays draft and Review alone marks ready.
- CLI tests cover every new command's success, empty, invalid, interrupted, and
  recovery states.
- Packaging tests install a fixture entry-point plugin from an isolated path.
- Existing tests must remain green; the full verification script is the release
  gate.

## Non-goals

- Autonomous repair loops or merging after Review.
- A hosted control plane, web dashboard, or central telemetry collector.
- Sending prompts, source, diffs, logs, tool arguments, or credentials to OTLP.
- Replacing GitHub issue/PR collaboration with a proprietary task database.
- Automatically installing secrets or changing branch protection.

