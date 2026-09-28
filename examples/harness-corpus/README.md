# Harness regression corpus

A small set of fixed Tasks for checking whether a prompt, adapter, or model
change made first-pass Execute worse. Each case runs through the public CLI in
its own disposable repository, then reads the outcome from
`machinist report --json --source local`.

Running the corpus makes paid model calls: one Spec, one Execute, and one
Review for each case. `machinist rehearse` is the free alternative when you
only need to check the controller.

## Run it

From the repository root:

```sh
uv run python scripts/harness_corpus.py --harness claude-code
uv run python scripts/harness_corpus.py --harness codex --model gpt-5
uv run python scripts/harness_corpus.py --harness claude-code --case reject-unknown-timezone
```

The script approves each case's Spec itself, by its exact commit, because the
repository is disposable. Real Tasks still need your Approval.

A case passes when its first Execute attempt succeeds without repair. The
script compares that result with `baseline.json`, keyed by Harness and model,
and exits 1 when a case that passed in the baseline fails now. Duration and
token counts are printed but never fail the run, because they vary between
runs.

With no baseline for a Harness and model, record one:

```sh
uv run python scripts/harness_corpus.py --harness claude-code --write-baseline
```

Commit the updated `baseline.json` with the change it describes. Add
`--keep` to keep each disposable repository and its command log for
inspection.

## Add a case

Add an entry to `cases.json` with an `id`, a tracked `project` directory, an
`objective`, and a `test_command`. Keep objectives small enough that a
working Harness finishes them in one Execute attempt.
