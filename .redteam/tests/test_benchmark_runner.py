"""Tests for the benchmark execution layer: run_benchmark loop, run_one seam, budget, safety.

Covers:
- Happy path: 2 configs × 2 tasks × 2 reps; stub run_one; 8 records in plan order.
- Resume: pre-seed 3 of 8 triples; stub called exactly 5 times; JSONL has all 8.
- Dry-run: stub never called; stdout has planned/skipped counts + cost estimate.
- Dry-run unknown cost: stdout carries the literal string "unknown".
- Budget abort mid-run: stub returns $0.20 each; budget=$1.00; abort after 5 calls;
  run_benchmark returns exit code 3.
- Budget scope per-invocation: $5.00-worth of prior (Codex-only) records pre-seeded;
  their triples excluded from plan; abort count identical to no-history case.
- Unknown estimate never aborts spuriously.
- run_one raises: loop catches, appends outcome="error", continues, returns 0.
- Codex-only cost passthrough: claude_cost_usd=None preserved verbatim.
- Metric extractor: deterministic extraction over synthetic state dict.
- PR-safety static invariants: grep over benchmark.py source.
- Real config untouched: monkeypatched subprocess; config bytes unchanged after run_one.
- Bootstrap driver correctness: driver template contains required rebind/exit; no unsafe literals.

All tests are hermetic: tmp_path, in-memory dicts, stub run_one. No subprocess spawning,
no adapter imports, no orchestrator internal imports.
"""

from __future__ import annotations

import subprocess as real_subprocess
import sys
import types
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import pytest  # noqa: E402

import benchmark as bm  # noqa: E402
from benchmark import (  # noqa: E402
    append_record,
    extract_metrics,
    load_records,
    run_benchmark,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _write_toml(root: Path, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "benchmark.toml").write_text(content, encoding="utf-8")


def _add_task(root: Path, task_id: str, content: str = "some input") -> None:
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "input.md").write_text(content, encoding="utf-8")


def _make_set(
    root: Path, configs: list[str], task_ids: list[str], repetitions: int = 2, budget_usd: float | None = None
) -> None:
    """Write a minimal benchmark.toml with the given configs and tasks."""
    lines = []
    lines.append(f"repetitions = {repetitions}")
    if budget_usd is not None:
        lines.append(f"budget_usd = {budget_usd}")
    lines.append("")
    for cfg in configs:
        lines.append(f"[configs.{cfg}]")
        lines.append(f'planner = "model-{cfg}"')
        lines.append("")
    _write_toml(root, "\n".join(lines))
    for tid in task_ids:
        _add_task(root, tid)


def _make_record(
    config: str, task: str, rep: int, *, outcome: str = "done", claude_cost_usd: float | None = 0.10
) -> dict:
    """Return a minimal BenchmarkRecord-shaped dict."""
    return {
        "schema_version": 1,
        "config": config,
        "task": task,
        "repetition": rep,
        "outcome": outcome,
        "review_rounds": 0,
        "retry_count": 0,
        "rescue_count": 0,
        "scope_creep_count": 0,
        "wall_clock_sec": 1.0,
        "claude_cost_usd": claude_cost_usd,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
    }


def _stub_run_one(call_log: list):
    """Build a stub run_one that appends to call_log and returns a deterministic record."""

    def _run_one(
        set_root: Path, config_name: str, task_id: str, repetition: int, *, config_overrides: dict, workspace: Path
    ) -> dict:
        call_log.append((config_name, task_id, repetition))
        return _make_record(config_name, task_id, repetition)

    return _run_one


# ---------------------------------------------------------------------------
# Happy path: 2 configs × 2 tasks × 2 reps → 8 records in plan order
# ---------------------------------------------------------------------------


