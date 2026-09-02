# ADR 0002: Deep module ownership for controller policy

Date: 2026-09-02

Status: accepted

Deciders: Vinny Carpenter

## Context

AgentMachinist's operator workflow is mature, but several policies are interpreted
in multiple places. Evidence keys are decoded by each Phase and report, repository
custody is repeated in Spec and Execute, Click commands reconstruct Task Runs,
status strings double as dispatch rules, configuration defaults are projected by
several implementations, and journal filenames are parsed outside lifecycle.
Execute also retains a second Verification Gate implementation as a fallback.

This duplication is concentrated around the controller's highest-leverage and
highest-trust decisions. A correction in one copy can leave another copy stale.

## Decision

Give each policy one deep module:

- Evidence owns known key vocabulary, typed interpretation, and Phase-aware new
  checkpoint validation while preserving open-ended version-1 JSON persistence.
- Repository custody owns origin/GitHub binding and exact PR identity checks.
- Dispatch owns Task Run construction for Spec, Execute, and Review.
- Verification has one authoritative implementation with no internal fallback.
- Pipeline transitions own state vocabulary, ordering, eligibility, and next
  actions.
- Validated configuration owns starter and effective projection behavior.
- Lifecycle owns append-only journal discovery and artifact interpretation.

Existing public and persisted contracts remain compatible. The controller still
owns every mutation, Review remains a first-class read-only Phase, and the human
merge Gate remains outside AgentMachinist.

## Consequences

- Security-sensitive fixes gain leverage because one implementation protects all
  Phases and command paths.
- Internal callers depend on smaller interfaces and no longer learn storage key or
  filename grammar.
- Some new internal modules are introduced, but each deletes repeated policy and
  can be tested without Click or external processes.
- Historical Evidence remains readable, so validation is deliberately stricter for
  new known checkpoints than for loaded open-ended data.
- The version-1 Task Run and configuration schemas do not require migration.

## Alternatives rejected

- Keep duplicated helpers and rely on parity tests: detects drift after it happens
  and leaves every policy change multi-file.
- Replace Task Run Evidence with a closed persisted schema: breaks forward and
  backward compatibility for existing local records.
- Move Phase policy into Click commands: improves no locality and makes reuse and
  focused testing harder.
- Retain the Verification fallback for defensive compatibility: internal module
  loading is not a public compatibility contract, and two engines can disagree on
  a Gate result.
