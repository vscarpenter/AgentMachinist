# Spec → Approval → Execute Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the top recommendation and every Strong card of the 2026-09-03 adversarial simplification review of the Spec → Approval → Execute path.

**Architecture:** Each policy gets one owner: the Workshop owns custody invocation, the Verification Gate owns its Evidence projection, Evidence vocabulary equals what recovery reads, Gate 1 trusts only the managed workflow, and the CLI seam has one recovery entry and one renderer. Modules get deeper; none merge (ADR-0002 stands).

**Tech Stack:** Python 3.12, Click, pydantic, pytest (offline; `gh` and harness subprocesses are faked, git tests use real repos in `tmp_path`).

**Spec:** `tasks/spec.md` (approved 2026-09-03). Line-level evidence for every task lives in the review outputs under the session scratchpad `arch-review/lens-{A..F}.md` and `verify-{A..F}.md`; the finding ids below (C-1, E-1, …) index those files.

## Global Constraints

- Preserve CLAUDE.md invariants 1–8. Invariant 2 tightens (bot-only markers); invariant 7 wording changes to match code.
- ADR-0001 and ADR-0002 stand; no module merges.
- Persisted Task Run schema v1 stays readable; historical unknown keys stay readable.
- TDD: red before green for every behaviour change; pure deletions are covered by the existing suite plus a red test proving the surface is gone where a test is cheap.
- Conventional Commit subjects with scope; one commit per task; `Claude-Session:` trailer.
- Subagents do not edit `CHANGELOG.md`, `CLAUDE.md`, or `README.md`; they report the wording and the lead applies it in Task 11.
- Subagents do not commit; they report the exact file list.

---

### Task 1: Gate 1 trusts only the managed workflow (card 4: B-1, B-4)

**Files:**
- Modify: `src/machinist/github.py:283-313` (`approval_sha`, `approve_pr`)
- Modify: `src/machinist/cli.py:1433-1437` (`approve` caller of `approve_pr`)
- Modify: `docs/architecture.md:96-98`, `docs/trust-model.md` (marker-author sentence, if present)
- Test: `tests/test_github.py:587-663`, `tests/test_cli.py:2204, 2217-2219, 2253`

**Interfaces:**
- Produces: `GitHubClient.approval_sha(number) -> str | None` unchanged signature, trusts markers only when `author.login in {"github-actions", "github-actions[bot]"}`.
- Produces: `GitHubClient.approve_pr(number, *, head_sha)` (no `label`).

- [ ] **Step 1: Write the failing tests** in `tests/test_github.py` (adapt the fixtures already there at :587-652):

```python
def test_approval_sha_ignores_human_authored_marker(...):
    # comment authored by login "vinny", author_association "OWNER",
    # body "<!-- agentmachinist:approval sha=<head> -->"
    assert client.approval_sha(18) is None

def test_approval_sha_accepts_workflow_bot_marker(...):
    # login "github-actions[bot]", association "NONE"
    assert client.approval_sha(18) == head

def test_approve_pr_has_no_label_parameter():
    import inspect
    assert "label" not in inspect.signature(GitHubClient.approve_pr).parameters
```

- [ ] **Step 2: Run** `uv run pytest -q tests/test_github.py -k "approval_sha or approve_pr"`; expected: the human-author test FAILS (marker accepted today), the signature test FAILS.
- [ ] **Step 3: Implement** in `github.py`: delete `trusted_author` and the association set; keep the login check; remove `label` from `approve_pr`'s signature, the `del label` line, and the docstring sentence about third-party adapters. Update the existing test at `test_github.py:633` (human OWNER marker) to expect `None`; update the `approve_pr` doubles at `test_github.py:655-663`, `test_cli.py:2204, 2253` and the assertion at `test_cli.py:2217-2219` to the two-argument shape; update `cli.py:1433-1437`.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_github.py tests/test_cli.py tests/test_docs.py`; expected PASS.
- [ ] **Step 5: Docs** `docs/architecture.md:96-98` and any `docs/trust-model.md` sentence naming OWNER/MEMBER/COLLABORATOR as marker authors → "the managed approve workflow, authored as `github-actions[bot]`". Report the CLAUDE.md invariant-2 wording and CHANGELOG entry to the lead.
- [ ] **Step 6: Commit** `fix(github): trust only workflow-authored approval markers`

### Task 2: One Harness pass-through helper (card 10: E-3)