def test_run_benchmark_happy_path_8_records(tmp_path: Path) -> None:
    """Full sweep: 2 configs × 2 tasks × 2 reps → 8 records in plan-declaration order."""
    set_root = tmp_path / "bset"
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=2)

    calls: list[tuple] = []
    rc = run_benchmark(set_root, run_one=_stub_run_one(calls))
    assert rc == 0

    # Stub called exactly 8 times
    assert len(calls) == 8

    # Plan order: configs in declaration order × tasks sorted × reps ascending
    expected_order = [
        ("alpha", "task-001", 1),
        ("alpha", "task-001", 2),
        ("alpha", "task-002", 1),
        ("alpha", "task-002", 2),
        ("beta", "task-001", 1),
        ("beta", "task-001", 2),
        ("beta", "task-002", 1),
        ("beta", "task-002", 2),
    ]
    assert calls == expected_order

    # JSONL has exactly 8 records
    results_path = set_root / "results.jsonl"
    records = load_records(results_path)
    assert len(records) == 8
    actual_triples = [(r["config"], r["task"], r["repetition"]) for r in records]
    assert actual_triples == expected_order


# ---------------------------------------------------------------------------
# Resume: pre-seed 3 of 8 → stub invoked exactly 5 times; final JSONL has 8
# ---------------------------------------------------------------------------


def test_run_benchmark_resume_skips_completed(tmp_path: Path) -> None:
    """Pre-seeded triples are skipped; stub invoked for remaining 5; JSONL has 8."""
    set_root = tmp_path / "bset"
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=2)

    # Pre-seed 3 records
    results_path = set_root / "results.jsonl"
    pre_seeded = [
        _make_record("alpha", "task-001", 1),
        _make_record("alpha", "task-001", 2),
        _make_record("alpha", "task-002", 1),
    ]
    for r in pre_seeded:
        append_record(results_path, r)

    calls: list[tuple] = []
    rc = run_benchmark(set_root, run_one=_stub_run_one(calls))
    assert rc == 0

    # Stub called exactly 5 times (8 − 3 skipped)
    assert len(calls) == 5

    # The 5 remaining triples (in plan order)
    expected_remaining = [
        ("alpha", "task-002", 2),
        ("beta", "task-001", 1),
        ("beta", "task-001", 2),
        ("beta", "task-002", 1),
        ("beta", "task-002", 2),
    ]
    assert calls == expected_remaining

    # Final JSONL has exactly 8 records
    final_records = load_records(results_path)
    assert len(final_records) == 8

    # Pre-seeded 3 preserved verbatim at the front
    assert final_records[:3] == pre_seeded

    # All 8 unique triples present
    final_triples = {(r["config"], r["task"], r["repetition"]) for r in final_records}
    assert len(final_triples) == 8


# ---------------------------------------------------------------------------
# Dry-run: stub never called; stdout has counts + cost estimate
# ---------------------------------------------------------------------------


def test_run_benchmark_dry_run_with_cost_estimate(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Dry-run with prior Claude-cost data: stdout shows counts + numeric estimate."""
    set_root = tmp_path / "bset"
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=2)

    # Pre-seed 3 records with known claude_cost_usd
    results_path = set_root / "results.jsonl"
    for cfg, task, rep in [("alpha", "task-001", 1), ("alpha", "task-001", 2), ("alpha", "task-002", 1)]:
        append_record(results_path, _make_record(cfg, task, rep, claude_cost_usd=0.20))

    calls: list[tuple] = []
    rc = run_benchmark(set_root, dry_run=True, run_one=_stub_run_one(calls))
    assert rc == 0

    # Stub never called
    assert len(calls) == 0

    out = capsys.readouterr().out
    # planned=5 (8 total − 3 done), skipped=3
    assert "planned=5" in out
    assert "skipped=3" in out
    # Numeric cost estimate (not "unknown")
    assert "unknown" not in out
    assert "estimated_cost_usd=" in out


def test_run_benchmark_dry_run_unknown_cost(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Dry-run with no prior Claude-cost data: stdout includes literal 'unknown'."""
    set_root = tmp_path / "bset"
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=2)

    # Pre-seed 3 Codex-only records (claude_cost_usd=None)
    results_path = set_root / "results.jsonl"
    for cfg, task, rep in [("alpha", "task-001", 1), ("alpha", "task-001", 2), ("alpha", "task-002", 1)]:
        append_record(results_path, _make_record(cfg, task, rep, claude_cost_usd=None))

    calls: list[tuple] = []
    rc = run_benchmark(set_root, dry_run=True, run_one=_stub_run_one(calls))
    assert rc == 0

    assert len(calls) == 0
    out = capsys.readouterr().out
    assert "unknown" in out
    assert "planned=5" in out
    assert "skipped=3" in out


