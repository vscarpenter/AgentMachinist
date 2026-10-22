# Decision output implementation plan

Approved scope: `tasks/decision-output-spec.md`.

1. Preserve the user's proposal; work on `codex/local-decision-output`.
2. Add failing inspection contracts and implement a bounded summary using the
   current validated read model, with accumulated Task paths and counts derived
   before detail truncation. Keep exact delivery guards and repeated actions.
3. Add failing Spec revision read/display contracts. Reuse saved commit identity,
   safe bounded blob reads, and diagnostic sanitization; show unchanged text and
   fresh Approval explicitly. Keep comparison failure separate from successful
   revision delivery.
4. Update the existing README/local workflow guide and Unreleased changelog.
5. Review the combined diff independently, run the canonical verification gate,
   record concrete results, and commit the completed implementation locally.

No extra human Approval gate follows the user's implementation request.
