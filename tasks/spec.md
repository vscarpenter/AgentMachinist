# Spec → Approval → Execute simplification specification

Status: approved by the user on 2026-09-03 (design = the adversarial review
report's Strong cards; supersedes the completed 2026-09-02 deepening spec).

This specification implements the top recommendation and every Strong card of
the 2026-09-03 adversarial simplification review of the Spec → Approval →
Execute path, plus the two Strong findings inside card 11 and the invariant 7
documentation correction. It deletes interface width that only test doubles
use, duplicated policy that 0.12.0 left behind, and Evidence nobody reads,
without changing the operator workflow except where stated.

## Goal

Make the Workshop, Verification Gate, Evidence, Gate 1, and CLI seam each own
their policy once, so the Execute Phase shrinks to the linear sequence the
architecture documents and the remaining interface is the one the single
dispatcher actually sends.

## Inputs and outputs

- Inputs remain the current `machinist.yaml` schema, GitHub state, Workshop
  state, and version-1 Task Run projections/journals.
- Outputs remain the current Task Run JSON schema (open mapping; some known
  keys are no longer written), GitHub mutations, and local reports.
- Public CLI changes (documented, CHANGELOG): `run` loses `--retry`,
  `--resume`, `--fresh` (`retry <n> --phase execute --run [--resume|--fresh]`
  is the one recovery entry); `approve_pr` loses its ignored `label`
  argument (internal client).
- Trust change (documented, CHANGELOG, CLAUDE.md invariant 2): an approval
  marker is trusted only when authored by `github-actions` or
  `github-actions[bot]`. Human-authored markers are no longer evidence.

## Constraints

- Every core invariant in CLAUDE.md (1–8) is preserved; invariant 2 is
  tightened and its wording updated; invariant 7's wording is corrected to
  match code (tests do not rerun after a crash-after-push; the Harness never
  reruns).
- ADR-0001 and ADR-0002 stand. No module is merged; each becomes deeper.
- Persisted Task Run schema version 1 stays readable and writable; unknown
  historical keys remain readable.
- The raw-before-Git custody ordering on resume is preserved and gains an
  end-to-end test with a real `Workspace`.
- The card-13 exception-wrapper deletion is out of scope (contradicts the
  2026-09-02 spec item 2; needs a separate decision).
- No new dependency. `scripts/verify.sh` passes at ≥ 80% coverage.

## Design

1. **Gate 1 marker trust (card 4).** `GitHubClient.approval_sha` trusts only
   the bot logins. `approve_pr(number, *, head_sha)` drops `label`. Execute's
   refusal names the full `/machinist-execute <sha>` form; the Spec PR body
   stops claiming "machinist only watches the label".
2. **Harness pass-through (card 10).** `Harness._passthrough_argv()` on the
   base class returns `["--model", model]` when set plus `extra_args`; all
   four adapters call it at their prompt-relative position in both profiles.
3. **Preview ownership (card 8).** `Workspace` keeps the in-memory dev/inode
   claim for preview directories and deletes the nonce sidecar file and its
   reserve/assert/remove helpers; a pre-existing target maps to the existing
   `WorkspaceError`.
4. **Verification Evidence (card 5).** Execute uses
   `VerificationReport.as_dict()` for the empty report, `str(exc)` for the
   blocked message, typed `exc.report.failures` for classification, returns
   the change-summary dict directly, reads the PR once before commenting, and
   derives the resume blocker from the persisted report instead of a second
   key.
5. **Unread Evidence (card 3).** Stop writing `ready_intended_sha`,
   `ready_observed_sha`, `review_required_sha`,
   `completion_comment_intended_sha`, `completion_comment_observed_sha`,
   `workspace_head`, `recovery_mode`, `pr_observed_base`,
   `verification_log_dir`, `completion_duration_seconds`, `spec_recovery`,
   `pr_observed_number`, `pr_observed_sha`, `resume_forbidden_reason`; remove
   their vocabulary entries and the `ready_*` relationship from
   `evidence.py`; one instruction-evidence vocabulary produced by
   `InstructionsConfig` for Spec and Execute. Keep `progress_detail` and the
   pre-gate `change_summary` checkpoint.
6. **Custody layer (card 1, top recommendation).** `Workspace` exposes the
   custody token it captured in `provision` (`git_custody(path)`);
   `resume(path, *, branch, expected_sha, git_custody)` rebinds the token and
   asserts it before any Git subprocess, raising the existing "start a fresh
   retry" error when the token is `None`. Execute deletes
   `_capture_workspace_custody`, `_resume_workspace_custody`,
   `_assert_workspace_metadata_custody`, the `git_custody` parameter
   threading, and merges `_assert_approved_head`/`_assert_git_custody` into
   one helper keyed on actor; one `provision(..., attempt=)` form.
   `_assert_owned_checkout` uses the raw layout instead of two
   `git rev-parse` calls, keeping the worktree-parent binding.
7. **Test-double interface (card 2).** `run_spec_phase`, `run_execute_phase`,
   `run_review_phase` require `claim`; the `hasattr`/`getattr` guards for
   `cancel_check`, `mark_draft`, `provision_preview`, `log_directory`,
   `progress`, `log_path`, and `github.repo` go; `revise`, `force`, `feedback`
   are forwarded unconditionally as plain booleans/strings; `recovery` becomes
   `resume: bool`; the dispatcher wires Workshop `cancel_check` beside the
   Harness one. Test doubles gain the members.
8. **Spec custody handoff (card 9).** `repository_custody.verify_branch_pr`
   replaces the hand-rolled identity checks in `_select_delivery_pr` (state
   taken from the observed PR); the read-only-Harness tree check moves into
   `_generate_spec` after the post-harness cancel boundary;
   `TaskDispatcher.preview_spec(issue)` replaces `_preview_harness`
   (`rehearse` keeps an issue-less harness factory); approved-label metadata
   becomes one `github.py` constant; the Phase returns the observed
   `PullRequest`.
9. **CLI seam (card 6).** Delete `run --retry/--resume/--fresh`; one
   `_report_phase_outcome(config, phase, issue, pr, *, verb, notify_only)`
   used by `spec`, `run`, `retry`, `amend`, and the watch closures (notify
   only); `amend` delegates to `_execute_command(issue, *, force=True,
   feedback)`; commands build the dispatcher first and read its stores;
   `DraftPR.head_sha`. (The `watch --dry-run` fold through
   `watch_once(dispatch=False)` is deferred; see Out of scope.)
10. **Transitions and watch (card 11, D-2/D-3).** `RunDisposition` keeps
    `display` and `next_action`; `WatchResult` is a plain dataclass with
    `events`, `deferred`, `attempted`, `failures`, and the CLI reads them.
11. **Docs.** CHANGELOG "Unreleased"; CLAUDE.md invariants 2 and 7, module
    map (`run` flags, harness convention); README/getting-started/
    first-run-guide `run --retry` mentions; `docs/architecture.md` marker
    author text; `docs/harnesses.md` pass-through convention.

## Edge cases

- A marker authored by a human OWNER on an otherwise valid PR now yields
  `approval pending`; the operator re-approves through the workflow.
- Resume with a `None` custody token still refuses with "start a fresh
  retry" before any Git subprocess.
- A crash after commit but before push observation still re-enters the
  leased push with the same SHA; the Harness never reruns.
- Historical Task Run records containing the removed keys load unchanged and
  `inspect` still prints them.
- `retry --run` on an interrupted (RUNNING) record still recovers; the
  deleted `run --retry` could not.
- `watch` still renders its own event lines; the shared renderer only
  notifies there.

## Out of scope

- Card 7 (resume-push restructure), card 11 beyond D-2/D-3, card 12, card 13,
  card 14, the speculative tail, and F-11.
- F-5 (`watch --dry-run` through `watch_once(dispatch=False)`): deferred to
  its own change. Its verifier confirmed the duplication, but the dry-run path
  carries a virtual-admission counter the live path does not, which needs a
  separate decision.
- Renaming `Workspace` to Workshop; version or release changes; push, PR,
  merge.

## Acceptance criteria

1. `approval_sha` ignores human-authored markers; `approve_pr` has no `label`
   parameter; Execute's refusal text includes the SHA form; the Spec PR body
   no longer mentions watching the label.
2. Eight adapter argv blocks are replaced by one base helper; exact-argv tests
   pass unchanged.
3. No `.agentmachinist-preview-*` file is created by `provision_preview`; the
   two ownership attack tests still pass.
4. Execute contains no literal verification-report dict, no second "gates
   blocked" renderer, no dict scan of gate statuses, no `_ChangeSummary`, and
   one PR read before the completion comment.
5. `grep` for each removed Evidence key finds no writer in `src/`; the
   `ready_*` relationship is gone; Spec and Execute write one instruction
   vocabulary.
6. `execute.py` has no `_capture_workspace_custody`,
   `_resume_workspace_custody`, `_assert_workspace_metadata_custody`, or
   `git_custody` parameter; a real-`Workspace` resume test proves the token
   is asserted before the first Git call and that a tampered token refuses.
7. No `claim is None`, `hasattr(workspace, "cancel_check")`,
   `getattr(claim, ...)`, `getattr(github, "repo", ...)`, `bool | None`
   Phase option, or `_RECOVERY_MODES` remains in the Phases or dispatcher.
8. `spec.py` decides PR identity through `repository_custody` only; only the
   dispatcher constructs a Harness for a Task.
9. `machinist run --retry` is rejected by Click; `retry --run` on an Execute
   Task prints "implemented" and the correct Review-aware next action, and
   notifies PR_READY only when the PR was marked ready.
10. `tests/test_docs.py` passes with the documentation changes;
    `scripts/verify.sh` passes.

## Verification plan

Red/green per task with the narrowest test module, then
`uv run pytest -q` after each commit, then `bash scripts/verify.sh` before
the final handoff. Each task is one commit with a Conventional Commit subject.
