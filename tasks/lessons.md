# Lessons

## Toolkit expansion

- When a producer also evaluates its own output, model the independent review as
  a separate durable lifecycle phase. The boundary is more important than the
  prompt wording.
- A plugin contract should version discovery, validate identity, reserve built-in
  names, and isolate load failures before it expands configuration from an enum
  to arbitrary identifiers.
- Telemetry privacy is easier to preserve with an allowlisted aggregate payload
  than by redacting an open-ended evidence object after serialization.
- A generated setup branch is not adoption proof until the same managed-file,
  doctor, and configured verification checks run before its first commit.
- Managed Task structure and managed workflow ownership are separate controls;
  repositories should retain the issue form even when they own Actions files.

## Dependency compatibility

- A PEP 695 type alias can work with current Pydantic while failing schema
  generation on the declared minimum. Exercise configuration model creation
  and JSON Schema generation under minimum dependencies before release.

## Deep module ownership

- Keep persisted Task Run Evidence open for compatibility, but give production
  callers a typed read Interface and validate known fields when checkpoints are
  written. This adds locality without making historical records an upgrade Gate.
- Journal paths are lifecycle implementation detail. Inventory should return
  attempts, orphans, and typed corrupt artifacts so admission and reporting do
  not learn filename grammar.
- Pipeline state strings become hidden policy when callers sort, dispatch, and
  derive recovery commands from them. A transition module should return all four
  decisions together.
- Construct every claimed Phase through one dispatcher Interface. Inject Phase
  functions at the CLI seam so focused command tests remain useful while Claim,
  Harness, Workshop, cancellation, and retry wiring stay in one implementation.
- Repository custody should normalize the controller origin once and reuse exact
  PR identity checks across Spec, Execute, and Review. Verification likewise has
  more leverage as one engine than as a primary path plus an internal fallback.
- Starter files and effective configuration displays should project from the
  validated model. Rendering may choose a sparse presentation, but it should not
  restate runtime defaults.

## Simplification pass (2026-09-03)

- Pin every operator-visible string change with a red `match=` before touching
  code. Two message fixes claimed by the changelog slipped through eleven
  green commits because no test named the new wording; the whole-branch
  review caught them, not the suite.
- When a review merges several findings into one card, keep a checklist of
  the individual findings: the card can be "done" while a small member of it
  is not.
