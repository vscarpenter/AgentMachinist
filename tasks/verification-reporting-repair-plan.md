# Implementation plan: verification, local reporting, and bounded repair

1. Record the approved specification and create an implementation branch.
2. Replace dogfood gate configuration with existing ordered check-only commands;
   validate schema and generated workflow parity. Commit this independently.
3. Write failing reporting contracts, then add source-aware history collection,
   honest first-pass/usage/repair metrics, local delivery snapshots, and explicit
   local export consent. Preserve existing aggregates and source identities.
4. Write failing repair/config/Evidence contracts. Add the bounded coordinator
   and immutable budget/deadline state, then integrate it into both Execute
   paths with per-invocation custody, limits, snapshots, and separate logs.
5. Test ordinary failure repair, exhausted and unsafe cases, persisted budget,
   explicit resume/fresh retry, and post-commit recovery. Keep default-off
   behavior compatible with existing callers and Task Run history.
6. Update operating/architecture/trust references, config examples, changelog,
   and task ledger; clarify local saved settings and telemetry semantics.
7. Independently review implementation and recovery semantics. Resolve findings,
   run the canonical gate, and commit coherent completed changes locally.

Report progress and verification evidence in `tasks/todo.md`. Publication and
release require a separate request.
