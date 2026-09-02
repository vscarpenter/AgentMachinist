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
