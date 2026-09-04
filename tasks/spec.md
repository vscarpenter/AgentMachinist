# Resume-push fold and flags-only approve specification

Status: approved by the user on 2026-09-03 (cards 7 and 14 of the adversarial
review; supersedes the completed Spec → Execute simplification spec).

## Goal

Give Execute one push step that both fresh runs and resumed runs re-enter,
make partial-push reconciliation consult the remote branch the controller
pushed to (not only GitHub's lagging PR listing), and drop the positional
`approve TARGET` form so a Gate 1 action can never be ambiguous.

## Inputs and outputs

- Task Run Evidence keys are unchanged (`push_intended_sha`,
  `push_observed_sha`, `implementation_sha`, `approved_sha`).
- Public CLI change (documented, CHANGELOG): `machinist approve` takes exactly
  one of `--issue <n>` or `--pr <n>`; the positional target and its ambiguity
  error are removed.
- Operator message change: a fresh run whose Workshop or remote head no longer
  matches the approved SHA says the remote Task branch moved after Approval
  and to inspect it before approving a new head; it no longer says "approve
  the current head again".

## Constraints

- Invariant 4: every push stays leased on the approved SHA.
- Invariant 7: a crash after commit or after push is reconciled from
  checkpoints; the Harness never reruns; gates rerun only when the crash
  preceded the implementation commit.
- Invariant 2 untouched: `approve` still posts `/machinist-execute <observed
  head>` and the workflow re-reads the head.
- `scripts/verify.sh` passes.

## Design

1. `_reconciled_push(previous, approval_sha, pr, *, read_remote_sha, force)`
   returns the intended SHA when it equals GitHub's PR head or, failing that,
   the remote branch head read lazily through `read_remote_sha` (called only
   when a prior push intent exists, so first attempts pay no `ls-remote`).
   Execute passes `lambda: workspace.remote_sha(workspace.repo_root, branch)`.
2. The `"push"` resume stage no longer has its own helper. Execute sets
   `implementation_sha` and the delivery inputs from prior Evidence, skips the
   Harness, gates, and commit, and re-enters the same leased push, observation,
   checkpoint, and delivery the fresh path uses (`recovered=True`).
   `_retry_intended_push` is deleted. A remote that moved elsewhere fails the
   lease loudly; a remote already at the implementation is a no-op push.
3. `_assert_head` messages for the no-actor case name the moved remote branch
   and point at `machinist inspect`; they keep the phrase "approved SHA".
4. `approve` takes exactly one of `--issue`/`--pr` and resolves the PR by
   number or by `<branch_prefix>issue-<n>`; docs (README, getting-started,
   CLAUDE.md) and the docstring drop the positional form.

## Acceptance criteria

1. A fresh run after a crashed push whose PR listing still shows the approved
   head reconciles and delivers without provisioning a Workshop or running
   the Harness.
2. `--resume` after a crash between commit and push observation re-enters the
   shared push step; the Harness and gates do not rerun; `push` is called with
   the approved SHA as its lease.
3. No Execute message contains "approve the current"; head mismatch messages
   still contain "approved SHA".
4. `machinist approve 42` is a Click usage error; `--issue` and `--pr` work;
   both or neither is a usage error.
5. Docs tests pass; CHANGELOG has an Unreleased section for both changes.
