"""The regression corpus compares first-pass Execute outcomes without a model."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "harness_corpus.py"
_CASES = _SCRIPT.parents[1] / "examples" / "harness-corpus" / "cases.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("harness_corpus", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through the registered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


corpus = _load_module()


def _report(terminal: int, succeeded: int, median: float | None = 42.0) -> dict:
    return {
        "first_pass_execute": {
            "terminal_attempts": terminal,
            "succeeded_without_repair": succeeded,
            "success_rate": succeeded / terminal if terminal else None,
        },
        "duration_seconds": {"median": median, "p95": median},
        "token_totals": {"total_tokens": 1200},
    }


def test_committed_cases_are_valid_and_use_a_tracked_project():
    cases = corpus.load_cases(_CASES)

    assert len({case.id for case in cases}) == len(cases) >= 3
    for case in cases:
        assert (_CASES.parents[2] / case.project / "pyproject.toml").is_file()
        assert case.objective and case.test_command


@pytest.mark.parametrize(
    "payload, message",
    [
        ([], "at least one case"),
        ([{"id": "a", "project": "p", "objective": "o"}], "test_command"),
        (
            [
                {"id": "a", "project": "p", "objective": "o", "test_command": "t"},
                {"id": "a", "project": "p", "objective": "o", "test_command": "t"},
            ],
            "duplicate case id 'a'",
        ),
        (
            [{"id": "a", "project": "../p", "objective": "o", "test_command": "t"}],
            "inside the repository",
        ),
    ],
)
def test_invalid_cases_fail_before_any_paid_run(tmp_path, payload, message):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(corpus.CorpusError, match=message):
        corpus.load_cases(path)


def test_summary_reads_first_pass_execute_from_the_report():
    assert corpus.summarize(_report(1, 1)) == {
        "first_pass": True,
        "duration_seconds": 42.0,
        "total_tokens": 1200,
    }
    assert corpus.summarize(_report(1, 0))["first_pass"] is False


def test_summary_treats_a_run_without_execute_as_not_passing():
    # A failed Spec or baseline never reaches Execute; that is not a pass.
    assert corpus.summarize(_report(0, 0, median=None))["first_pass"] is False


def test_baseline_key_separates_models_of_one_harness():
    assert corpus.baseline_key("codex", None) == "codex"
    assert corpus.baseline_key("codex", "gpt-5") == "codex/gpt-5"


def test_only_a_lost_first_pass_is_a_regression():
    baseline = {
        "fixed": {"first_pass": True},
        "still-failing": {"first_pass": False},
        "broken": {"first_pass": True},
        "not-run": {"first_pass": True},
    }
    results = {
        "fixed": {"first_pass": True, "duration_seconds": 900.0},
        "still-failing": {"first_pass": False},
        "broken": {"first_pass": False},
        "new-case": {"first_pass": False},
    }

    assert corpus.find_regressions(baseline, results) == [
        "broken: passed on first Execute in the baseline, failed now"
    ]


_FAKE_MACHINIST = """#!{python}
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_LOG"]).open("a").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "status":
    print(json.dumps({{"spec_sha": "a" * 40}}))
if sys.argv[1] == "report":
    passed = int(os.environ["FAKE_FIRST_PASS"])
    print(json.dumps({{
        "first_pass_execute": {{"terminal_attempts": 1, "succeeded_without_repair": passed}},
        "duration_seconds": {{"median": 3.0}},
        "token_totals": {{}},
    }}))
"""


@pytest.fixture
def fake_machinist(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "machinist"
    executable.write_text(_FAKE_MACHINIST.format(python=sys.executable))
    executable.chmod(0o755)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    return log


def test_run_records_a_baseline_then_fails_on_a_lost_first_pass(
    tmp_path, monkeypatch, fake_machinist, capsys
):
    baseline = tmp_path / "baseline.json"
    arguments = [
        "--harness",
        "codex",
        "--model",
        "gpt-5",
        "--case",
        "reject-unknown-timezone",
        "--baseline",
        str(baseline),
    ]

    monkeypatch.setenv("FAKE_FIRST_PASS", "1")
    assert corpus.main([*arguments, "--write-baseline"]) == 0
    assert json.loads(baseline.read_text()) == {
        "codex/gpt-5": {"reject-unknown-timezone": {"first_pass": True}}
    }
    calls = fake_machinist.read_text().splitlines()
    assert calls[0].startswith("start Reject unknown timezone names")
    assert calls[1:] == [
        "status T1 --json",
        f"approve --task T1 --spec-sha {'a' * 40}",
        "report --json --source local --since 1d",
    ]

    monkeypatch.setenv("FAKE_FIRST_PASS", "0")
    assert corpus.main(arguments) == 1
    assert "REGRESSION reject-unknown-timezone" in capsys.readouterr().out


def test_unknown_case_is_a_usage_error_before_any_run(fake_machinist):
    assert corpus.main(["--harness", "codex", "--case", "missing"]) == 2
    assert not fake_machinist.exists()
