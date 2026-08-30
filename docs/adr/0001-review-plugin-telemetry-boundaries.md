# ADR 0001: Review, plugin, and telemetry boundaries

Date: 2026-08-30

Status: accepted

## Context

AgentMachinist needs an independent review step, extensible Harness support,
provider-neutral Spec CI, and useful operational metrics. These additions cross
the controller's highest-trust boundaries: code execution, managed GitHub
files, plugin import, and external data transmission.

## Decision

Review is a first-class local `Phase`, not a substep hidden inside Execute.
Execute owns implementation delivery and leaves the PR draft. Review owns a
read-only Harness invocation, a versioned structured report, and the transition
to ready-for-review. Findings are advisory in the first contract version.

Harness plugins use Python entry points in the versioned group
`agentmachinist.harnesses.v1`. A loaded object must be a Harness subclass with
a matching name and descriptor version. Built-in names are reserved. Discovery
isolates entry-point failures and reports them without discarding healthy
adapters. Managed Spec CI is rendered from the resolved adapter descriptor; a
Harness without a CI profile cannot be selected for GitHub-hosted Spec.

Operational reports are derived locally from the existing append-only Task Run
journal. Optional export uses a small OTLP/HTTP JSON client in the standard
library. The exporter accepts aggregate metrics only and constructs its own
allowlisted attributes. It never serializes Task evidence wholesale.

## Consequences

- The pipeline state model gains `awaiting review` and Review run states.
- Existing configurations remain two-phase unless Review is explicitly enabled;
  new starter configurations enable it.
- Adapter authors have a stable versioned integration seam and conformance
  tests, but plugin code is trusted code running in the controller process.
- CI projection fails early when a selected adapter cannot describe a safe Spec
  installation/authentication path.
- Local reports work offline. External export has an explicit configuration or
  CLI boundary and a deliberately narrow schema.
- Review cannot prove semantic correctness; it adds independent evidence before
  human review without weakening the human merge gate.

## Alternatives rejected

- Running Review inside Execute: hides retries and lets one Harness both produce
  and certify its work.
- Treating findings as an automatic implementation veto: creates an unbounded
  agent loop before the structured report contract has operational evidence.
- A mutable global adapter registry API: makes import order and collisions hard
  to reason about; entry points provide packaging metadata and deterministic
  discovery.
- Exporting Task Run evidence as tracing events: evidence can contain paths,
  diagnostics, and future fields that were not designed for disclosure.
- Adding an OpenTelemetry SDK dependency: unnecessary for the first aggregate
  OTLP/HTTP metrics contract and increases adoption cost.

