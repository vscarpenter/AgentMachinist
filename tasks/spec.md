# Architecture deepening specification

Status: approved by the user on 2026-09-02

This specification implements all seven recommendations from the whole-codebase
architecture review in one continuous pass. The change deepens existing modules
without changing AgentMachinist's operator-facing workflow or trust model.

## Goal

Concentrate Task Run Evidence, repository custody, Phase dispatch, pipeline
transitions, configuration behavior, verification, and journal interpretation so
each policy has one authoritative implementation.

## Inputs and outputs

- Inputs remain the current CLI arguments, `machinist.yaml` schema, GitHub state,
  Workshop state, and version-1 Task Run projections/journals.
- Outputs remain the current CLI text/JSON, GitHub mutations, Task Run JSON, local
  reports, and managed files.
- Internal callers receive typed Evidence interpretation, one repository-custody
  decision, one Phase-dispatch implementation, one transition decision, one
  configuration projection, one Verification Gate implementation, and one Task
  Run inventory.

## Constraints

- The controller, never the Harness, owns commits, pushes, PR transitions, Claims,
  and Task Runs.
- Approval remains bound to one exact Spec SHA; Review remains a first-class,
  read-only Phase; human review and merge remain the final Gate.
- Existing Task Run projection and journal schema version 1 remains readable and
  writable without migration.
- `RunRecord.evidence` remains a JSON mapping for persistence and compatibility;
  production code interprets known Evidence through the deep Evidence module.
- Unknown historical Evidence keys remain readable. Known checkpoint keys receive
  Phase-aware type and consistency validation.
- The append-only journal remains the source of attempt history. Callers may not
  parse its directory or filename grammar.
- Configuration version 1 and generated starter behavior remain compatible.
- The Verification Gate remains authoritative after Harness work and keeps its
  current required/advisory, cancellation, timeout, and fail-closed semantics.
- No new dependency is introduced.

## Design

1. Add a deep Evidence module that owns known key vocabulary, typed reads,
   Phase-aware checkpoint validation, and recovery relationships while preserving
   JSON persistence.
2. Add a repository-custody module that owns origin/GitHub binding, PR-base
   validation, same-repository checks, and exact delivered-PR validation. Phase
   modules retain only their distinct transition policy and error type.
3. Add a Task Run dispatcher that owns Harness, Workshop, cancellation, Claim,
   and Phase-function wiring. Click commands retain argument validation, rendering,
   notifications, and daemon-loop presentation.
4. Remove Execute's dynamic import and fallback Verification Gate. The existing
   verification module becomes the sole implementation.
5. Add a pipeline transition model that owns state vocabulary, ordering,
   dispatch eligibility, and next-action derivation. Status, watch, explain, and
   observability consume that decision model.
6. Make validated configuration own starter projection and effective behavior;
   CLI helpers perform persistence and presentation without restating defaults.
7. Extend lifecycle inventory to own journal discovery, identity parsing, orphan
   classification, and corrupt-artifact meaning. Admission and observability
   consume lifecycle results only.

## Edge cases

- Legacy Task Run Evidence with unknown keys or earlier weakly typed values remains
  readable; new inconsistent known checkpoints fail before persistence.
- A retry after an interrupted Spec or Execute run still reconciles only when the
  intended, observed, and current SHAs agree.
- Cross-repository PRs, mismatched GitHub hosts, changed bases, changed heads, and
  unbound origins continue to fail closed.
- A successful Execute Task Run is dispatchable to Review only when Review is
  enabled and the draft PR head matches delivered Evidence.
- Malformed or noncanonical journal artifacts remain visible and block budgeted
  dispatch as they do today.
- Legacy `tests.command` still resolves to the same effective named Verification
  Gate representation.

## Out of scope

- New commands, configuration fields, pipeline states, persistence migrations, or
  release/version changes.
- Renaming the public `Workspace` code interface to Workshop.
- Splitting every oversized historical file or changing unrelated internals.
- Push, pull-request creation, merge, publication, or deployment.

## Acceptance criteria

1. Spec, Execute, Review, status, reporting, and recovery no longer duplicate
   known Evidence parsing for the migrated fields; checkpoint contradictions are
   rejected by a Phase-aware contract.
2. Spec and Execute use one repository-custody implementation, and Review uses its
   exact-PR checks without weakening Review independence.
3. Spec, Execute, Review, retry-now, amendment, and watcher dispatch enter Task
   Runs through the dispatcher; CLI output and notifications stay unchanged.
4. Execute imports and invokes the verification module directly; the fallback and
   compatibility import path are deleted.
5. `PIPELINE_STATES`, ordering, watcher eligibility, and next actions derive from
   one transition model with unchanged observable values.
6. Starter/effective configuration projections derive from validated model
   behavior and remain semantically identical to current output.
7. Admission and observability contain no Task Run journal path grammar; lifecycle
   inventory supplies attempts, orphans, and typed corrupt artifacts.
8. `CONTEXT.md`, architecture documentation, changelog, and module map describe
   the deepened architecture and include Review in the Phase definition.
9. Targeted tests pass after each red/green/refactor cycle; `scripts/verify.sh`
   passes with at least 80% coverage and the worktree contains no temporary files.

## Test stubs

- Evidence rejects a known key in the wrong Phase and contradictory push SHAs,
  while preserving unknown legacy keys on read.
- Repository custody binds one exact host/repository and reports Phase-specific
  errors for origin, base, head, state, and fork mismatches.
- Dispatcher tests prove dependency construction and lifecycle options for Spec,
  Execute, Review, retry, and amendment without Click.
- Verification tests prove Execute has one direct engine path and preserves
  checkpoint callbacks and required/advisory outcomes.
- Table-driven transition tests cover every pipeline state, priority, Phase, and
  next action; watch consumes transition decisions rather than state literals.
- Configuration tests prove starter text validates and effective output resolves
  defaults, profiles, legacy tests, and path normalization from one model.
- Lifecycle inventory tests cover current, historical, orphaned, malformed, and
  noncanonical artifacts; admission and observability tests use no path parser.

## Verification plan

Run focused tests for each logical change, then run `bash scripts/verify.sh` for
workflow drift, formatting, lint, types, coverage, distributions, and isolated
package smoke tests. Review the final diff and confirm no persisted or public
contract drift.