**Files:**
- Modify: `src/machinist/harness/base.py:88-99`; `src/machinist/harness/claude_code.py:57-60, 81-84`; `codex.py:35-38, 57-60`; `pi.py:67-70, 76-79`; `opencode.py:46-49, 55-58`
- Modify: `docs/harnesses.md` (pass-through convention paragraph)
- Test: `tests/test_harness.py` (exact-argv tests must pass unchanged)

**Interfaces:**
- Produces: `Harness._passthrough_argv(self) -> list[str]` returning `["--model", model]` when `self.config.model` is set, followed by `list(self.config.extra_args)`.

- [ ] **Step 1: Failing test** in `tests/test_harness.py`:

```python
def test_passthrough_argv_threads_model_then_extra_args():
    harness = ClaudeCodeHarness(HarnessConfig(name="claude-code", model="opus", extra_args=["--x"]))
    assert harness._passthrough_argv() == ["--model", "opus", "--x"]

def test_passthrough_argv_is_empty_without_model_or_extra_args():
    harness = ClaudeCodeHarness(HarnessConfig(name="claude-code"))
    assert harness._passthrough_argv() == []
```
(Use the constructor shape the existing tests in that file use.)

- [ ] **Step 2: Run** `uv run pytest -q tests/test_harness.py -k passthrough`; expected FAIL with AttributeError.
- [ ] **Step 3: Implement** the helper on `base.Harness`; replace each of the eight blocks with `argv.extend(self._passthrough_argv())` at exactly the same position (claude-code: after `-p <prompt>`; codex/opencode/pi: before the positional prompt). Leave `pi.py:33-37` (`authentication_argv`) alone.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_harness.py tests/test_harness_plugins.py`; expected PASS with the exact-argv tests untouched.
- [ ] **Step 5: Docs** `docs/harnesses.md`: replace the "add to both spec_argv and implement_argv on all four adapters" convention with "extend `_passthrough_argv()` on the base class". Report the CLAUDE.md sentence to change to the lead.
- [ ] **Step 6: Commit** `refactor(harness): thread model and extra_args through one base helper`

### Task 3: Delete the preview-ownership sidecar (card 8: E-5)

**Files:**
- Modify: `src/machinist/workspace.py:104-111` (`_PreviewClaim` fields), `280-317` (`cleanup_preview`), `319-375` (`_reserve_preview`), `393-456` (`_assert_preview_claim`, `_remove_preview_sidecar`, `_unlink_if_identity`)
- Test: `tests/test_workspace.py:124-153` (must stay green), plus new tests

**Interfaces:**
- Consumes: `Workspace.provision_preview(task_name, ...)` / `cleanup_preview(path)` signatures unchanged.

- [ ] **Step 1: Failing tests** in `tests/test_workspace.py`:

```python
def test_provision_preview_creates_no_sidecar_file(workspace, ...):
    path = workspace.provision_preview("preview-spec-1-abc")
    hidden = [p for p in path.parent.iterdir() if p.name.startswith(".agentmachinist-preview")]
    assert hidden == []

def test_provision_preview_refuses_existing_target(workspace, ...):
    target = <root>/"<repo>-preview-spec-1-abc"; target.mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="already exists"):
        workspace.provision_preview("preview-spec-1-abc")
```
(Match the error text `_reserve_preview` raises today at :339-342 so the message is preserved.)

- [ ] **Step 2: Run** `uv run pytest -q tests/test_workspace.py -k preview`; expected: the sidecar test FAILS (file exists today); the existing-target test passes or fails depending on current message — keep whichever assertion matches today's message.
- [ ] **Step 3: Implement**: `_reserve_preview` becomes `target.mkdir(mode=0o700)` inside `try/except FileExistsError → WorkspaceError(<same message>)`, then `os.stat` for dev/inode into `_PreviewClaim`; delete the nonce/sidecar fields, `_assert_preview_claim`, `_remove_preview_sidecar`, `_unlink_if_identity`, and the sidecar re-read in `cleanup_preview`. Keep the :283-287 and :293-303 refusals.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_workspace.py`; expected PASS including :124-153.
- [ ] **Step 5: Commit** `refactor(workspace): drop the preview sidecar; the inode claim owns preview cleanup`

### Task 4: Verification Gate owns its Evidence and messages (card 5: E-2, C-7, C-8, C-11)

**Files:**
- Modify: `src/machinist/phases/execute.py:65-79, 631-636, 988-999, 1051-1099, 1126-1132`; `src/machinist/evidence.py:148-149`
- Test: `tests/test_execute_phase.py` (around 1213, 1235 for the resume blocker; 1602 for the delivery read)

