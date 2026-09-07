# AgentMachinist documentation

This is the current operating documentation for AgentMachinist. Start with the
path that matches what you are trying to do:

## Understand the workflow

- [How AgentMachinist works](https://agentmachinist.vinny.dev/) — concept-first
  product overview.
- [One-minute explainer](https://agentmachinist.vinny.dev/explainer.html) —
  animated issue-to-PR walkthrough.
- [First-run field guide](https://agentmachinist.vinny.dev/first-run-guide.html)
  — visual setup for the existing GitHub integration.

## Adopt and operate it

- [Getting Started](getting-started.md) — complete installation, configuration,
  first Task, and troubleshooting reference.
- [Local workflow and optional publication](local-workflow.md) — foreground
  Tasks, exact Approval, local integration, GitHub/GitLab publication, and teams.
- [TL;DR](tldr.md) — one setup checklist plus concise local and GitHub Actions
  Task flows.
- [Machinist Job Card](https://agentmachinist.vinny.dev/job-card.html) — compact
  local-versus-Actions checklist.
- [Operator runbook](operator-runbook.md) — readiness, dispatch, recovery,
  cancellation, cleanup, and releases.

## Understand the boundaries

- [Architecture and lifecycle](architecture.md) — ownership, state, Claims,
  Task Runs, and recovery.
- [Trust model](trust-model.md) — enforced controls, advisory controls, and
  residual risks.
- [Harness support matrix](harnesses.md) — adapter behavior, authentication,
  and compatibility checks.

Start locally with `machinist start "Handle an invalid timezone without crashing"`.
Read the saved Spec and approve its exact SHA to continue implementation and
Review. Optional `integrate` and `publish` commands are explicit human actions.
This local journey is unreleased; use the source-install instructions in the
local workflow guide. For the existing GitHub automation, use
`machinist onboard` (or `machinist onboard --yes` for hands-free defaults and a
detected test command), merge setup, then run `machinist doctor --run-gates`
— the single health check that verifies labels, workflows, the sealed issue form,
and verification gates and prints the exact fix for any `FAIL`. Only run
`machinist sync-labels --check`, `machinist sync-workflows --check`, or
`machinist task template --check` if doctor asks. Use `machinist rehearse` to
prove the controller flow without a model. During operation, `machinist explain
<issue>`, `machinist status --watch`, and `machinist report` expose effective
policy, live state, and local reliability. Run `machinist --help` for grouped
`Setup`, `Tasks`, `Build`, and `Operate — daily` vs `Operate — advanced` help.

## Architecture decisions

- [ADR 0001: Review, plugin, and telemetry boundaries](adr/0001-review-plugin-telemetry-boundaries.md)
- [ADR 0002: Deep module ownership for controller policy](adr/0002-deep-module-ownership.md)
- [ADR 0003: Local workflow and optional publication](adr/0003-local-workflow-and-optional-publication.md)

## Historical design records

Files under `superpowers/` preserve earlier specifications and implementation
plans. They explain why the product evolved, but they are not current operating
documentation. Each record links back to the current Getting Started and
Architecture references.