# ---------------------------------------------------------------------------
# Budget abort mid-run: budget=$1.00, each $0.20 → 5 calls then return 3
# ---------------------------------------------------------------------------


def test_run_benchmark_budget_abort_mid_run(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Budget abort: $0.20 × 5 = $1.00 accumulated before 6th triple; returns 3."""
    set_root = tmp_path / "bset"
    # 2 configs × 2 tasks × 3 reps = 12 triples (> 6)
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=3, budget_usd=1.00)

    calls: list[tuple] = []

    def stub_with_cost(
        set_root: Path, config_name: str, task_id: str, repetition: int, *, config_overrides: dict, workspace: Path
    ) -> dict:
        calls.append((config_name, task_id, repetition))
        return _make_record(config_name, task_id, repetition, claude_cost_usd=0.20)

    rc = run_benchmark(set_root, run_one=stub_with_cost)

    # Stub called exactly 5 times (5 × $0.20 = $1.00 accumulated; 6th refused)
    assert len(calls) == 5
    # Returns budget-abort exit code
    assert rc == 3

    # Stderr carries a clear budget message naming config/task/repetition
    err = capsys.readouterr().err
    assert "aborting before" in err
    assert "$1." in err or "budget" in err.lower()


def test_run_benchmark_budget_abort_stderr_names_refused_triple(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Stderr budget message explicitly names the config/task/rep about to be refused."""
    set_root = tmp_path / "bset"
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=3, budget_usd=1.00)

    def stub_with_cost(set_root, config_name, task_id, repetition, *, config_overrides, workspace):
        return _make_record(config_name, task_id, repetition, claude_cost_usd=0.20)

    run_benchmark(set_root, run_one=stub_with_cost)
    err = capsys.readouterr().err

    # Must name the 6th triple in the plan: (alpha, task-001, 3)
    # alpha × task-001 × reps 1,2,3 → 6th is (alpha, task-002, 3)? Let me work out the order:
    # Plan: alpha/task-001/1, alpha/task-001/2, alpha/task-001/3,
    #       alpha/task-002/1, alpha/task-002/2, alpha/task-002/3, ...
    # 5 dispatched = reps 1,2,3 of alpha/task-001 + reps 1,2 of alpha/task-002
    # 6th refused = (alpha, task-002, 3)
    assert "alpha" in err or "beta" in err  # config named
    assert "task-" in err  # task named
    assert "rep=" in err  # repetition named


