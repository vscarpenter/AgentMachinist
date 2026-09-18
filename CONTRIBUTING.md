# Contributing

Use Python 3.12 or newer and `uv`:

```sh
uv sync
bash scripts/verify.sh
```

`uv run pytest -o addopts=` runs the test suite with verbose progress. The full
script also checks the lockfile, managed workflows, formatting, lint, types,
coverage, and unchanged source before building and smoke-testing both package
distributions. CI additionally tests multiple Python versions and minimum
dependencies.

This repository's `machinist.yaml` uses the script's ordered `workflows`,
`format`, `lint`, `types`, and `coverage` subcommands as required check-only
Verification Gates. Packaging remains part of the full script, outside those
Task Gates. Existing saved local settings under `.machinist/runs/local/` do not
automatically adopt root configuration changes.

Changes to lifecycle behavior should start with a failing contract test. Keep
legacy GitHub CLI construction behind `GitHubClient`, optional forge operations
behind `forge.py`/`gitlab.py`, and Git behavior behind `Workspace`/`LocalWorkspace`,
known Evidence interpretation in `evidence.py`, Task Run construction in
`dispatch.py`, journal discovery in `lifecycle.py`, transition decisions in
`transitions.py`, repository/PR checks in `repository_custody.py`, and Gate
execution in `verification.py`. Optional bounded Execute repair belongs in
`repair.py`; Phase code retains custody and change-limit checks around every
process. Repair defaults off, consumes its single extra attempt durably, and
never replaces explicit retry of a failed Task Run.

Keep aggregate reporting in `reporting.py`, preserving separate local and legacy
Task identities and treating missing usage as unknown. All telemetry network
export belongs in `telemetry.py`; OTLP attributes are an allowlist, not a
redaction pass. Local/all reports require an explicit export endpoint; root
telemetry configuration applies only to `report --source legacy`.

When config affects GitHub Actions, update the source template and projection
tests, then run:

```sh
uv run machinist sync-workflows
uv run machinist sync-workflows --check
```

Update user documentation and `CHANGELOG.md` for command, config, state, trust,
or compatibility changes. Do not include secrets, generated Task Run files, or
retained workspaces in commits.

Third-party Harnesses register one subclass in the
`agentmachinist.harnesses.v1` entry-point group. The entry-point name must match
the adapter name and built-in names are reserved. Declare supported phases,
structured-usage support, documentation, and optional hosted-Spec CI metadata
in `HarnessDescriptor`; add isolated discovery-failure and packaging tests.
