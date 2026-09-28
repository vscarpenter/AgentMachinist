# Harness support matrix

AgentMachinist 0.19.0 includes five built-in adapters, including Goose, and
discovers installed v1 plugins. The guided local workflow requires executable adapters for all three
Phases; the first-run selector accepts a full-pipeline adapter. Phase profiles
may subsequently select different supported adapters. “Spec and Review control”
describes adapter arguments; the controller also checks repository custody and
rejects changes from these read-only Phases.

This matrix describes the adapters in AgentMachinist 0.19.0. It covers the local
workflow, GitLab publication, and local readiness.
The CI column below describes the existing GitHub Actions Spec workflow, not
GitLab CI. See [Goose](#goose) for its advisory read-only profile.

| Config value | Executable | Spec and Review control | Implementation control | Managed Spec CI secret |
| --- | --- | --- | --- | --- |
| `claude-code` | `claude` | Plan permission mode, Read/Grep/Glob tools, no session persistence | Edit mode with gate commands allowlisted and no session persistence; prompt plus Git postconditions | `ANTHROPIC_API_KEY` |
| `codex` | `codex` | Read-only sandbox and ephemeral session | Workspace-write sandbox, approval prompts disabled, ephemeral session; prompt plus Git postconditions | `OPENAI_API_KEY` |
| `pi` | `pi` | Read/grep/find/ls allowlist; extensions, skills, prompt templates, and sessions disabled | Normal print-mode tools with session persistence disabled; prompt plus Git postconditions | `GEMINI_API_KEY` |
| `opencode` | `opencode` | Pure plan agent; treated as advisory | Normal run agent; prompt plus Git postconditions | `ANTHROPIC_API_KEY` by default |
| `goose` | `goose` | Quiet JSON run with only the developer builtin loaded and no session; treated as advisory | Normal profile with no session; prompt plus Git postconditions | None; managed Spec CI is unsupported |

### Goose

Goose's `developer` builtin reads files for Spec and Review, but it also writes
files and runs shell commands. `goose run` has no flag that limits it to
reading, so its read-only control is advisory. The controller rejects any
change a Spec or Review run makes, as it does for OpenCode. Spec and Review add
`--no-profile`, so your other Goose extensions stay unloaded there.

Spec and Review also add `--output-format json`. Goose's text output mixes its
tool transcript into the answer even with `-q`, so a saved Spec used to start
with shell commands and their output, and a Review report could fail to parse.
The adapter keeps only the text of the last assistant message and fails the run
when that message has none. Execute keeps text output, so its
`harness-report.txt` still shows every tool call.

Every Goose run sets `GOOSE_MODE=auto`. The `approve` and `smart_approve`
modes wait for a confirmation that a headless Task Run cannot give. This
overrides a `GOOSE_MODE` in your shell or Goose configuration for AgentMachinist
runs only.

First-run discovery never picks Goose from PATH, because pressly/goose, a Go
database migration tool, installs the same `goose` executable. Choose it with
`machinist start --harness goose` or `harness.name: goose`. Goose model names
depend on the provider, so pass the provider through `extra_args` when your
Goose configuration does not already set one:

```yaml
harness:
  name: goose
  extra_args: ["--provider", "anthropic"]
```

Goose ships through an install script or Homebrew rather than a pinnable
package, so it has no managed GitHub Spec CI profile. Use
`github.spec_source: local` with Goose.

Review is a separate durable Task Run even when it inherits the same adapter.
It receives the approved Spec, diff, verification Evidence, and Task context,
and runs under the adapter's read-only Review argv. Its version-1 findings are
advisory. Local Review is mandatory: invalid output, mutation, or a changed
candidate prevents integration/publication eligibility. In the GitHub issue
pipeline, `review.enabled` controls Review; when enabled, failure leaves the
PR draft. A changed successful Execute SHA permits another Review, while a
completed Review for the same candidate is not repeated.

## Verification feedback loop

When gates are configured and `verification.harness_may_run_gates` is true
(the default), the implementation prompt lists each gate command and asks the
harness to run required gates and iterate until they pass before finishing.
`codex`, `pi`, `opencode`, and `goose` execute modes already permit command
execution, so only the prompt changes for them. `claude-code`'s headless edit mode
denies commands, so the adapter additionally allowlists the configured gate
commands and variants with additional arguments
(`--allowedTools "Bash(<command>)" "Bash(<command>:*)"`). The controller's own
gate run afterwards stays authoritative.

### Bounded controller repair

Opt-in `verification.repair.max_attempts: 1` reuses the resolved Execute Harness for at
most one additional invocation after an eligible required Gate failure. It
defaults to `0`. The repair prompt includes the approved implementation
instructions and bounded, sanitized failure diagnostics treated as untrusted
data. The controller repeats all configured Gates before delivery.

`verification.repair.timeout_minutes` (default 10, range 1–240) limits the
additional Harness invocation and final controller Gates together. The resolved
Execute Harness timeout and individual Gate timeouts also remain in force. This
is independent of the Harness's own iterative Gate runs and does not relax
adapter permissions, Git custody, or the approved Spec. Interrupted or failed
paid repair work cannot be replayed on resume; see the
[eligibility and recovery contract](getting-started.md#bounded-verification-repair).

## Authentication

Runs use the Harness's existing provider authentication. Local setup checks
installed executables and Phase support; it does not run the provider's login
probe or validate model access. Use the checks below before your first Task.
Plain `doctor` checks the installed version, parses configured Spec and Execute
invocations, and checks Review when `review.enabled: true` for the GitHub
workflow. Optional `doctor --local` uses the
same probes with the local settings that `start` resolves, including mandatory
Review. These diagnostics use the adapter's read-only authentication probe when one is available;
plugins without a probe require manual verification. A successful probe
confirms configured credentials, not subscription quotas or access to every
possible model.

Current authentication entry points are:

| Harness | Check | Sign in or configure |
| --- | --- | --- |
| Claude Code | `claude auth status --json` | `claude auth login` |
| Codex | `codex login status` | `codex login` |
| OpenCode | `opencode auth list --pure` | `opencode auth login` |
| Pi | `pi auth check --model <model> --json --no-refresh` (or the default Google provider when no model is set) | Configure credentials for the selected provider or model, then rerun the check. |
| Goose | None; `doctor` warns that Goose has no non-interactive auth probe | `goose configure`, then verify one Goose run yourself |

These CLIs evolve independently. Confirm the command with the installed
harness's `--help` output when upgrading.

The GitHub Actions Spec template installs the selected adapter at the pinned
version declared in its descriptor and binds only the descriptor's secret.
`github.spec_secret_env` may change the repository secret name without storing
its value. A third-party adapter without `ci_spec` metadata cannot run managed
GitHub Spec CI; workflow projection fails with a recovery choice instead of
guessing. It can still support foreground local Tasks or the local GitHub
watcher if its declared Phases are sufficient. GitLab issue import/publication
uses `glab` independently and does not install a hosted Spec workflow.

## Credential environment

Harness subprocesses retain provider variables such as `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY` through an explicit allowlist. Other provider or plugin keys
are not automatically preserved: common secret-name suffixes and cloud
credential variables are filtered. AgentMachinist also removes common forge
tokens, Git askpass, and SSH-agent variables and disables terminal credential
prompting. This is credential reduction, not credential isolation; see the
[trust model](trust-model.md).

## Model and additional arguments

`harness.model` passes one model selection to the adapter. `harness.extra_args`
supplies additional argv entries. Spec, Execute, and Review inherit these base
settings unless their `harness.spec`, `harness.execute`, or `harness.review`
profile overrides them. Selecting a different adapter in a Phase resets
inherited `command`, `model`, and `extra_args`; specify that adapter's values
in its profile. An explicit `model: null` clears an inherited model, and
`extra_args: []` clears inherited additional arguments.

Spec and Review default to `harness.spec_timeout_minutes` (10 minutes, maximum
60); Execute defaults to `harness.timeout_minutes` (30 minutes, maximum 240).
Each Phase can set `timeout_minutes` in its own profile, subject to the same
Phase limit. Review inherits the base read-only timeout, not a timeout override
under `harness.spec`.

For the built-in
adapters, AgentMachinist rejects reserved sandbox, permission, model, session,
and tool flags, including duplicate forms that could override its controls.
Third-party adapters do not inherit that reserved-argument map and must validate
their own controls. Other additional arguments are appended to the adapter
command and may change behavior as harness CLIs
evolve, so keep `extra_args` empty unless you have reviewed the final command
and updated your threat assessment.

## Third-party adapter contract

Plugins are trusted local Python code. A distribution registers exactly one
`Harness` subclass per entry point:

```toml
[project.entry-points."agentmachinist.harnesses.v1"]
example = "example_harness:ExampleHarness"
```

The entry-point name and class `name` must match a lowercase 1–64 character
identifier. Built-in names are reserved. `HarnessDescriptor` declares contract
version 1, display name, HTTPS documentation, supported phases, whether token
usage is structured, and optional `HarnessCIProfile` install argv plus secret
name. Discovery isolates load failures; one broken plugin cannot hide healthy
adapters, and the unknown-adapter error lists both installed choices and failed
entry points.

Adapters splice `self._passthrough_argv()` (the operator's `harness.model` and
`harness.extra_args`) into both the read-only and the edit profile at their own
prompt-relative position instead of restating that block.

Adapters also have two optional class members. Set
`auto_select = False` when another common tool installs the same executable
name, so first-run discovery never guesses the adapter. Return non-secret
variables from `environment_overrides()` when the CLI reads a control only from
the environment. They replace inherited values after credential reduction. Adapter tests should
pin exact Spec, Execute, and Review argv; prove read-only controls for
Spec/Review; and install a fixture entry point from an isolated path. A plugin
that declares structured usage must record nonnegative integer aggregate
`input_tokens`, `output_tokens`, or `total_tokens` fields before
`machinist report` includes them. A recorded zero is known usage; an omitted or
invalid value remains unknown.

Aggregate reports
read both local and legacy history by default; `--source local` and
`--source legacy` select one. `usage_coverage` reports which attempts and token
fields were observed. Token totals cover only those known fields and do not
estimate cost. The printed local Task report and `machinist status T1 --json`
still provide individual foreground Evidence.

## Compatibility checks

Harness CLIs evolve independently. Before unattended GitHub watcher use after
an upgrade:

```sh
machinist doctor
```

The compatibility rows execute `--help` against the configured Spec and Execute
argv, plus Review when enabled, without starting a Harness Task. If an argument
changes, update the adapter, its exact argv test, this matrix, and the changelog together.
Plain `doctor` reads root `machinist.yaml` and checks GitHub setup when that
file exists; without it, plain `doctor` runs the local checks below. The
`machinist doctor --local` option reads saved local settings or previews
first-start discovery without saving it. It runs no model, forge, release-update probe, or Verification
Gate by default. `--run-gates` explicitly runs project commands in the
controller checkout, where they may write or download; passing does not prove
the isolated Workshop baseline. See [local readiness](local-workflow.md#optional-local-readiness).

For foreground Tasks, inspect the saved configuration via
`machinist config show --path .machinist/runs/local/config.yaml`, check the
selected Harness's authentication, and use `machinist rehearse --harness` only
when you intend to invoke its configured profiles in a disposable repository.
Plain `machinist rehearse` exercises the production local controller with a
fake Harness and no model cost.

Local orchestration does not select a local model. A configured adapter may use
a cloud provider. An offline setup needs a provider/adapter combination that
supports local inference, downloaded models and dependencies, and a separately
verified run with network access denied.
