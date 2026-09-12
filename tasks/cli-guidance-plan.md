# CLI completion guidance

Make the existing workflow easier to follow through concrete completion receipts.
This is an additive guidance pass authorized by the September 12 request. Keep
all commands, configuration, Phase execution, and human Gates available.

## Contract

- After creating a GitHub issue, recommend a useful next activity with its actual
  identifier. Do not ask users to lint the input that just passed local lint.
- Respect the configured Spec source. Local generation and hosted generation
  must not be suggested together; label commands must safely quote custom labels.
- Spec completion asks the user to read it before showing one primary Approval
  command. Approval requests explain asynchronous processing and how to inspect it.
- Completed Review points to human review. Local integration remains explicit;
  completion makes publication optional and shows its required provider selector.
- Status guidance must agree with receipts while waiting for hosted Spec or
  Approval. Dispatch eligibility and all trust controls stay unchanged.
- Preserve structured output and avoid additional remote calls or automatic work
  just to display hints. Watcher notification-only callbacks stay quiet.

## Plan and verification

1. Add failing command contracts for dispatch mode, shell quoting, Approval
   selectors, shared Spec receipts, optional publication, and structured output.
2. Update completion rendering and canonical next-action wording only.
3. Update the quickstart, workflow guides, and Unreleased changelog.
4. Run focused tests, independent review, and `bash scripts/verify.sh`.
5. Exercise the real local workflow in a disposable repository using the
   deterministic Harness, then commit the verified change locally.

No new wizard, command aliases, shell tab completion, persistence migration,
package version change, release, or remote publication is included.
