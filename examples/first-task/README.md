# First Task example

A tiny Python project for trying AgentMachinist on something disposable. It
has one function, one passing test, a committed `uv.lock`, and nothing else.

## Try it

The package does not include this directory, so clone AgentMachinist first.
Copy the directory somewhere outside the clone and give it its own Git history.
Set the author inside the new repository, because AgentMachinist ignores your
global Git identity:

```sh
git clone https://github.com/vscarpenter/AgentMachinist.git
mkdir -p ~/tmp
cp -r AgentMachinist/examples/first-task ~/tmp/first-task
cd ~/tmp/first-task
git init -b main
git config user.name "Your Name"
git config user.email "you@example.com"
git add .
git commit -m "Baseline for the first AgentMachinist Task"
```

Then start one Task. The verification command is detected from
`pyproject.toml`, so no flag is needed:

```sh
machinist start "Reject unknown timezone names in parse_timezone with a ValueError"
```

Read the Spec and copy the printed Approval command. Approval runs
implementation, verification, and independent Review. `machinist status T1`
shows the report path and candidate commit, and `machinist integrate T1`
fast-forwards `main` when you accept the change.

To see the whole loop without spending model tokens first, run
`machinist rehearse` anywhere. It builds its own throwaway repository and uses
a fake Harness.

## Why these files

- `uv.lock` is committed because verification runs `uv run pytest` in an
  isolated Workshop. Without a lockfile, `uv run` writes one there, and the
  controller stops because the baseline changed the Workshop.
- `pythonpath = ["."]` in `pyproject.toml` lets the test import `timezones`
  when pytest runs from the Workshop root.
- `.gitignore` keeps `.venv/` and `.machinist/runs/` out of the Task's diff.
