# Spec: watch: send a macOS notification when a dispatch fails (#4)

## Summary

Add a best-effort macOS notifier (`osascript -e 'display notification …'`) and fire it whenever a `machinist watch` dispatch produces an error event, so an away-from-keyboard user learns that a spec or execute run failed. The notifier is a new injectable module following the existing `Runner = Callable[..., subprocess.CompletedProcess]` pattern used by `GitHubClient` and `Harness`; `watch_once` gains an optional `notify` callback so the dispatch core stays testable without any real `osascript`.

## Requirements

1. **New notifier function.** A new module `src/machinist/notify.py` exposes `notify(title: str, message: str, runner: Runner = subprocess.run) -> None`, which invokes `["osascript", "-e", 'display notification "<message>" with title "<title>"']` with `capture_output=True`, `text=True`, and a short timeout (5 seconds) so a hung `osascript` can never stall the daemon.
2. **Best-effort, never raises.** `notify` swallows `OSError` (which includes `FileNotFoundError` when `osascript` is absent on non-macOS) and `subprocess.SubprocessError` (which includes `TimeoutExpired`), and ignores a non-zero exit code. It never raises, prints, or logs — the stdout event stream stays exactly as today. This deliberate swallow is required by the issue ("best-effort only") and is documented in the module docstring.
3. **Safe message rendering.** Before embedding text in the AppleScript literal, `notify` collapses all whitespace (including newlines, which are illegal inside a one-line AppleScript string) to single spaces, truncates the message to 200 characters (the issue asks for "a short reason"; macOS truncates further anyway), and escapes `\` and `"`.
4. **Watch core hook.** `watch_once` in `src/machinist/phases/watch.py` gains a keyword-only parameter `notify: Callable[[str], None] | None = None`. In `_dispatch` (currently `watch.py:43-50`), when the action raises, the failure message `f"{phase} for issue #{issue_number} failed: {exc}"` — which already carries the issue number and reason — is both appended to events as `f"error: {message}"` (unchanged string) and passed to `notify` when one is provided. Successful dispatches never call `notify`. The default of `None` preserves current behavior for all existing callers and tests.
5. **CLI wiring.** The `watch` command in `src/machinist/cli.py` (currently `cli.py:118-168`) passes `notify=lambda message: notify("machinist watch", message)` to `watch_once`, in both daemon and `--once` modes. Interpretation: `--once` is documented as the cron-friendly pass (README line 97), and cron implies away-from-keyboard, so it notifies too. Only dispatch errors notify — the poll-level `poll error:` path stays stdout-only, per the issue's "dispatch produces an error event" wording.
6. **Tests use an injected runner only.** All new tests drive `notify` through a `FakeRunner` (the pattern at `tests/test_harness.py:13-24`) or a recording callback; no test executes real `osascript`. The full suite stays green under `uv run pytest`.

## Proposed approach

**New: `src/machinist/notify.py`** (~30 lines). Mirrors the shape of `src/machinist/github.py`: a module-level `Runner` type alias, then the function. Internals:

- `_applescript_string(text)` — collapse whitespace via `" ".join(text.split())`, escape `\` then `"`, wrap in double quotes.
- `notify(title, message, runner=subprocess.run)` — truncate the sanitized message to 200 chars, build the argv, call `runner(...)` inside `try/except (OSError, subprocess.SubprocessError): pass`. Return value ignored (non-zero exit is a silent skip).

**Changed: `src/machinist/phases/watch.py`.** Add `notify: Callable[[str], None] | None = None` to `watch_once`'s keyword-only parameters and thread it into both `_dispatch` calls. `_dispatch` builds the failure message once, appends the existing `error: …` event string verbatim, then calls `notify(message)` if set. No other logic changes; `WatchState` and the never-redispatch behavior are untouched.

**Changed: `src/machinist/cli.py`.** Import `notify` from `machinist.notify`; in the `watch` command, pass `notify=lambda message: notify("machinist watch", message)` to the `watch_once` call at `cli.py:151-154`. The `spec`, `run`, and `status` commands are foreground/interactive and get no notifications.

The pieces fit the codebase's existing dependency-injection seams: `notify` receives its subprocess runner the same way `Harness.__init__` and `GitHubClient.__init__` do, and `watch_once` receives the notifier the same way it already receives `run_spec`/`run_execute`, keeping the dispatch core free of subprocess concerns.

## Testing plan

TDD order: write each test, confirm it fails for the right reason, then implement. Run with `uv run pytest` (the repo's configured gate; `addopts = "-q"` in `pyproject.toml`).

**New: `tests/test_notify.py`** — defines a local `FakeRunner` copied from `tests/test_harness.py:13-24` (this codebase duplicates small fakes per test file, e.g. `FakeGitHub` in `tests/test_watch_phase.py:19-28`):

1. `test_notify_runs_osascript_display_notification` — `FakeRunner(("", 0, ""))`; assert the recorded argv is exactly `["osascript", "-e", 'display notification "spec for issue #7 failed: boom" with title "machinist watch"']` and kwargs include `timeout=5`. *(Req 1)*
2. `test_notify_swallows_missing_osascript` — `FakeRunner(FileNotFoundError("osascript"))`; the call returns without raising. *(Req 2, silent-skip-on-failure)*
3. `test_notify_swallows_nonzero_exit_and_timeout` — `FakeRunner(("", 1, "AppleScript error"))` and `FakeRunner(subprocess.TimeoutExpired(cmd=["osascript"], timeout=5))`; neither raises. *(Req 2)*
4. `test_notify_escapes_quotes_and_flattens_newlines` — message containing `"` and `\n`; assert the argv contains the escaped, single-line form. *(Req 3)*
5. `test_notify_truncates_long_messages` — 1000-char message; assert the embedded string is ≤ 200 chars. *(Req 3)*

**Extended: `tests/test_watch_phase.py`** — reuses the existing `Dispatcher` and `FakeGitHub` fakes and the `run(...)` helper (extended to accept `notify`):

6. `test_dispatch_failure_notifies_with_issue_and_reason` — `Dispatcher(error=RuntimeError("boom"))`, `notify=notifications.append`; assert one notification containing `#7` and `boom`, and that the stdout event list is unchanged from today's format. *(Reqs 4, 5 — notify-on-error)*
7. `test_successful_dispatch_does_not_notify` — successful `Dispatcher`, recording `notify`; assert `notifications == []` while a success event is still emitted. *(Req 4 — no-notify-on-success)*
8. Existing tests (`test_dispatch_failure_becomes_event_and_daemon_survives`, etc.) pass unmodified, proving the `notify=None` default. *(Req 4)*

**Extended: `tests/test_cli.py`** — follows the monkeypatch style of `test_watch_once_prints_dispatch_events` (`tests/test_cli.py:117-132`):

9. `test_watch_once_wires_notifier_with_watch_title` — monkeypatch `machinist.cli.notify` with a recorder and `machinist.cli.watch_once` with a fake that invokes its `notify` kwarg with a failure message; assert the recorder saw title `"machinist watch"` and the message. *(Req 5)*

## Out of scope

- Notifications for the interactive `machinist spec`, `machinist run`, and `machinist status` commands.
- Notifying on poll-level errors (the `poll error: …` branch in `cli.py`) or on successful dispatches.
- Non-macOS notification backends (`notify-send`, `terminal-notifier`), notification sounds, or click actions.
- A `machinist.yaml` toggle to disable notifications — the feature is silent-by-design wherever `osascript` is unavailable, so no config surface is added until someone asks for one.
- Any change to event string formats, `WatchState`, or the never-redispatch policy.

## Open questions

None.
