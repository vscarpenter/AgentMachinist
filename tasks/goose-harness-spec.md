# Goose Harness adapter

Status: design approved by Vinny on 2026-09-27. Branch `feat/goose-harness`.

## Context

Vinny asked for Pi, OpenCode, and Goose support. Pi and OpenCode already ship
as built-in adapters (`src/machinist/harness/pi.py`,
`src/machinist/harness/opencode.py`), so this change adds only Goose.

Goose 1.52.0 (`aaif-goose/goose`, formerly `block/goose`) runs headlessly
through `goose run`. Two facts shape the adapter:

- `goose run` has no sandbox or tool-allowlist flag. Its `developer` builtin
  extension provides file reading, shell, and file writing together, and the
  Spec prompt requires the Harness to explore the repository. Goose therefore
  joins OpenCode in the `advisory` read-only class.
- Goose selects its permission mode through `GOOSE_MODE` (environment or
  `config.yaml`), not argv. The `approve` and `smart_approve` modes wait for a
  confirmation that a headless Task Run cannot give.

## Scope

In scope:

- A built-in `goose` adapter, registry entry, and `HarnessName.GOOSE`.
- A `Harness.environment_overrides()` hook merged into the credential-reduced
  environment of every Harness Phase run.
- A `Harness.auto_select` flag that keeps an adapter out of first-run
  discovery. Goose sets it to false because pressly/goose, a Go database
  migration tool, also installs a `goose` executable.
- Reserved `extra_args` for Goose.
- Documentation and an `## Unreleased` changelog entry.

Out of scope:

- Managed GitHub Spec CI for Goose. Goose ships through an install script or
  Homebrew, not a pinnable npm or PyPI package, so the descriptor has no
  `ci_spec`. Workflow projection already refuses such adapters with a recovery
  message.
- An authentication probe. Goose has no non-interactive credential check;
  `goose doctor` checks the whole setup and may contact the provider.
- Structured token usage, a version bump, a release, and any paid live run.

## Contract

### Argv

Spec and Review:

```text
goose run -q --no-session --no-profile --with-builtin developer [--model <m>] [extra_args] --text <prompt>
```

- `-q` limits stdout to the model response, which the Spec and Review parsers
  read.
- `--no-profile` drops the operator's default extensions, and
  `--with-builtin developer` restores only the builtin that reads files.

Execute:

```text
goose run --no-session [--model <m>] [extra_args] --text <prompt>
```

Execute uses the operator's normal Goose profile, matching Pi and OpenCode.
Every profile disables session persistence.

### Environment

`Harness.environment_overrides() -> dict[str, str]` returns `{}` by default.
`Harness._run_with_heartbeat` applies it after `credential_reduced_environment`,
so it covers Spec, Execute, Review, and repair invocations. Goose returns
`{"GOOSE_MODE": "auto"}`, which replaces any inherited value for Machinist runs
only. Overrides must not carry secrets.

### Auto-selection

`Harness.auto_select: ClassVar[bool] = True`. When it is false:

- `local_setup._select_harness` skips the adapter when no `--harness` is given.
- `init_wizard._ask_harness` leaves it out of the "Available on PATH" list and
  the default choice. It stays a valid choice.

Explicit `--harness goose` and a saved `harness.name: goose` keep working.

### Descriptor and capabilities

- `name = "goose"`, `default_command = "goose"`.
- `HarnessCapabilities("advisory")`.
- Display name `Goose`, documentation `https://goose-docs.ai/docs/`, phases
  Spec, Execute, and Review, no `ci_spec`, `structured_usage` false.
- No `authentication_argv`, so `doctor` reports its existing warning that the
  Harness has no non-interactive auth probe.

### Reserved extra_args

Spec and Review reject: `run`, `--`, `-t`, `--text`, `-i`, `--instructions`,
`--recipe`, `--sub-recipe`, `--params`, `--explain`, `--render-recipe`, `-n`,
`--name`, `--session-id`, `--path`, `-s`, `--interactive`, `-r`, `--resume`,
`--no-session`, `--no-profile`, `--with-builtin`, `--with-extension`,
`--with-streamable-http-extension`, `--container`, `-q`, `--quiet`,
`--output-format`, and `--model`.

Execute rejects the same set except `-q`, `--quiet`, and `--output-format`,
because Execute does not parse stdout. `--provider` stays allowed in every
Phase because a Goose model must match its provider.

## Acceptance criteria

1. `get_harness(HarnessConfig(name="goose"))` returns the Goose adapter, and the
   parametrized built-in adapter tests pass for it.
2. Exact Spec, Review, and Execute argv match the contract, with and without
   `model` and `extra_args`.
3. A Goose Phase run passes `GOOSE_MODE=auto` to its subprocess even when the
   parent environment sets `GOOSE_MODE=approve`. Other adapters add nothing.
4. Reserved Goose arguments fail configuration validation for their Phase.
   `--provider anthropic` passes.
5. Local setup with only `goose` on PATH fails with the existing "No installed
   Harness" error. `--harness goose` saves Goose. The `init` wizard omits Goose
   from its detected list and default.
6. `doctor` warns that Goose has no non-interactive auth probe.
7. Managed GitHub Spec CI projection with Goose fails with the existing
   recovery message.
8. Documentation names Goose where it lists built-in adapters. HTML pages label
   it as an unreleased source-checkout addition, and `tests/test_docs.py`
   passes.
9. Each profile's argv plus `--help` parses on the installed Goose 1.52.0.

## Risks

- Goose's CLI changes independently. The `doctor` compatibility rows parse
  every profile with `--help` and catch removed flags.
- Read-only control is advisory. `GOOSE_MODE=auto` approves the `developer`
  shell and write tools in Spec and Review. The controller's dirty-tree and
  mutation postconditions stay authoritative, as they are for OpenCode.
- Forcing `GOOSE_MODE=auto` overrides an operator's `approve` preference for
  Machinist runs. The documentation says so.
- Prompts travel as argv, the same `ARG_MAX` exposure every adapter has.
