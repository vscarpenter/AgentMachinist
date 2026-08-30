# Toolkit expansion implementation notes

## 2026-08-30

- Classified the work as non-trivial under `coding-standards.md`.
- Treated the user's selected roadmap items as approval of the previously
  reviewed feature shapes, per the standing continuous spec-to-implementation
  instruction.
- Kept `coding-standards.md` as an untracked user-owned input; it is not part of
  the feature's commit allowlist unless the user later asks to add it.
- Chose a first-class Review lifecycle Phase, versioned entry-point adapter
  contract, managed issue-form projection, and local-first aggregate reporting.
- Verified current Harness installation/authentication guidance against primary
  vendor documentation before specifying provider-aware CI.
- Kept live status as a changed-only iterator so TTY redraw, non-TTY snapshots,
  and NDJSON all share one deterministic read model.
- Installed the issue form even when managed Actions workflows are disabled;
  task structure and workflow ownership are independent adoption controls.
- Split local aggregation from network export. Reporting reads the full local
  history, while the OTLP projector can receive only aggregate fields and emits
  an attribute allowlist by construction.
- Extended the existing observability filename parser to include Review; the
  first-class Phase was already durable, but the read model's regex still
  recognized only Spec and Execute history.
