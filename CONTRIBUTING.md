# Contributing

Use Python 3.12 or newer and `uv`:

```sh
uv sync
uv run pytest
uv build
```

Changes to lifecycle behavior should start with a failing contract test. Keep
GitHub CLI construction behind `GitHubClient`, Git behavior behind `Workspace`,
and lifecycle eligibility/persistence behind the lifecycle and phase modules.

When config affects GitHub Actions, update the source template and projection
tests, then run:

```sh
uv run machinist sync-workflows
uv run machinist sync-workflows --check
```

Update user documentation and `CHANGELOG.md` for command, config, state, trust,
or compatibility changes. Do not include secrets, generated Task Run files, or
retained workspaces in commits.
