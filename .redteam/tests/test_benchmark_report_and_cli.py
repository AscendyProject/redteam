"""Tests for build_report, run_report, and orchestrator.main CLI dispatch for benchmark subcommands.

Covers:
- build_report: table shape, arithmetic (approval rate, avg wall-clock), column ordering,
  zero-record config (PR-001), n/a Claude cost paths, formatting rules.
- run_report: missing JSONL, empty JSONL → non-zero exit + operator message.
- orchestrator.main dispatch: benchmark / benchmark --dry-run / benchmark-report,
  unknown-flag / missing-arg / non-existent-dir → exit 2, USAGE or clear message.
- USAGE constant contains both new subcommand names (including --dry-run).
All CLI tests monkeypatch benchmark.run_benchmark / benchmark.run_report — no real execution.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import _engine  # noqa: E402
import benchmark as bm  # noqa: E402
from benchmark import build_report, run_report  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _orch():
    return _engine.orchestrator()


def _rec(
    config: str,
    outcome: str = "done",
    *,
    review_rounds: int = 2,
    retry_count: int = 0,
    rescue_count: int = 0,
    scope_creep_count: int = 0,
    wall_clock_sec: float = 60.0,
    claude_cost_usd: float | None = 0.10,
) -> dict:
    return {
        "schema_version": 1,
        "config": config,
        "task": "t1",
        "repetition": 1,
        "outcome": outcome,
        "review_rounds": review_rounds,
        "retry_count": retry_count,
        "rescue_count": rescue_count,
        "scope_creep_count": scope_creep_count,
        "wall_clock_sec": wall_clock_sec,
        "claude_cost_usd": claude_cost_usd,
        "started_at": "2024-01-01T00:00:00+00:00",
        "finished_at": "2024-01-01T00:01:00+00:00",
    }


def _make_set_dir(tmp_path: Path) -> Path:
    """Create a minimal benchmark set directory (just needs to exist as a dir)."""
    d = tmp_path / "myset"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# build_report — table shape and arithmetic
# ---------------------------------------------------------------------------


def test_build_report_basic_table_shape():
    """2 configs, 3 records each → exactly 2 config columns in declaration order."""
    records = [
        _rec("alpha", "done", wall_clock_sec=60.0),
        _rec("alpha", "done", wall_clock_sec=90.0),
        _rec("alpha", "deferred", wall_clock_sec=120.0),
        _rec("beta", "done", wall_clock_sec=100.0),
        _rec("beta", "deferred", wall_clock_sec=50.0),
        _rec("beta", "error", wall_clock_sec=75.0),
    ]
    report = build_report(["alpha", "beta"], records)

    # Header row has both config names
    assert "alpha" in report
    assert "beta" in report
    header_line = report.splitlines()[0]
    assert header_line.index("alpha") < header_line.index("beta")

    # All 8 metric rows present
    for label in (
        "Sample size",
        "Approval rate",
        "Avg review rounds",
        "Retry rate",
        "Rescue rate",
        "Scope-creep rate",
        "Avg wall-clock sec",
        "Claude cost / approved task",
    ):
        assert label in report, f"metric row missing: {label!r}"


def test_build_report_approval_rate_arithmetic():
    """alpha: 2/3 done → 66.67%; beta: 1/3 done → 33.33%."""
    records = [
        _rec("alpha", "done"),
        _rec("alpha", "done"),
        _rec("alpha", "deferred"),
        _rec("beta", "done"),
        _rec("beta", "deferred"),
        _rec("beta", "error"),
    ]
    report = build_report(["alpha", "beta"], records)
    assert "66.67%" in report
    assert "33.33%" in report


def test_build_report_avg_wall_clock_arithmetic():
    """avg wall-clock: (60 + 90 + 120) / 3 = 90.00."""
    records = [
        _rec("cfg", "done", wall_clock_sec=60.0),
        _rec("cfg", "done", wall_clock_sec=90.0),
        _rec("cfg", "done", wall_clock_sec=120.0),
    ]
    report = build_report(["cfg"], records)
    assert "90.00" in report


def test_build_report_column_declaration_order():
    """Columns follow config_names declaration order, not record arrival order."""
    records = [_rec("zz"), _rec("aa")]
    report = build_report(["aa", "zz"], records)
    header = report.splitlines()[0]
    assert header.index("aa") < header.index("zz")


# ---------------------------------------------------------------------------
# build_report — n/a Claude cost paths
# ---------------------------------------------------------------------------


def test_build_report_claude_cost_na_when_all_approved_have_none_cost():
    """All done records have claude_cost_usd=None → cell is 'n/a', never '$0.00'."""
    records = [
        _rec("cfg", "done", claude_cost_usd=None),
        _rec("cfg", "done", claude_cost_usd=None),
    ]
    report = build_report(["cfg"], records)
    cost_line = next(ln for ln in report.splitlines() if "Claude cost" in ln)
    assert "n/a" in cost_line
    assert "$0.00" not in cost_line


def test_build_report_claude_cost_na_when_done_count_zero():
    """Config with only error/deferred records → Claude cost cell is 'n/a'."""
    records = [
        _rec("cfg", "error", claude_cost_usd=5.0),
        _rec("cfg", "deferred", claude_cost_usd=3.0),
    ]
    report = build_report(["cfg"], records)
    cost_line = next(ln for ln in report.splitlines() if "Claude cost" in ln)
    assert "n/a" in cost_line


# ---------------------------------------------------------------------------
# build_report — zero-record config (PR-001)
# ---------------------------------------------------------------------------


def test_build_report_zero_record_config_shown_as_column():
    """Config declared in config_names but absent from records still appears as a column."""
    records = [_rec("a", "done"), _rec("a", "done"), _rec("a", "deferred")]
    report = build_report(["a", "b"], records)
    header = report.splitlines()[0]
    assert "a" in header
    assert "b" in header


def test_build_report_zero_record_config_in_notes():
    """Notes section names the zero-record config."""
    records = [_rec("a")]
    report = build_report(["a", "b"], records)
    notes = report[report.index("## Notes") :]
    assert "b" in notes


def test_build_report_zero_record_config_notes_declaration_order():
    """Notes zero-record list follows config_names order (ordered list comprehension, not set diff)."""
    records = [_rec("b")]  # only 'b' has records; 'a' and 'c' are zero
    report = build_report(["a", "b", "c"], records)
    notes = report[report.index("## Notes") :]
    # The zero-record line should list 'a' before 'c' in declaration order
    zero_line = next(ln for ln in notes.splitlines() if "Zero-record" in ln)
    # "a, c" appears in the line (not "c, a"), confirming declaration order
    assert "a, c" in zero_line


# ---------------------------------------------------------------------------
# build_report — formatting rules
# ---------------------------------------------------------------------------


def test_build_report_rates_formatted_as_percentage():
    """Rates appear as 'XX.XX%', not raw decimals."""
    records = [_rec("cfg", "done"), _rec("cfg", "done"), _rec("cfg", "deferred")]
    report = build_report(["cfg"], records)
    # approval_rate = 2/3 ≈ 66.67%
    assert "66.67%" in report


def test_build_report_cost_formatted_as_dollar():
    """A non-n/a Claude cost appears as '$X.XX'."""
    records = [_rec("cfg", "done", claude_cost_usd=0.42)]
    report = build_report(["cfg"], records)
    cost_line = next(ln for ln in report.splitlines() if "Claude cost" in ln)
    assert "$0.42" in cost_line


def test_build_report_notes_has_record_count():
    """Notes section states the total number of records read."""
    records = [_rec("cfg"), _rec("cfg")]
    report = build_report(["cfg"], records)
    notes = report[report.index("## Notes") :]
    assert "2" in notes  # 2 record(s) read


def test_build_report_notes_codex_disclaimer():
    """Notes section mentions Codex and n/a in the cost disclaimer."""
    report = build_report(["cfg"], [_rec("cfg")])
    notes = report[report.index("## Notes") :]
    assert "Codex" in notes
    assert "n/a" in notes


# ---------------------------------------------------------------------------
# run_report — error cases
# ---------------------------------------------------------------------------


def test_run_report_missing_jsonl(tmp_path, capsys):
    """run_report on a dir without results.jsonl returns non-zero + operator message."""
    set_root = tmp_path / "myset"
    set_root.mkdir()
    rc = run_report(set_root)
    assert rc != 0
    captured = capsys.readouterr()
    assert "orchestrator benchmark" in (captured.out + captured.err)


def test_run_report_empty_jsonl(tmp_path, capsys):
    """run_report on a results.jsonl with zero records returns non-zero + operator message."""
    set_root = tmp_path / "myset"
    set_root.mkdir()
    (set_root / "results.jsonl").write_text("", encoding="utf-8")
    rc = run_report(set_root)
    assert rc != 0
    captured = capsys.readouterr()
    assert "orchestrator benchmark" in (captured.out + captured.err)


# ---------------------------------------------------------------------------
# orchestrator.main — CLI dispatch (all runners monkeypatched)
# ---------------------------------------------------------------------------


def test_main_benchmark_calls_run_benchmark(tmp_path, monkeypatch):
    """benchmark <set> dispatches to run_benchmark with dry_run=False."""
    orch = _orch()
    set_dir = _make_set_dir(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(bm, "run_benchmark", lambda path, *, dry_run=False, **kw: calls.append(("call", dry_run)) or 0)
    rc = orch.main(["orchestrator.py", "benchmark", str(set_dir)])
    assert rc == 0
    assert calls == [("call", False)]


def test_main_benchmark_dry_run_flag(tmp_path, monkeypatch):
    """benchmark <set> --dry-run calls run_benchmark with dry_run=True."""
    orch = _orch()
    set_dir = _make_set_dir(tmp_path)
    calls: list = []
    monkeypatch.setattr(bm, "run_benchmark", lambda path, *, dry_run=False, **kw: calls.append(dry_run) or 0)
    rc = orch.main(["orchestrator.py", "benchmark", str(set_dir), "--dry-run"])
    assert rc == 0
    assert calls == [True]


def test_main_benchmark_wrong_dry_run_ordering_rejected(tmp_path, monkeypatch, capsys):
    """benchmark --dry-run <set> (wrong ordering) → exit 2, USAGE on stderr, no run_benchmark."""
    orch = _orch()
    set_dir = _make_set_dir(tmp_path)
    calls: list = []
    monkeypatch.setattr(bm, "run_benchmark", lambda *a, **kw: calls.append(1) or 0)
    rc = orch.main(["orchestrator.py", "benchmark", "--dry-run", str(set_dir)])
    assert rc == 2
    assert not calls
    err = capsys.readouterr().err
    assert err  # some output on stderr (USAGE or error message)


def test_main_benchmark_report_calls_run_report(tmp_path, monkeypatch):
    """benchmark-report <set> dispatches to run_report."""
    orch = _orch()
    set_dir = _make_set_dir(tmp_path)
    calls: list = []
    monkeypatch.setattr(bm, "run_report", lambda path: calls.append(str(path)) or 0)
    rc = orch.main(["orchestrator.py", "benchmark-report", str(set_dir)])
    assert rc == 0
    assert len(calls) == 1


def test_main_benchmark_returns_runner_exit_code(tmp_path, monkeypatch):
    """main forwards the exit code from run_benchmark."""
    orch = _orch()
    set_dir = _make_set_dir(tmp_path)
    monkeypatch.setattr(bm, "run_benchmark", lambda *a, **kw: 3)
    rc = orch.main(["orchestrator.py", "benchmark", str(set_dir)])
    assert rc == 3


def test_main_benchmark_unknown_flag_rejected(tmp_path, monkeypatch, capsys):
    """benchmark <set> --unknown → exit 2 + message on stderr, run_benchmark not called."""
    orch = _orch()
    set_dir = _make_set_dir(tmp_path)
    calls: list = []
    monkeypatch.setattr(bm, "run_benchmark", lambda *a, **kw: calls.append(1) or 0)
    rc = orch.main(["orchestrator.py", "benchmark", str(set_dir), "--unknown"])
    assert rc == 2
    assert not calls
    assert capsys.readouterr().err


def test_main_benchmark_missing_set_root(capsys):
    """benchmark with no <set-root> → exit 2 + output on stderr."""
    orch = _orch()
    rc = orch.main(["orchestrator.py", "benchmark"])
    assert rc == 2
    assert capsys.readouterr().err


def test_main_benchmark_report_missing_set_root(capsys):
    """benchmark-report with no <set-root> → exit 2 + output on stderr."""
    orch = _orch()
    rc = orch.main(["orchestrator.py", "benchmark-report"])
    assert rc == 2
    assert capsys.readouterr().err


def test_main_benchmark_nonexistent_dir(tmp_path, monkeypatch, capsys):
    """benchmark with non-existent <set-root> → exit 2 + message, run_benchmark not called."""
    orch = _orch()
    calls: list = []
    monkeypatch.setattr(bm, "run_benchmark", lambda *a, **kw: calls.append(1) or 0)
    rc = orch.main(["orchestrator.py", "benchmark", str(tmp_path / "no-such-dir")])
    assert rc == 2
    assert not calls
    assert capsys.readouterr().err


def test_main_benchmark_report_nonexistent_dir(tmp_path, monkeypatch, capsys):
    """benchmark-report with non-existent <set-root> → exit 2 + message, run_report not called."""
    orch = _orch()
    calls: list = []
    monkeypatch.setattr(bm, "run_report", lambda *a, **kw: calls.append(1) or 0)
    rc = orch.main(["orchestrator.py", "benchmark-report", str(tmp_path / "no-such-dir")])
    assert rc == 2
    assert not calls
    assert capsys.readouterr().err


def test_main_benchmark_runner_not_called_on_bad_args(tmp_path, monkeypatch, capsys):
    """run_benchmark must NOT be called on any bad-arg path — verified by raising sentinel."""
    orch = _orch()

    def _fail(*a, **kw) -> int:  # pragma: no cover
        raise AssertionError("run_benchmark must NOT be called on bad args")

    monkeypatch.setattr(bm, "run_benchmark", _fail)

    # Missing set-root
    assert orch.main(["orchestrator.py", "benchmark"]) == 2
    capsys.readouterr()

    # Unknown flag
    set_dir = _make_set_dir(tmp_path)
    assert orch.main(["orchestrator.py", "benchmark", str(set_dir), "--bad"]) == 2
    capsys.readouterr()

    # Non-existent directory
    assert orch.main(["orchestrator.py", "benchmark", str(tmp_path / "no-such")]) == 2
    capsys.readouterr()

    # Wrong --dry-run ordering
    assert orch.main(["orchestrator.py", "benchmark", "--dry-run", str(set_dir)]) == 2
    capsys.readouterr()


def test_main_benchmark_report_runner_not_called_on_bad_args(tmp_path, monkeypatch, capsys):
    """run_report must NOT be called on any bad-arg path — verified by raising sentinel."""
    orch = _orch()

    def _fail(*a, **kw) -> int:  # pragma: no cover
        raise AssertionError("run_report must NOT be called on bad args")

    monkeypatch.setattr(bm, "run_report", _fail)

    # Missing set-root
    assert orch.main(["orchestrator.py", "benchmark-report"]) == 2
    capsys.readouterr()

    # Non-existent directory
    assert orch.main(["orchestrator.py", "benchmark-report", str(tmp_path / "no-such")]) == 2
    capsys.readouterr()

    # Unknown flag after set-root
    set_dir = _make_set_dir(tmp_path)
    assert orch.main(["orchestrator.py", "benchmark-report", str(set_dir), "--unknown"]) == 2
    capsys.readouterr()


# ---------------------------------------------------------------------------
# USAGE constant
# ---------------------------------------------------------------------------


def test_usage_constant_lists_benchmark_subcommands():
    """USAGE constant in orchestrator.py mentions both new subcommand names."""
    orch = _orch()
    assert "benchmark" in orch.USAGE
    assert "benchmark-report" in orch.USAGE


def test_usage_constant_documents_dry_run():
    """USAGE constant documents the --dry-run flag for the benchmark subcommand."""
    orch = _orch()
    assert "--dry-run" in orch.USAGE


# ---------------------------------------------------------------------------
# #172 — an unmeasured metric renders n/a, not 0.00%
# ---------------------------------------------------------------------------


def test_rescue_rate_is_na_when_unmeasured_and_scoped_to_that_metric():
    """#172: rescue_count is None (the engine writes no rescue telemetry), so the
    rate reads n/a — while every other rate keeps computing.

    0.00% would be worse than a blank: it presents "never measured" as "measured
    and found to be zero", which nothing in the report contradicts. Same honesty
    rule the cost column already follows for Codex-only runs.

    The three halves are one claim — the None-handling is correct AND scoped —
    and are asserted together because only the n/a half differs from pre-change
    code; the other two are true on both sides and could not discriminate alone.
    """
    # 1. Unmeasured → n/a (the new behaviour).
    report = build_report(["cfg"], [_rec("cfg", "done", rescue_count=None)] * 2)
    rescue_line = next(ln for ln in report.splitlines() if "Rescue rate" in ln)
    assert "n/a" in rescue_line
    assert "0.00%" not in rescue_line

    # 2. Not hardcoded: if a real source ever supplies values, the rate computes.
    report = build_report(["cfg"], [_rec("cfg", "done", rescue_count=1), _rec("cfg", "done", rescue_count=0)])
    rescue_line = next(ln for ln in report.splitlines() if "Rescue rate" in ln)
    assert "50.00%" in rescue_line

    # 3. Scoped: the neighbouring rates are untouched by the None handling.
    report = build_report(["cfg"], [_rec("cfg", "done", retry_count=1, scope_creep_count=1), _rec("cfg", "done")])
    assert "50.00%" in next(ln for ln in report.splitlines() if "Retry rate" in ln)
    assert "50.00%" in next(ln for ln in report.splitlines() if "Scope-creep rate" in ln)


def test_rescue_rate_uses_only_measured_records_as_the_denominator():
    """#172 review IR-001: unmeasured records must not dilute a measured rate.

    A resumed set can mix legacy records that carry real counts with new ones that
    carry None. Dividing by ALL records reported [1, 0, None, None] as 25.00% when
    the measured subset is 50.00% — a quietly wrong number, which is worse than
    n/a because it looks computed.
    """
    records = [
        _rec("cfg", "done", rescue_count=1),
        _rec("cfg", "done", rescue_count=0),
        _rec("cfg", "done", rescue_count=None),
        _rec("cfg", "done", rescue_count=None),
    ]
    report = build_report(["cfg"], records)
    rescue_line = next(ln for ln in report.splitlines() if "Rescue rate" in ln)
    assert "50.00%" in rescue_line
    assert "25.00%" not in rescue_line