- [ ] **Step 1: Failing tests**: (a) on a blocked gate, the Task Run error text equals `str(VerificationFailed)` from the engine (today Execute re-renders with a different separator/tail); (b) `_ChangeSummary` no longer exists (`hasattr(execute_module, "_ChangeSummary") is False`); (c) a fake GitHub counts `pr_for_branch` calls during delivery and expects three (pre-comment, pre-ready, post-ready), not four; (d) after a gate failure the persisted Evidence has `verification_report` but no `resume_forbidden_reason`, and `--resume` is still refused with the same message.
- [ ] **Step 2: Run** them; expected FAIL for (a), (b), (c), (d).
- [ ] **Step 3: Implement**: `VerificationReport(gates=(), duration_seconds=0.0).as_dict()`; `str(exc)`; classify via `exc.report.failures` and `GateStatus`; delete `_verification_failure_message`, `_ChangeSummary` (return the dict), the first `pr_for_branch` read at :1126-1132, and the `resume_forbidden_reason` write; compute `_verification_resume_blocker(previous.verification_report or {})` at :631; drop `evidence.py:148-149`.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_execute_phase.py tests/test_evidence.py tests/test_verification.py`; PASS.
- [ ] **Step 5: Commit** `refactor(execute): consume the Verification Gate's own projection and message`

### Task 5: Stop writing Evidence nobody reads (card 3: D-1, C-9, A-3)

**Files:**
- Modify: `src/machinist/phases/execute.py` (writers listed in verify-D §D-1 and lens-C §C-9), `src/machinist/phases/spec.py:161-172, 269-271`, `src/machinist/phases/review.py:270`, `src/machinist/evidence.py:23-52, 301`, `src/machinist/config.py` (`InstructionsConfig`)
- Test: `tests/test_execute_phase.py:690, 739, 1233, 1479, 1696, 1727, 1744`; `tests/test_spec_phase.py:723, 772, 897-901`; `tests/test_execute_phase.py:605-608`; `tests/test_evidence.py`

- [ ] **Step 1: Failing tests**: (a) after a successful Execute the persisted Evidence contains none of the 13 keys (assert `set(record.evidence) & REMOVED == set()`); (b) `TaskEvidence` has no attribute for `ready_intended_sha`; (c) `InstructionsConfig.evidence(sources)` returns `{"instructions_sha256": ..., "instruction_paths": [...], "instruction_append": bool}` and both Spec and Execute persist exactly that mapping; (d) legacy record with `ready_observed_sha` still loads.
- [ ] **Step 2: Run**; expected FAIL.
- [ ] **Step 3: Implement**: remove the writers, the `_PHASE_FIELDS`/`_SHA_FIELDS` entries, the `ready_*` relationship, `_reset_fresh_execution_evidence` entries for the removed keys; add `InstructionsConfig.evidence`; use it in both Phases. Keep `progress_detail`, `last_progress_at`, the pre-gate `change_summary` checkpoint, and every `push_*`/`pushed_sha`/`implementation_sha` key. Delete the three `workspace_head` test seeds.
- [ ] **Step 4: Run** `uv run pytest -q`; PASS.
- [ ] **Step 5: Commit** `refactor(evidence): write only the checkpoints recovery and reports read`

### Task 6: Collapse Execute's custody layer into the Workshop (card 1: C-1, E-1, C-4, C-10, E-7)

**Files:**
- Modify: `src/machinist/workspace.py:141-235` (`provision`), `641-671` (`resume`), `1117-1201` (`assert_git_custody`), `1337-1350`, `1756-1773` (`_assert_owned_checkout`)
- Modify: `src/machinist/phases/execute.py:555-597, 685-843, 729-783, 445-473, 565-577`
- Test: `tests/test_workspace.py` (new real-Workspace resume tests), `tests/test_execute_phase.py:820, 840, 1033, 1061`

**Interfaces:**
- Produces: `Workspace.git_custody(path) -> dict | None` (the token captured by `provision`, keyed by resolved path).
- Produces: `Workspace.resume(path, *, branch, expected_sha, git_custody: dict | None)`; `None` raises `WorkspaceError("... start a fresh retry")` before any Git subprocess.
- Produces in execute.py: `_assert_head(workspace, path, branch, *, head_expected, remote_expected, actor)` replacing the two asserts.

- [ ] **Step 1: Failing tests** in `tests/test_workspace.py` using a real repo in `tmp_path`:

