# Resume-push fold and flags-only approve — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Implement cards 7 and 14 of the 2026-09-03 review in one PR.
**Spec:** `tasks/spec.md`. **Tech:** Python 3.12, pytest (offline).

## Global Constraints
- Invariants 2, 4, 7 preserved (see spec). TDD: red before green. One commit per card.

### Task 1: Card 7 — one push step, remote-aware reconciliation
**Files:** `src/machinist/phases/execute.py` (`_reconciled_push`, the resume-stage fork, `_retry_intended_push`, `_assert_head`); `tests/test_execute_phase.py` (`FakeWorkspace.repo_root`, `test_stale_pr_read_does_not_erase_unobserved_push_intent` → reconciles, new tests).
- [ ] Red: lagging PR head + remote at the intended SHA delivers without provision/harness; `_retry_intended_push` absent; head-mismatch message has no "approve the current".
- [ ] Green per spec §1–3. Run `uv run pytest -q tests/test_execute_phase.py`, then the full suite.
- [ ] Commit `refactor(execute): reconcile from the remote branch and share one push step`.

### Task 2: Card 14 — `approve` takes exactly one flag
**Files:** `src/machinist/cli.py` (`approve`); `tests/test_cli.py` (two approve tests); `README.md:108-109`, `docs/getting-started.md:221-223`, `CLAUDE.md:55-56`.
- [ ] Red: `approve 42` exits 2; both flags exit 2; `--pr 42` and `--issue 42` approve the right PR.
- [ ] Green per spec §4; docs; `uv run pytest -q tests/test_cli.py tests/test_docs.py`.
- [ ] Commit `refactor(cli): approve by --issue or --pr only`.

### Task 3: Changelog and gate
- [ ] `CHANGELOG.md` Unreleased; `bash scripts/verify.sh`; commit `docs: changelog for the resume-push fold and flags-only approve`.
