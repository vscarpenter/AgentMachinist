"""Run fixed Tasks through the public CLI and compare first-pass Execute outcomes.

Each case gets its own disposable repository, so `report --source local`
describes exactly one Task. Running this makes paid model calls: one Spec, one
Execute, and one Review per case.

    uv run python scripts/harness_corpus.py --harness claude-code
    uv run python scripts/harness_corpus.py --harness codex --model gpt-5 --write-baseline
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "examples" / "harness-corpus"
_REQUIRED = ("id", "project", "objective", "test_command")


class CorpusError(Exception):
    """A corpus input cannot be run or compared."""


@dataclass(frozen=True)
class Case:
    id: str
    project: str
    objective: str
    test_command: str


def load_cases(path: Path) -> list[Case]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, list) or not payload:
        raise CorpusError(f"{path} must list at least one case")
    cases: list[Case] = []
    for entry in payload:
        missing = [key for key in _REQUIRED if not entry.get(key)]
        if missing:
            raise CorpusError(
                f"case {entry.get('id')!r} is missing {', '.join(missing)}"
            )
        project = PurePosixPath(entry["project"])
        if project.is_absolute() or ".." in project.parts:
            raise CorpusError(
                f"case {entry['id']!r} project must stay inside the repository"
            )
        if any(case.id == entry["id"] for case in cases):
            raise CorpusError(f"duplicate case id {entry['id']!r}")
        cases.append(Case(*(str(entry[key]) for key in _REQUIRED)))
    return cases


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    first_pass = report["first_pass_execute"]
    # A Harness login, baseline, or Spec failure stops before Execute. That says
    # nothing about Execute quality, so it gets no first-pass verdict.
    reached = first_pass["terminal_attempts"] == 1
    return {
        "reached_execute": reached,
        "first_pass": first_pass["succeeded_without_repair"] == 1 if reached else None,
        "duration_seconds": report["duration_seconds"]["median"],
        "total_tokens": report["token_totals"].get("total_tokens"),
    }


def baseline_key(harness: str, model: str | None) -> str:
    return harness if model is None else f"{harness}/{model}"


def find_regressions(
    baseline: dict[str, dict[str, Any]], results: dict[str, dict[str, Any]]
) -> list[str]:
    # Duration and tokens vary run to run; only a lost first pass fails the corpus.
    return [
        f"{case_id}: passed on first Execute in the baseline, failed now"
        for case_id, expected in baseline.items()
        if expected.get("first_pass")
        and case_id in results
        and results[case_id]["first_pass"] is False
    ]


def run_case(
    case: Case, *, harness: str, model: str | None, machinist: str, work: Path
) -> dict[str, Any]:
    repository = work / case.id
    _copy_tracked_project(case.project, repository)
    settings: dict[str, Any] = {"name": harness}
    if model is not None:
        settings["model"] = model
    (repository / "machinist.yaml").write_text(
        json.dumps(
            {"harness": settings, "workspace": {"root": str(work / "workshops")}}
        )
    )
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Harness Corpus"),
        ("config", "user.email", "corpus@example.invalid"),
        ("config", "maintenance.auto", "false"),
        ("add", "."),
        ("commit", "-qm", "Corpus baseline"),
    ):
        subprocess.run(["git", *args], cwd=repository, check=True)

    log = work / f"{case.id}.log"

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [machinist, *args],
            cwd=repository,
            capture_output=True,
            text=True,
            env={**os.environ, "MACHINIST_NO_UPDATE_CHECK": "1"},
        )
        with log.open("a") as handle:
            handle.write(
                f"$ machinist {' '.join(args)}\n{result.stdout}{result.stderr}\n"
            )
        return result

    if cli("start", case.objective, "--test-cmd", case.test_command).returncode == 0:
        status = json.loads(cli("status", "T1", "--json").stdout)
        cli("approve", "--task", "T1", "--spec-sha", status["spec_sha"])
    report = cli("report", "--json", "--source", "local", "--since", "1d")
    return summarize(json.loads(report.stdout))


def _copy_tracked_project(project: str, destination: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", project],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    files = [name for name in listed.split("\0") if name]
    if not files:
        raise CorpusError(f"{project} has no tracked files")
    for name in files:
        target = destination / Path(name).relative_to(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / name, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--harness", required=True)
    parser.add_argument("--model")
    parser.add_argument(
        "--case", action="append", dest="only", help="Run one case id; repeatable."
    )
    parser.add_argument("--cases", type=Path, default=CORPUS_DIR / "cases.json")
    parser.add_argument("--baseline", type=Path, default=CORPUS_DIR / "baseline.json")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument(
        "--keep", action="store_true", help="Keep the disposable repositories."
    )
    options = parser.parse_args(argv)

    try:
        cases = load_cases(options.cases)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if options.only:
        unknown = set(options.only) - {case.id for case in cases}
        if unknown:
            print(
                f"error: unknown case ids: {', '.join(sorted(unknown))}",
                file=sys.stderr,
            )
            return 2
        cases = [case for case in cases if case.id in options.only]
    machinist = shutil.which("machinist")
    if machinist is None:
        print("error: machinist is not on PATH; run through `uv run`", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="harness-corpus-"))
    results: dict[str, dict[str, Any]] = {}
    for case in cases:
        result = run_case(
            case,
            harness=options.harness,
            model=options.model,
            machinist=machinist,
            work=work,
        )
        results[case.id] = result
        if not result["reached_execute"]:
            print(f"ERROR {case.id}: did not reach Execute; see {work / case.id}.log")
            continue
        outcome = "first pass" if result["first_pass"] else "FAILED first pass"
        print(f"{case.id}: {outcome} ({result['duration_seconds']}s)")
    unmeasured = [case_id for case_id, r in results.items() if not r["reached_execute"]]

    key = baseline_key(options.harness, options.model)
    baselines = (
        json.loads(options.baseline.read_text()) if options.baseline.is_file() else {}
    )
    if options.write_baseline:
        recorded = baselines.setdefault(key, {})
        recorded.update(
            {
                case_id: {"first_pass": r["first_pass"]}
                for case_id, r in results.items()
                if r["reached_execute"]
            }
        )
        options.baseline.write_text(
            json.dumps(baselines, indent=2, sort_keys=True) + "\n"
        )
        print(f"Recorded the {key} baseline in {options.baseline}.")
    # Keep the logs that the ERROR lines point to.
    if options.keep or unmeasured:
        print(f"Disposable repositories and logs kept in {work}.")
    else:
        shutil.rmtree(work)
    if key not in baselines:
        print(f"No {key} baseline yet; rerun with --write-baseline to record one.")
        return 2 if unmeasured else 0
    regressions = find_regressions(baselines[key], results)
    for line in regressions:
        print(f"REGRESSION {line}")
    if regressions:
        return 1
    return 2 if unmeasured else 0


if __name__ == "__main__":
    sys.exit(main())