```python
def test_resume_asserts_custody_before_any_git_subprocess(...):
    path = ws.provision(...); token = ws.git_custody(path)
    # tamper: append a sensitive key to the workshop-owned config (or hooks/) that the token watches
    calls = []  # inject a runner that records argv
    with pytest.raises(WorkspaceError, match="custody"):
        ws2 = Workspace(...runner=recording_runner); ws2.resume(path, branch=..., expected_sha=..., git_custody=token)
    assert calls == []          # no git subprocess ran

def test_resume_without_token_refuses_fresh_retry(...):
    with pytest.raises(WorkspaceError, match="fresh retry"):
        ws.resume(path, branch=..., expected_sha=..., git_custody=None)
```
- [ ] **Step 2: Run**; expected FAIL (`git_custody` accessor missing / `resume` rejects kwarg).
- [ ] **Step 3: Implement** `Workspace` side; then delete the Execute helpers and parameter threading; merge the two asserts; `try/except ExecutePhaseError` stays as-is (no `finally`); one `provision(..., attempt=attempt)` form; `_assert_owned_checkout` uses `_resolve_git_layout_raw` common dirs instead of `git rev-parse` (keep the strategy branch). Update `FakeWorkspace.resume` signature and the two real-Workspace Execute tests (1033 unchanged, 1061 expects `WorkspaceError`).
- [ ] **Step 4: Run** `uv run pytest -q`; PASS.
- [ ] **Step 5: Commit** `refactor(execute): let the Workshop own custody invocation on provision and resume`

### Task 7: Delete the test-double-shaped Phase interface (card 2: C-2, D-4, A-4, D-5, B-5, A-6, C-6)

**Files:**
- Modify: `src/machinist/phases/execute.py` (`claim` required; `resume: bool`; drop `_RECOVERY_MODES`, `hasattr(workspace,"cancel_check")`, `getattr(claim, ...)`, `getattr(github, "repo", None)`, the tempdir branch, `_checkpoint`), `src/machinist/phases/spec.py:73-78, 84-267, 246, 301-304, 355-357`, `src/machinist/phases/review.py:255-257, 295`, `src/machinist/phases/progress.py:7-22`, `src/machinist/dispatch.py:101-161, 209-221`, `src/machinist/cli.py:1507, 1965, 2079` (`recovery=` → `resume=`)
- Test: `tests/test_execute_phase.py` (FakeClaim gains `log_directory`, `progress`; 36 calls gain `claim=`), `tests/test_spec_phase.py` (FakeClaim gains `attempt`, `log_path`, `log_directory`, `progress`; FakeGitHub gains `mark_draft`, `repo`; 19 calls), `tests/test_review_phase.py` (2 calls), `tests/test_cli.py:1136, 2279, 2407` (closed-signature fakes gain `force`/`feedback`), `tests/test_dispatch.py:142-169`

- [ ] **Step 1: Failing tests**: (a) `run_execute_phase(...)` without `claim` raises `TypeError`; (b) `inspect.signature(run_execute_phase).parameters["resume"].annotation is bool` and no `recovery` parameter; (c) `TaskDispatcher.run_spec(issue, revise=False)` forwards `revise=False` to the runner (today omitted); (d) a Workspace passed to `TaskDispatcher` gets `cancel_check` set (assert on the real-construction path).
- [ ] **Step 2: Run**; FAIL.
- [ ] **Step 3: Implement** per the spec §7; the `hasattr(claim, "progress")` in `progress.py` goes with it. Give every fake the members.
- [ ] **Step 4: Run** `uv run pytest -q`; PASS.
- [ ] **Step 5: Commit** `refactor(phases): require the Claim and plain option types the dispatcher always sends`

### Task 8: Spec Phase custody handoff (card 9: A-1, A-2, A-7/F-4, A-8, A-9)

**Files:**
- Modify: `src/machinist/repository_custody.py` (add `verify_branch_pr`), `src/machinist/phases/spec.py:176-184, 222-226, 251-281, 284-317, 320-354, 450-470`, `src/machinist/cli.py:138, 144-153, 729-737, 1200-1208, 1586-1601, 1639-1650`, `src/machinist/dispatch.py` (add `preview_spec`), `src/machinist/github.py` (label metadata constant)
- Test: `tests/test_repository_custody.py`, `tests/test_spec_phase.py:181-184 area, 310, 323-362, 584-603`, `tests/test_cli.py:1908-1928`, `tests/test_dispatch.py`

