# AgentMachinist documentation

This is the current operating documentation for AgentMachinist. Start with the
path that matches what you are trying to do:

## Understand the workflow

- [How AgentMachinist works](https://agentmachinist.vinny.dev/) — concept-first
  product overview.
- [One-minute explainer](https://agentmachinist.vinny.dev/explainer.html) —
  animated issue-to-PR walkthrough.
- [First-run field guide](https://agentmachinist.vinny.dev/first-run-guide.html)
  — visual, copy-and-run setup.

## Adopt and operate it

- [Getting Started](getting-started.md) — complete installation, configuration,
  first Task, and troubleshooting reference.
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

Run `machinist doctor --run-gates`, `machinist sync-labels --check`, and
`machinist sync-workflows --check` before the first unattended Task. Those
commands distinguish configuration, local verification, GitHub labels, and
workflow deployment instead of treating setup as one opaque pass.

## Historical design records

Files under `superpowers/` preserve earlier specifications and implementation
plans. They explain why the product evolved, but they are not current operating
documentation. Each record links back to the current Getting Started and
Architecture references.
