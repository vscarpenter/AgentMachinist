# Onboarding review: September 27, 2026

Reviewed the newcomer path on `docs/goose-site-onboarding` (0.18.0 plus the
unreleased Goose adapter). A structural read covered every entry point. A
separate walkthrough followed `docs/tldr.md` literally in a scratch copy of
`examples/first-task`, using only free commands: `doctor --local`, plain
`rehearse`, and `--help`. It stopped before `machinist start`, so no model
ran. Each finding below was checked against the code or CLI unless marked
unverified. This review changes nothing; it ranks what to change.

## Fix contradictions first

These are small, verified mismatches that mislead a first-time user.

1. Git author. `docs/tldr.md:28`, `README.md:77`, and `docs/local-workflow.md:27`
   ask for a "configured Git author". The controller runs Git with
   `GIT_CONFIG_GLOBAL=/dev/null` (`src/machinist/workspace.py:883`), so a
   global identity is invisible. Without a repository-local `user.email`,
   `commit_all` authors the Spec and candidate commits as the AgentMachinist
   bot, and integration fast-forwards them onto the user's branch. `doctor
   --local` warns about this, while `docs/operator-runbook.md:74` says no extra
   setup is needed. Fix the docs to ask for `git config user.name` and
   `git config user.email` inside the repository, and add both to the
   example's steps.
2. First command. `machinist onboard --help` says "recommended first command",
   and `machinist config show` before the first start tells the user to run
   `onboard`. The docs and `machinist --help` start with `machinist start`.
   Reword `onboard` as optional GitHub setup, and point pre-start errors at
   `machinist start` or `machinist doctor --local`.
3. Amend. `machinist amend --help` offers to "Revise a local Task Spec", and
   the README command table says "Regenerate the local Spec from feedback".
   The guides say amendment cannot revise the initial Spec. Align the help and
   table with the guides.
4. Retry. `machinist retry --help` describes `--fresh` as "the safe default",
   which is true only for legacy GitHub Tasks; local retries resume by
   default. `--phase` has no help text.
5. Sample objective. `docs/tldr.md` points to the example project, then uses
   "Handle an invalid timezone without crashing". The example never crashes;
   unknown names pass through. Use the example's own objective.
6. Baseline rule. `docs/tldr.md:35` says the Gate "must not leave new files
   behind", yet `uv run pytest` always creates `.venv/`. The real rule is no
   new unignored files.
7. Example copy step. `examples/first-task/README.md:12` runs
   `cp -r examples/first-task ~/tmp/first-task`, which assumes a clone and an
   existing `~/tmp`. The package does not ship `examples/`. Add the clone and
   `mkdir -p` steps.

## Simplify the first-Task path

1. Pick one canonical first-Task page. `README.md` and `docs/README.md` name
   `docs/tldr.md`, while `docs/index.html`, `docs/how-it-works.html`, and
   `docs/explainer.html` send "Complete your first Task" to the visual guide.
   Every such link should reach the same page.
2. Lead the README with the path. Release notes and this repository's own
   dogfood Gates fill `README.md:19-38` before Install. Install also carries
   update-check and workflow-drift maintenance. Move release notes to the
   changelog, move maintenance to an upgrade section, and put a three-line
   "New here?" pointer under the title.
3. Drop version history from newcomer pages. Phrases such as "introduced in
   0.14.0" and "Available since 0.16.0" appear 11 times in the README, the
   local workflow guide, and the harness matrix. No test requires them.
4. Add a free preflight to the TLDR. It never mentions `machinist rehearse`
   or `machinist doctor --local`, which checks Harness authentication in under
   a second. Add one sentence that `start` and `approve` make paid model calls
   and run in the foreground.
5. Define six terms where the TLDR starts: Task, Spec, Approval, Harness,
   Workshop, and candidate. Link `CONTEXT.md`, which no page links today.

## Decisions for Vinny

1. Global Git identity. Should the controller read the operator's global
   `user.name` and `user.email` explicitly while still ignoring the rest of
   the global config? That would make item 1 above mostly a code fix.
2. Formats. The docs offer ten first-Task entry points and seven learning
   formats. `docs/job-card.html` overlaps the visual guide, and
   `docs/explainer.html` overlaps `docs/how-it-works.html`.
3. README GitHub section. About 140 README lines cover GitHub automation that
   `docs/getting-started.md` also covers. Moving it needs a deliberate change
   to `test_setup_docs_require_review_commit_and_push`, which requires the
   README itself to carry those steps.
4. Visibility. `doctor --local` does not print the resolved Harness or Gate
   command, `rehearse` prints nothing for about 29 seconds, and `--harness`
   help does not list valid names. Each is a small CLI change.

## Keep

- The core sequence `start`, `approve --task T1 --spec-sha`, `status T1`, and
  `integrate T1` is identical everywhere.
- `doctor --local` is fast, writes nothing, and probes authentication.
- `rehearse` costs nothing and ends with a clear next step.
- The example project's baseline passes from a fresh clone, and its "Why these
  files" section teaches the isolated-checkout rule well.

## Unverified

- The site may serve linked `.md` pages as raw text, because `docs/.nojekyll`
  disables rendering. This was inferred, not checked live.
- The published site now shows labeled unreleased Goose notes that PyPI 0.18.0
  users cannot act on. That follows the documented convention, but it is a
  trade-off worth confirming.
