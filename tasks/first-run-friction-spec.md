# First-run friction: specification

Approved on 2026-09-26 after walking a fresh Python repository through the
local workflow with an injected fake Harness. Four defects appear inside the
first ten minutes of use. This work fixes them and adds a copyable example
project. Approval, Git custody, Review, and integration semantics do not change.

## 1. Baseline Verification failures are reported truthfully

- When the baseline Gate fails, the Spec Task Run error is the Verification
  message. The Workshop read-only assertion never replaces it. Controller-owned
  custody fields are still checked.
- When the baseline Gate passes but leaves the Workshop changed, the error names
  the changed paths and tells the user to commit generated files such as
  lockfiles or ignore them. Evidence records `baseline_workshop_changes`.
- A Verification failure message shows stderr and stdout evidence when both
  exist, each bounded, so a runner that prints failures on stdout is visible.
- `status` shows `Error:` and `Logs:` lines for a failed Phase. JSON output
  carries `error` and `log_dir`.
- A Spec failure caused by the baseline reports state `baseline failed`. The
  rendered status explains the two recoveries: fix the verification command or
  its dependencies and retry, or commit a baseline change and start a new Task.
  The next action remains the retry command.

## 2. Detection prefers a command that exists

- `detect_test_command` takes an injectable `which`. For a pytest project it
  returns `uv run pytest` when `uv` is on PATH, otherwise `python3 -m pytest`
  when `python3` is on PATH, otherwise `python -m pytest`. A lockfile no longer
  decides the runner.

## 3. A failed start saves nothing, and flags win until the first Task

- `start` checks the clean tree and checked-out branch before it saves local
  configuration. A precondition failure leaves no local configuration behind.
- The clean-tree error lists up to five pending paths.
- With saved local configuration and no recorded Task, `--harness` and
  `--test-cmd` replace the saved values. With at least one Task, the existing
  conflict error stays.

## 4. Commands route to the local workflow when `machinist.yaml` is absent

- `doctor` without `--local` runs local readiness when `machinist.yaml` is
  absent and says so in one line.
- `status` with neither configuration prints the "No local Tasks" guidance and
  exits 0.
- `config show`, `config validate`, and `config set` without `--path` use
  `machinist.yaml` when present, otherwise the saved local configuration when
  present, otherwise `machinist.yaml`.
- `clean` loads local configuration when `machinist.yaml` is absent. It lists
  and removes local Task Workshops alongside issue Workshops, and `--task T1`
  selects one local Task. Active local Claims refuse removal.
- `runs` ends with a pointer to `machinist status` when local Tasks exist.

## 5. Example project

- `examples/first-task/` is a tiny Python project with a committed `uv.lock`,
  pytest configuration, and a README that explains how to copy it into a fresh
  repository and run one Task.
- The short first-Task guide links to it as an optional throwaway starting
  point.

## Out of scope

Abbreviated Approval SHAs, help regrouping, README restructuring, terminal
progress lines, and wording changes for the GitHub issue workflow.