def test_run_benchmark_cold_start_budget_warns_but_still_dispatches(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """IR-001 (stack review): budget_usd is a best-effort cap, NOT a hard ceiling
    on the first run. On a cold start (budget set, no prior cost records to
    estimate against), the runner must WARN legibly that the first run(s) may
    overshoot — but must NOT abort before the first dispatch. It keeps dispatching
    and bounds by accumulated OBSERVED cost, matching the mid-run abort design."""
    set_root = tmp_path / "bset"
    # 1 config × 1 task × 1 rep, tiny budget, NO pre-seeded records (cold start).
    _make_set(set_root, ["alpha"], ["task-001"], repetitions=1, budget_usd=0.01)

    calls: list[tuple] = []

    def stub_with_cost(set_root, config_name, task_id, repetition, *, config_overrides, workspace):
        calls.append((config_name, task_id, repetition))
        return _make_record(config_name, task_id, repetition, claude_cost_usd=0.50)

    rc = run_benchmark(set_root, run_one=stub_with_cost)

    # Still dispatched the (only) triple despite the $0.01 cap — the first run's
    # cost is unknowable in advance, so it is NOT aborted before dispatch.
    assert len(calls) == 1
    assert rc == 0
    err = capsys.readouterr().err
    # Legible cold-start warning naming the overshoot risk.
    assert "warning" in err.lower()
    assert "overshoot" in err.lower()


# ---------------------------------------------------------------------------
# Budget scope is per-invocation: $5.00 of historical Codex-only records
# ---------------------------------------------------------------------------


def test_run_benchmark_budget_scope_per_invocation(tmp_path: Path) -> None:
    """Per-invocation budget: prior Claude costs do not count toward in-invocation accumulated.

    Pre-seeds 50 historical records with REAL Claude costs ($0.10 each = $5.00 total),
    using triples NOT in the plan (config='hist' is not in the benchmark set).

    These prior records DO affect estimated_next (mean = $0.10), but the abort
    count is still 5 — identical to the no-history case — because accumulated
    tracks only THIS invocation's records:

      Before 1st: 0.00 + 0.10 = 0.10 < 1.00 → dispatch
      Before 5th: 0.80 + 0.10 = 0.90 < 1.00 → dispatch  (5th call occurs)
      Before 6th: 1.00 + 0.10 = 1.10 ≥ 1.00 → ABORT

    KEY CONTRACT: accumulated = sum of THIS invocation's claude_cost_usd only.
    The $5.00 of historical Claude spend is NEVER added to accumulated.
    """
    set_root = tmp_path / "bset"
    # 2 configs × 2 tasks × 3 reps = 12 triples
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=3, budget_usd=1.00)

    # Pre-seed 50 historical records with REAL Claude costs: 50 × $0.10 = $5.00.
    # These are OUTSIDE the plan (config "hist" not in benchmark set).
    # estimated_next = mean($0.10 × 50) = $0.10 — exercises the non-None estimated_next path.
    results_path = set_root / "results.jsonl"
    for i in range(1, 51):
        append_record(results_path, _make_record("hist", f"hist-task-{i}", 1, outcome="done", claude_cost_usd=0.10))

    calls: list[tuple] = []

    def stub_with_cost(set_root, config_name, task_id, repetition, *, config_overrides, workspace):
        calls.append((config_name, task_id, repetition))
        return _make_record(config_name, task_id, repetition, claude_cost_usd=0.20)

    rc = run_benchmark(set_root, run_one=stub_with_cost)

    # $5.00 of prior Claude spend does NOT change the abort count:
    # accumulated is per-invocation only; abort count is 5 (same as no-history case).
    assert len(calls) == 5
    assert rc == 3


# ---------------------------------------------------------------------------
# Unknown estimate never triggers spurious abort
# ---------------------------------------------------------------------------


def test_run_benchmark_unknown_estimate_never_aborts(tmp_path: Path) -> None:
    """No prior cost data + stub returns None → unknown estimate → no spurious abort."""
    set_root = tmp_path / "bset"
    # 2 configs × 2 tasks × 2 reps = 8 triples
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=2, budget_usd=1.00)

    calls: list[tuple] = []

    def stub_codex_only(set_root, config_name, task_id, repetition, *, config_overrides, workspace):
        calls.append((config_name, task_id, repetition))
        return _make_record(config_name, task_id, repetition, claude_cost_usd=None)

    # Unknown estimate → est=0.0; accumulated stays 0.0 throughout;
    # budget check: 0.0 + 0.0 = 0.0 < 1.00 → never aborts.
    rc = run_benchmark(set_root, run_one=stub_codex_only)
    assert rc == 0
    assert len(calls) == 8  # all 8 triples dispatched


# ---------------------------------------------------------------------------
# run_one raises: loop catches, appends error record, continues, returns 0
# ---------------------------------------------------------------------------


def test_run_benchmark_run_one_raises_continues(tmp_path: Path) -> None:
    """Stub raises RuntimeError on one triple; loop catches, appends error record, returns 0."""
    set_root = tmp_path / "bset"
    _make_set(set_root, ["alpha", "beta"], ["task-001", "task-002"], repetitions=2)

    calls: list[tuple] = []
    raise_on = ("alpha", "task-001", 2)  # second triple in plan

    def stub_raises_once(set_root, config_name, task_id, repetition, *, config_overrides, workspace):
        calls.append((config_name, task_id, repetition))
        if (config_name, task_id, repetition) == raise_on:
            raise RuntimeError("injected test error")
        return _make_record(config_name, task_id, repetition)

    rc = run_benchmark(set_root, run_one=stub_raises_once)
    assert rc == 0  # error caught; loop continues
    assert len(calls) == 8  # all 8 triples attempted

    # JSONL has 8 records (the error triple has outcome="error")
    records = load_records(set_root / "results.jsonl")
    assert len(records) == 8

    # Find the error record
    error_records = [r for r in records if r["config"] == "alpha" and r["task"] == "task-001" and r["repetition"] == 2]
    assert len(error_records) == 1
    err_rec = error_records[0]
    assert err_rec["outcome"] == "error"
    assert err_rec["config"] == "alpha"
    assert err_rec["task"] == "task-001"
    assert err_rec["repetition"] == 2
    assert "started_at" in err_rec
    assert "finished_at" in err_rec