**Interfaces:**
- Produces: `repository_custody.verify_branch_pr(pr, *, branch, base) -> None` raising `RepositoryCustodyError` on branch, base, cross-repository, or head-repository mismatch; state taken from `pr`.
- Produces: `TaskDispatcher.preview_spec(issue) -> str`.
- Produces: `github.APPROVED_LABEL_COLOR = "0e8a16"`, `APPROVED_LABEL_DESCRIPTION = "Machinist: spec approved for implementation"`.

- [ ] **Step 1: Failing tests**: custody tests for branch and base mismatch (`match="unexpected branch"` / `"targets base"` — the messages spec.py uses today); a dispatcher test that `preview_spec` builds Harness and Workspace through `_harness`/`_workspace` and sets `cancel_check` on both; a spec test that a dirty tree after the Harness is reported once with the "read-only" message on both run and preview; a test that `run_spec_phase` returns an object with `head_sha`.
- [ ] **Step 2: Run**; FAIL.
- [ ] **Step 3: Implement** per spec §8. Keep the reopen path: build the expectation from the observed PR's state.
- [ ] **Step 4: Run** `uv run pytest -q`; PASS.
- [ ] **Step 5: Commit** `refactor(spec): decide PR identity through repository custody; preview through the dispatcher`

### Task 9: Transitions and watch results (card 11: D-2, D-3)

**Files:**
- Modify: `src/machinist/transitions.py:51-60, 188-256`, `src/machinist/phases/watch.py:40-80`, `src/machinist/cli.py:1855-1892`, `src/machinist/observability.py:224-242`
- Test: `tests/test_transitions.py` (add `describe_run` tests), `tests/test_watch_phase.py:92, 110, 177, 467-472`, `tests/test_cli.py:717, 832, 898, 2948`

- [ ] **Step 1: Failing tests**: `RunDisposition` has exactly the fields `display`, `next_action`; `WatchResult` is not a `Sequence` and `WatchResult() == []` is False; the CLI poll-error branch reports zero failures without `getattr`.
- [ ] **Step 2: Run**; FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q`; PASS.
- [ ] **Step 5: Commit** `refactor(transitions,watch): drop unread disposition fields and the list emulation`

### Task 10: One recovery entry and one renderer at the CLI seam (card 6: F-1, F-2, F-5, F-6, F-7, F-9)

**Files:**
- Modify: `src/machinist/cli.py:1493-1539, 1576-1660, 1697-1900, 1907-1994, 2058-2102`, `src/machinist/phases/watch.py:117-181` (`watch_once(dispatch=False)`, deferral reasons), `src/machinist/github.py:46-48` (`DraftPR.head_sha: str | None = None`), `src/machinist/phases/spec.py` (set it)
- Modify docs (lead): `README.md`, `docs/getting-started.md`, `docs/first-run-guide.html:1513`, `CLAUDE.md:48, 56`
- Test: `tests/test_cli.py:2446` (delete), new `retry --run` renderer test, `tests/test_watch_phase.py:317-383`

- [ ] **Step 1: Failing tests**: (a) `retry 42 --phase execute --run` with `review.enabled: true` prints "implemented", prints the `machinist review 42` next action, and does NOT notify PR_READY (reproduces the bug); (b) `run 42 --retry` exits with a Click usage error; (c) `amend` on a ready PR still refuses without fresh Approval (guards `force=True`); (d) `watch --dry-run` and live `watch` produce identical deferral text for the same state.
- [ ] **Step 2: Run**; (a), (b), (d) FAIL. **Step 3: Implement** per spec §9. **Step 4: Run** `uv run pytest -q tests/test_cli.py tests/test_watch_phase.py tests/test_docs.py`; PASS.
- [ ] **Step 5: Commit** `refactor(cli): one recovery entry and one Phase outcome renderer`

### Task 11: Documentation, changelog, and the final gate

**Files:**
- Modify: `CHANGELOG.md` (new `## Unreleased`), `CLAUDE.md` (invariants 2 and 7; `run` flags in the module map; harness pass-through sentence; Current state), `README.md`, `docs/getting-started.md`, `docs/first-run-guide.html`, `docs/architecture.md`, `tasks/todo.md`

- [ ] **Step 1:** `uv run pytest -q tests/test_docs.py` after each doc edit.
- [ ] **Step 2:** `bash scripts/verify.sh` → exit 0.
- [ ] **Step 3:** `tasks/todo.md` "Resuming From Here"; commit `docs: describe the simplified Spec → Execute path`.
