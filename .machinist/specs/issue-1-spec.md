# Spec: Create a friendly user-guide for AgentMachinist (#1)

## Summary

Write a beginner-friendly "Getting Started with AgentMachinist" guide as a new `docs/getting-started.md`, walking a newcomer from zero to their first harness-generated spec PR, and link it from `README.md`. The guide documents only what actually ships in v0.1 (`machinist init` and `machinist spec <n>`), clearly marks `watch`/`run`/`status` as upcoming, and is backed by drift tests so its commands and config examples can never silently diverge from the code.

## Requirements

The issue asks for "a Getting started with AgentMachinist guide that will allow anyone to be able to leverage this build system." Interpretation chosen: a single in-repo Markdown guide (no docs-site generator), aimed at a developer who knows git and GitHub but has never seen AgentMachinist, documenting the current v0.1 behavior truthfully rather than the full three-phase vision as if it were done.

1. A guide exists at `docs/getting-started.md` and covers, in order: what AgentMachinist is (the three-phase issue → spec → approve → execute pipeline, and that the agent never merges), prerequisites, installation, repository setup with `machinist init`, running a first task with `machinist spec <n>`, reviewing and approving via the `machinist:approved` label or `/machinist-execute` comment, local vs. CI spec generation, a full `machinist.yaml` configuration reference, harness selection, troubleshooting, and current v0.1 limits.
2. Every `machinist <subcommand>` the guide mentions is a real command registered on the Click group in `src/machinist/cli.py` (`init`, `spec`, `watch`, `run`, `status`), and every flag shown (`--force`, `--workflows/--no-workflows`, `--version`) exists.
3. Every fenced ```yaml block in the guide validates against `MachinistConfig` from `src/machinist/config.py` (all fields have defaults, so partial top-level snippets validate too; `extra="forbid"` catches typos).
4. The configuration reference documents every key in the packaged template `src/machinist/templates/machinist.yaml`, including defaults and the validated ranges from `config.py` (`timeout_minutes` 1–240, `spec_timeout_minutes` 1–60, `poll_interval_seconds` ≥ 10, `repo` shaped `owner/repo`).
5. The troubleshooting section covers the real failure modes and quotes the actual error text users will see: missing config (`machinist.yaml not found. Run 'machinist init' first.` from `config.py`), missing `gh` (`github.py`), missing harness executable (`harness/base.py`), harness timeout, empty spec (`phases/spec.py`), and a leftover workspace (`workspace <path> already exists; remove it (or 'git worktree remove' it) and retry` from `workspace.py`).
6. Commands not yet implemented (`watch`, `run`, `status` — all raise "not implemented" in `cli.py`) are presented in a clearly labeled "What's next" section, matching the milestones in `docs/superpowers/specs/2026-08-16-agentmachinist-design.md`, never as working features.
7. `README.md` links to `docs/getting-started.md` near the top; existing README content is otherwise unchanged.
8. Drift tests in a new `tests/test_docs.py` enforce requirements 1–3 and 7, and the full suite stays green.

## Proposed approach

**New: `docs/getting-started.md`** — the guide itself, ~250–350 lines, friendly second-person prose. Section plan, grounded in the code:

1. **What is AgentMachinist?** — reuse the ASCII pipeline diagram style from `README.md`; emphasize the human-in-the-loop guarantees (spec approved before code, PR reviewed before merge).
2. **Before you begin** — prerequisites with verification commands: `git`, authenticated `gh` (`gh auth status`), `uv`, and at least one harness CLI (`claude`, `opencode`, `pi`, `codex` — the `default_command` values in `src/machinist/harness/*.py`).
3. **Install** — `uv tool install` per README's Install section; verify with `machinist --version` (works via `click.version_option(package_name="agentmachinist")` in `cli.py`).
4. **Set up your repository** — run `machinist init`; explain each artifact it writes (`machinist.yaml`, `.machinist/specs/`, `.github/workflows/machinist-spec.yml`, `machinist-approve.yml`) and the printed "Next steps"; cover `--no-workflows` and `--force`; note the `ANTHROPIC_API_KEY` secret is only needed for the CI path.
5. **Your first agent task** — create an issue, label it `agent-task`, run `machinist spec <n>`; narrate what happens using `phases/spec.py` as ground truth: isolated worktree under `~/.machinist/workspaces`, harness runs in read-only print mode, spec lands at `.machinist/specs/issue-<n>-spec.md` on branch `agent/issue-<n>`, draft PR titled `Spec: <title> (#<n>)` with `Closes #<n>`.
6. **Review and approve** — read the spec in the PR's Files changed; apply `machinist:approved` or comment `/machinist-execute`; note the OWNER/MEMBER/COLLABORATOR restriction from `machinist-approve.yml`.
7. **What's next (v0.1 status)** — `run`, `watch`, `status` are stubs today; link the design doc's milestones.
8. **Configuration reference** — one annotated full-config yaml block (kept identical in spirit to `src/machinist/templates/machinist.yaml`) plus a prose walkthrough of each section, including workspace strategies/cleanup policies and the test gate.
9. **Troubleshooting** — table of symptom → cause → fix using the real exception messages listed in requirement 5.

**Edit: `README.md`** — add one line under the intro paragraph: `New here? Start with the [Getting Started guide](docs/getting-started.md).` Nothing else moves.

**New: `tests/test_docs.py`** — follows the existing test style (plain functions, stdlib + project imports, like `tests/test_cli.py`):
- `test_guide_exists_with_required_sections` — asserts the file exists and contains the required section headings.
- `test_readme_links_to_guide` — asserts `docs/getting-started.md` appears in `README.md`.
- `test_guide_subcommands_are_real` — regex `machinist ([a-z][a-z-]*)` over the guide; every captured subcommand must be in `main.commands` (import `main` from `machinist.cli`).
- `test_guide_yaml_blocks_validate` — extract every ```yaml fenced block, `yaml.safe_load` it, and `MachinistConfig.model_validate` it; mirrors the existing `test_init_template_round_trips_through_schema` pattern so schema drift fails loudly. Guide convention (stated in a comment): yaml snippets are always rooted at the top level.
- `test_guide_uses_real_label_names` — asserts `agent-task` and `machinist:approved` appear, guarding against label renames leaving the docs stale.

Per the project's TDD convention, `tests/test_docs.py` is written first (red), then the guide and README link make it green.

## Testing plan

- **Automated:** `uv run pytest` (the repo's own `machinist.yaml` test gate; `pyproject.toml` sets `testpaths = ["tests"]`, `-q`). The five new tests in `tests/test_docs.py` prove requirements 1–3, 5 (heading/section presence), 7, and 8; the existing 59 tests must stay green.
- **Manual smoke:** in a scratch git repo, follow the guide verbatim through section 4 (`uv tool install` → `machinist --version` → `machinist init` → inspect written files) confirming every command and output matches. The `machinist spec` walkthrough is verified against `phases/spec.py` and `tests/test_spec_phase.py` behavior rather than a live run, since a live run creates a real PR.
- **Review check:** diff the guide's config reference against `src/machinist/config.py` field-by-field to confirm defaults and ranges (requirement 4).

## Out of scope

- No docs-site generator (MkDocs/Sphinx), hosting, or publishing pipeline — one Markdown file only.
- No screenshots, video, or diagrams beyond ASCII.
- No documentation of `watch`/`run`/`status` behavior beyond the roadmap note; no changes to CLI code, templates, or workflows.
- No restructuring of `README.md` beyond adding the link; no changes to the design doc under `docs/superpowers/specs/`.
- No translations.

## Open questions

1. Is `agentmachinist` actually published to PyPI? `README.md` offers both `uv tool install agentmachinist` and the `git+https://github.com/vscarpenter/AgentMachinist` fallback; the guide should lead with whichever actually works today.
2. Should the guide live at `docs/getting-started.md` (proposed) or replace/absorb parts of `README.md`? The proposal keeps README as the terse front door and the guide as the narrative walkthrough.