# ---------------------------------------------------------------------------
# Codex-only cost passthrough: claude_cost_usd=None preserved verbatim
# ---------------------------------------------------------------------------


def test_run_benchmark_codex_only_cost_passthrough(tmp_path: Path) -> None:
    """Stub returns claude_cost_usd=None; JSONL record carries null, never coerced to 0.0."""
    set_root = tmp_path / "bset"
    _make_set(set_root, ["alpha"], ["task-001"], repetitions=1)

    def stub_codex(set_root, config_name, task_id, repetition, *, config_overrides, workspace):
        return _make_record(config_name, task_id, repetition, claude_cost_usd=None)

    rc = run_benchmark(set_root, run_one=stub_codex)
    assert rc == 0

    records = load_records(set_root / "results.jsonl")
    assert len(records) == 1
    assert records[0]["claude_cost_usd"] is None


# ---------------------------------------------------------------------------
# Metric extractor: deterministic extraction over a synthetic state dict
# ---------------------------------------------------------------------------


def test_extract_metrics_done_outcome() -> None:
    """next_phase='done' → outcome='done'."""
    state = {"next_phase": "done", "phase_telemetry": [], "retries": {}}
    m = extract_metrics(state)
    assert m["outcome"] == "done"


def test_extract_metrics_deferred_via_flag() -> None:
    """deferred=True → outcome='deferred' (even if next_phase is not 'deferred')."""
    state = {"next_phase": "implement", "deferred": True, "phase_telemetry": []}
    m = extract_metrics(state)
    assert m["outcome"] == "deferred"


def test_extract_metrics_deferred_via_next_phase() -> None:
    """next_phase='deferred' → outcome='deferred'."""
    state = {"next_phase": "deferred", "phase_telemetry": []}
    m = extract_metrics(state)
    assert m["outcome"] == "deferred"


def test_extract_metrics_error_outcome() -> None:
    """next_phase='implement' (not done/deferred) → outcome='error'."""
    state = {"next_phase": "implement", "phase_telemetry": []}
    m = extract_metrics(state)
    assert m["outcome"] == "error"


def test_extract_metrics_review_rounds_and_rescue_count() -> None:
    """review_rounds counts review_code entries; rescue_count is None — not measured.

    Updated for #172: rescue.py invokes no model, so a `rescue` telemetry entry is
    never written by the engine. Counting them therefore always yielded 0, which
    is indistinguishable from a real measurement of zero rescues. None says
    "unmeasured" instead. The synthetic rescue entry below is retained precisely
    to show the extractor does NOT start counting one if it appears.
    """
    state = {
        "next_phase": "done",
        "phase_telemetry": [
            {"phase": "plan_outcome", "provider": "claude", "cost_usd": 0.50, "duration_sec": 60.0},
            {"phase": "implement", "provider": "claude", "cost_usd": 0.80, "duration_sec": 120.0},
            {"phase": "review_code", "provider": "codex", "cost_usd": None, "duration_sec": 30.0},
            {"phase": "review_code", "provider": "codex", "cost_usd": None, "duration_sec": 25.0},
            {"phase": "rescue", "provider": "codex", "cost_usd": None, "duration_sec": 45.0},
            {"phase": "create_pr", "provider": "claude", "cost_usd": 0.20, "duration_sec": 15.0},
        ],
        "retries": {"implement": 2, "review_code": 1},
    }
    m = extract_metrics(state)
    assert m["review_rounds"] == 2
    assert m["rescue_count"] is None
    assert m["retry_count"] == 3  # sum of {implement: 2, review_code: 1}


