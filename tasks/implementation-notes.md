# Architecture deepening implementation ledger

## 2026-09-02

- The user approved all seven review recommendations as one pass. Under the
  standing repository correction, that approval covers spec, plan, and
  implementation without another approval pause.
- Preserve version-1 Task Run and configuration persistence. Deep interfaces may
  validate new internal writes more strongly but must continue reading historical
  open-ended Evidence.
- Keep CLI presentation and notification policy in `cli.py`; move only Task Run
  construction and dependency wiring into dispatch.
- No tactical deviations yet.
- Evidence persistence remains an open JSON mapping. `TaskEvidence` owns known
  typed reads and `checkpoint_evidence` validates only newly written known fields,
  so weakly typed historical values stay inspectable instead of becoming an
  upgrade-time failure.
- `RunInventory` now includes attempts, orphans, and typed corrupt artifacts while
  retaining its original `records` and `corrupt` fields for compatibility.
- `PipelineState` retains every existing rendered value. `TransitionDecision`
  centralizes priority, dispatch Phase, and next action; Task Run dispositions now
  consume the same model without adding retryable/succeeded pipeline states.
- Repository custody now returns a normalized host/identity and structured PR
  mismatch reasons. Phase code translates those reasons into its existing
  operator-facing failure context without reimplementing the checks.
- Execute imports the authoritative Verification Gate implementation directly.
  The internal fallback was deleted; existing Gate behavior tests cover the sole
  path.
- `TaskDispatcher` now owns lifecycle entry and Phase dependency construction for
  Spec, Execute, Review, watcher dispatch, retry-now, and amendments. Click keeps
  argument checks, progress rendering, result text, and notifications.
- Runner functions remain injected by the CLI adapter so existing narrow command
  tests can replace Phase behavior without bypassing dispatcher contracts.
- `MachinistConfig` now owns both the sparse validated starter projection and the
  compatibility-resolved effective projection. `config_cli` only renders, loads,
  and persists those model-owned values; the Click adapter no longer restates
  defaults.
- Documentation now names all three Phases and records each deep module's
  Interface. No operator-facing, persisted, or external contract changed during
  implementation.
