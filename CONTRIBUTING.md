# Contributing

Use Python 3.12 or newer and `uv`:

```sh
uv sync
bash scripts/verify.sh
```

`uv run pytest -o addopts=` is the verbose local equivalent of CI.

Changes to lifecycle behavior should start with a failing contract test. Keep
GitHub CLI construction behind `GitHubClient`, Git behavior behind `Workspace`,
and lifecycle eligibility/persistence behind the lifecycle and phase modules.
Keep local aggregation in `reporting.py` and all network projection in
`telemetry.py`; OTLP attributes are an allowlist, not a redaction pass.

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