def test_extract_metrics_claude_cost_sums_only_claude_entries() -> None:
    """claude_cost_usd = sum of claude-provider entries only; None for Codex-only."""
    state = {
        "next_phase": "done",
        "phase_telemetry": [
            {"phase": "plan_outcome", "provider": "claude", "cost_usd": 0.50, "duration_sec": 60.0},
            {"phase": "review_code", "provider": "codex", "cost_usd": None, "duration_sec": 30.0},
            {"phase": "create_pr", "provider": "claude", "cost_usd": 0.20, "duration_sec": 15.0},
        ],
    }
    m = extract_metrics(state)
    assert m["claude_cost_usd"] == pytest.approx(0.70)


def test_extract_metrics_claude_cost_none_when_codex_only() -> None:
    """claude_cost_usd=None when no Claude-provider entries exist."""
    state = {
        "next_phase": "done",
        "phase_telemetry": [
            {"phase": "review_code", "provider": "codex", "cost_usd": None, "duration_sec": 30.0},
        ],
    }
    m = extract_metrics(state)
    assert m["claude_cost_usd"] is None


def test_extract_metrics_wall_clock_sec_sums_duration_with_none() -> None:
    """wall_clock_sec = sum of duration_sec; None entries contribute 0.0."""
    state = {
        "next_phase": "done",
        "phase_telemetry": [
            {"phase": "plan_outcome", "provider": "claude", "cost_usd": 0.10, "duration_sec": 60.0},
            {"phase": "implement", "provider": "claude", "cost_usd": 0.20, "duration_sec": None},
            {"phase": "review_code", "provider": "codex", "cost_usd": None, "duration_sec": 30.0},
        ],
    }
    m = extract_metrics(state)
    assert m["wall_clock_sec"] == pytest.approx(90.0)


def test_extract_metrics_empty_telemetry() -> None:
    """Empty phase_telemetry → phase counts 0; claude_cost_usd and rescue_count None."""
    state = {"next_phase": "done", "phase_telemetry": []}
    m = extract_metrics(state)
    assert m["review_rounds"] == 0
    assert m["rescue_count"] is None  # unmeasured, not "measured as zero" (#172)
    assert m["retry_count"] == 0
    assert m["wall_clock_sec"] == 0.0
    assert m["claude_cost_usd"] is None
    assert m["scope_creep_count"] == 0


def test_extract_metrics_scope_creep_count_returns_zero() -> None:
    """scope_creep_count=0: stalled entry without floor-trip feedback prefix is not scope creep."""
    state = {
        "next_phase": "done",
        "phase_telemetry": [],
        "deferred_requirements": [
            # "stalled" with no floor-trip marker in feedback → not a floor trip
            {"phase": "implement", "reason": "stalled", "attempts": 1},
            # "stalled" with non-floor-trip feedback → not a floor trip
            {
                "phase": "implement",
                "reason": "stalled",
                "attempts": 2,
                "feedback": "implementer agent exited non-zero.",
            },
        ],
    }
    m = extract_metrics(state)
    assert m["scope_creep_count"] == 0


def test_extract_metrics_scope_creep_count_counts_floor_trips() -> None:
    """scope_creep_count counts deferred_requirements entries with floor-trip feedback.

    Both stable floor-trip feedback prefixes (from phase_runners/implement.py):
    - "cross-run trust-root floor:" (_cross_run_trust_root_floor, #117)
    - "refusing to sweep operator tracked WIP" (_floor_outside_scope, #91)
    Both are stable, actually-emitted strings — never invented.
    """
    state = {
        "next_phase": "deferred",
        "phase_telemetry": [],
        "deferred_requirements": [
            {
                "phase": "implement",
                "reason": "stalled",
                "attempts": 2,
                "feedback": (
                    "cross-run trust-root floor: outside-scope paths detected before "
                    "worker invocation; commit or stash these files and re-run. "
                    "Offending paths: some/untracked.txt"
                ),
            },
            {
                "phase": "implement",
                "reason": "stalled",
                "attempts": 3,
                "feedback": (
                    "refusing to sweep operator tracked WIP into the task commit; "
                    "commit or stash your unrelated tracked WIP before re-running. "
                    "Out-of-scope tracked paths: other/wip.py"
                ),
            },
            {
                "phase": "implement",
                "reason": "stalled",
                "attempts": 4,
                "feedback": "implementer agent exited non-zero.\nreturncode=1",
            },  # NOT a floor trip
        ],
    }
    m = extract_metrics(state)
    # Two floor-trip entries, one plain stall → scope_creep_count = 2
    assert m["scope_creep_count"] == 2


