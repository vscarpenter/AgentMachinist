# AgentMachinist documentation

This is the current operating documentation for AgentMachinist **0.14.0**, including
the guided local workflow and optional GitHub/GitLab collaboration. Install with
`uv tool install agentmachinist`, or upgrade an existing tool installation with
`uv tool upgrade agentmachinist`. Start with the
[local workflow guide](local-workflow.md) for your first Task.

**Unreleased / source checkout:** optional `machinist doctor --local` previews
local readiness without adoption, model work, or forge setup. The source also
hardens remote-base validation and diagnostic rendering. These additions are
not in published 0.14.0; use `uv tool install --editable .` from the current
source checkout to try them. See [local readiness](local-workflow.md#optional-local-readiness)
and the [operator runbook](operator-runbook.md).

## Understand the workflow

- [How AgentMachinist works](index.html) — local Task to verified, reviewed change,
  with an interactive walkthrough.
- [One-minute explainer](explainer.html) — animated foreground journey through
  Spec, exact Approval, Execute, Review, integration, and optional publication.
- [First-run field guide](first-run-guide.html) — visual setup and first Task,
  with a separate GitHub automation path.
- [Machinist Job Card](job-card.html) — compact local and GitHub operator checklist.

Start in a clean Git checkout with a configured Harness and a real verification
command. `machinist start "Handle an invalid timezone without crashing"` guides
you through setup, checks the baseline, and creates a local Task and Spec. Read
the Spec, then approve its exact full SHA to run Execute, verification, and
independent Review. Decide whether to integrate locally, publish to GitHub or
GitLab, or retain the candidate for further inspection. No forge is required for
the local journey; the selected Harness may still use a cloud model.

## Adopt and operate it

- [Local workflow and optional publication](local-workflow.md) — installation,
  local configuration, exact Approval, recovery, integration, publication, and teams.
- [Getting Started](getting-started.md) — complete installation, both workflows,
  configuration reference, Harness selection, and troubleshooting.
- [TL;DR](tldr.md) — concise setup and daily commands.
- [Operator runbook](operator-runbook.md) — command scopes, readiness, dispatch,
  amendment, recovery, cancellation, cleanup, and releases.

Use `machinist status T1` for a local Task. `machinist rehearse` exercises the
production local pipeline with a deterministic fake Harness and real Git, without
model calls. For GitHub automation, `machinist onboard --setup-pr` creates or
resumes the setup PR; after merging setup, `machinist doctor --run-gates` checks
GitHub readiness. That doctor command is not a prerequisite for local Tasks.
The unreleased `doctor --local` is optional too; `--run-gates` explicitly runs
project commands in the controller checkout and does not prove the isolated
Workshop baseline.

Numeric issue commands, `runs`, `inspect`, `explain`, `report`, `queue`, and
`watch` describe the legacy GitHub workflow. They do not inspect or schedule
local `T1` records. The runbook's command-scope table explains the distinction.

## Understand the boundaries

- [Architecture and lifecycle](architecture.md) — controller ownership, local and
  GitHub state, Claims, Task Runs, custody, and recovery.
- [Trust model](trust-model.md) — enforced controls, advisory Review, exact-SHA
  Approval, publication binding, and residual risks.
- [Harness support matrix](harnesses.md) — adapters, authentication, plugins,
  compatibility checks, and model connectivity.

## Architecture decisions

- [ADR 0001: Review, plugin, and telemetry boundaries](adr/0001-review-plugin-telemetry-boundaries.md)
- [ADR 0002: Deep module ownership for controller policy](adr/0002-deep-module-ownership.md)
- [ADR 0003: Local workflow and optional publication](adr/0003-local-workflow-and-optional-publication.md)
- [ADR 0004: Local readiness and diagnostic boundaries](adr/0004-local-readiness-and-diagnostic-boundaries.md)

## Historical design records

Files under `superpowers/` preserve earlier specifications and implementation
plans. They explain the product's evolution and include historical worker
instructions and checklists. Each record links to current Getting Started,
Architecture, and Local workflow references; use those references for operation.
ADR applicability notes distinguish retained decisions from the local integration
exception introduced by ADR 0003.

`onboarding.html` remains a redirect to the first-run guide. `CNAME` and
`.nojekyll` retain the existing domain and static-site configuration. These
source pages are deployed separately to
[agentmachinist.vinny.dev](https://agentmachinist.vinny.dev/); editing them does not
publish a release or prove that the public site has been updated.