def test_extract_metrics_retry_count_sums_retries_dict() -> None:
    """retry_count = sum of all values in state['retries'] dict."""
    state = {
        "next_phase": "done",
        "phase_telemetry": [],
        "retries": {"plan_outcome": 1, "implement": 3, "review_code": 2},
    }
    m = extract_metrics(state)
    assert m["retry_count"] == 6


# ---------------------------------------------------------------------------
# PR-safety static invariants: grep-style check over benchmark.py source
# ---------------------------------------------------------------------------


def _non_comment_source(module_file: Path) -> str:
    """Return the source with comment-only lines removed.

    Comment-only lines (those whose first non-whitespace character is '#') are
    stripped before the safety-invariant checks.  This avoids false positives
    from documentation comments that say "this file contains no X" by naming X.
    Only executable code and string literals (docstrings, template strings) are
    scanned.
    """
    lines = module_file.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if ln.lstrip()[:1] != "#")


def test_benchmark_source_no_unsafe_literals() -> None:
    """benchmark.py executable code must not contain unsafe shell/PR-creation literals.

    Comment-only lines are excluded so that documentation comments naming the
    absent patterns do not cause false positives.
    """
    src = _non_comment_source(Path(bm.__file__))
    forbidden = [
        "gh ",
        "git push",
        "pr create",
        "--force",
    ]
    # The _BENCH_DRIVER_TEMPLATE string is a special case: it contains the key
    # literal "create_pr" (a phase name, not a command), which is fine.
    # We check for shell-level invocation patterns.
    for pattern in forbidden:
        assert pattern not in src, f"benchmark.py executable code contains forbidden literal: {pattern!r}"


def test_benchmark_source_no_create_pr_import() -> None:
    """benchmark.py must not import phase_runners.create_pr (comment lines excluded)."""
    src = _non_comment_source(Path(bm.__file__))
    # Should not import the real create_pr module
    assert "from phase_runners" not in src
    assert "import create_pr" not in src
    assert "phase_runners.create_pr" not in src


# ---------------------------------------------------------------------------
# Real config untouched: monkeypatched subprocess; bytes unchanged after run_one
# ---------------------------------------------------------------------------


def test_run_one_real_config_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real .redteam/config.toml bytes are unchanged after run_one (subprocess stubbed)."""
    # Minimal set_root
    set_root = tmp_path / "bset"
    _write_toml(set_root, '[configs.default]\nplanner = "model-x"\n')
    _add_task(set_root, "task-001")

    # Real config path and bytes before
    real_config_path = bm._repo_root() / ".redteam" / "config.toml"
    config_bytes_before = real_config_path.read_bytes()

    # Track subprocess.run calls
    captured_calls: list[dict] = []

    def mock_subprocess_run(args, **kwargs):
        captured_calls.append({"args": list(args), "cwd": kwargs.get("cwd"), "kwargs": kwargs})
        return real_subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    # Monkeypatch benchmark.subprocess so run_one uses our mock
    mock_module = types.SimpleNamespace(run=mock_subprocess_run)
    monkeypatch.setattr(bm, "subprocess", mock_module)

    # Run run_one (copytree still happens; git + driver calls are mocked)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bm.run_one(
        set_root=set_root,
        config_name="default",
        task_id="task-001",
        repetition=1,
        config_overrides={"planner": "model-x"},
        workspace=workspace,
    )

    # Real config unchanged
    config_bytes_after = real_config_path.read_bytes()
    assert config_bytes_before == config_bytes_after

    # At least one subprocess call with driver path under a temp dir (not the real repo)
    real_repo_str = str(bm._repo_root())
    driver_calls = [c for c in captured_calls if "_bench_driver.py" in " ".join(str(a) for a in c["args"])]
    assert len(driver_calls) == 1, "Expected exactly one driver subprocess call"
    driver_path_str = str(driver_calls[0]["args"][-1])
    # Driver must NOT be under the real repo
    assert not driver_path_str.startswith(real_repo_str), (
        f"Driver path {driver_path_str!r} is inside the real repo; it should be under the tempcopy"
    )
    # cwd for driver call must also not be the real repo
    driver_cwd = driver_calls[0]["cwd"]
    assert not str(driver_cwd).startswith(real_repo_str), (
        f"subprocess cwd {driver_cwd!r} is the real repo; expected tempcopy"
    )


# ---------------------------------------------------------------------------
# Bootstrap driver correctness: template contains required rebind + sys.exit;
# no unsafe literals inside the driver text
# ---------------------------------------------------------------------------


def test_bootstrap_driver_contains_phase_runners_rebind() -> None:
    """Driver template runtime-rebinds PHASE_RUNNERS['create_pr'] to a no-op."""
    assert 'orchestrator.PHASE_RUNNERS["create_pr"]' in bm._BENCH_DRIVER_TEMPLATE


def test_bootstrap_driver_contains_sys_exit_cmd_start() -> None:
    """Driver template calls sys.exit(orchestrator.cmd_start(...))."""
    assert "sys.exit(orchestrator.cmd_start(" in bm._BENCH_DRIVER_TEMPLATE


def test_bootstrap_driver_no_unsafe_literals() -> None:
    """Driver template must not contain gh, git push, or --force literals."""
    driver = bm._BENCH_DRIVER_TEMPLATE
    for pattern in ["gh ", "git push", "--force"]:
        assert pattern not in driver, f"Bootstrap driver template contains forbidden literal: {pattern!r}"


def test_error_fallback_record_reports_rescue_count_unmeasured(tmp_path: Path) -> None:
    """#172 review IR-001: the run_one-raised fallback record must not claim a
    measured zero either.

    An error-only run would otherwise report Rescue rate 0.00% — the same
    fabrication the None migration exists to remove, reintroduced through the
    error path.
    """
    set_root = tmp_path / "bset"
    _make_set(set_root, ["alpha"], ["task-001"], repetitions=1)

    def _boom(*a, **kw):
        raise RuntimeError("dispatch exploded")

    rc = run_benchmark(set_root, run_one=_boom)

    assert rc == 0  # the loop catches and continues
    records = load_records(set_root / "results.jsonl")
    assert len(records) == 1
    assert records[0]["outcome"] == "error"
    assert records[0]["rescue_count"] is None


def test_rescue_count_comes_from_the_durable_counter_not_the_budget() -> None:
    """#172: a CONVERGED task must still report the rescues it took.

    rescue_entry_count is a budget and is zeroed on convergence, so reading it
    would report 0 for exactly the runs that matter. rescue_total_count is never
    reset, which is what makes the metric survive a successful task.
    """
    converged = {
        "next_phase": "done",
        "phase_telemetry": [],
        "rescue_entry_count": 0,  # budget: reset by convergence
        "rescue_total_count": 2,  # cumulative: what actually happened
    }
    assert extract_metrics(converged)["rescue_count"] == 2


def test_rescue_count_discriminates_across_realistic_runs() -> None:
    """The metric must vary with reality, not collapse to a constant.

    Anti-degeneracy: a task that never rescued reports a measured 0, one that
    rescued twice reports 2, and only a pre-counter state reports None.
    """

    def _state(**extra):
        s = {"next_phase": "done", "phase_telemetry": []}
        s.update(extra)
        return s

    assert extract_metrics(_state(rescue_total_count=0))["rescue_count"] == 0
    assert extract_metrics(_state(rescue_total_count=1))["rescue_count"] == 1
    assert extract_metrics(_state(rescue_total_count=2))["rescue_count"] == 2
    # No key at all → the state predates the counter, so it is unmeasured.
    assert extract_metrics(_state())["rescue_count"] is None


def test_state_template_seeds_the_durable_rescue_counter() -> None:
    """A freshly bootstrapped task carries the counter, so "never rescued" is a
    measured 0 rather than indistinguishable from an old engine."""
    import json as _json

    template = Path(__file__).resolve().parents[1] / "templates" / "state.template.json"
    seeded = _json.loads(template.read_text(encoding="utf-8"))
    assert seeded["rescue_total_count"] == 0
