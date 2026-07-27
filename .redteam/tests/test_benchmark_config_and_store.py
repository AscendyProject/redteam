"""Tests for the benchmark data-layer: config loader + JSONL result store.

Covers:
- Loader happy path (BenchmarkSet fields, configs order, task_ids sorted).
- Loader fail-loud cases (each raises ValueError naming the offending key/section).
- Per-config role value type validation (bool/int/float/empty/non-scalar rejected).
- JSONL store: append_record / load_records / completed_triples.
- Module invariants: stdlib-only imports, no filesystem side effects at import time.

All tests are hermetic: only tmp_path, no network, no subprocesses, no imports
of adapters or orchestrator internals.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))

import pytest  # noqa: E402

from benchmark import (  # noqa: E402
    BenchmarkSet,
    append_record,
    completed_triples,
    load_benchmark_set,
    load_records,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(root: Path, content: str) -> None:
    (root / "benchmark.toml").write_text(content, encoding="utf-8")


def _add_task(root: Path, task_id: str, content: str = "some input") -> None:
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "input.md").write_text(content, encoding="utf-8")


def _minimal_set(root: Path) -> None:
    """Write the minimal valid benchmark.toml + one task."""
    _write_toml(
        root,
        '[configs.default]\nplanner = "model-x"\n',
    )
    _add_task(root, "task-001")


# ---------------------------------------------------------------------------
# Loader — happy path
# ---------------------------------------------------------------------------


def test_load_happy_path_full(tmp_path: Path) -> None:
    """Full fixture: repetitions, budget_usd, two named configs, two tasks."""
    _write_toml(
        tmp_path,
        "repetitions = 2\n"
        "budget_usd = 5.0\n"
        "\n"
        "[configs.alpha]\n"
        'planner = "model-a"\n'
        'implementer = "model-b"\n'
        "\n"
        "[configs.beta]\n"
        'reviewer = "model-c"\n'
        'rescue = "model-d"\n',
    )
    _add_task(tmp_path, "task-002")
    _add_task(tmp_path, "task-001")

    bs = load_benchmark_set(tmp_path)

    assert isinstance(bs, BenchmarkSet)
    assert bs.repetitions == 2
    assert bs.budget_usd == 5.0
    # Configs: declaration order preserved
    assert list(bs.configs.keys()) == ["alpha", "beta"]
    assert bs.configs["alpha"] == {"planner": "model-a", "implementer": "model-b"}
    assert bs.configs["beta"] == {"reviewer": "model-c", "rescue": "model-d"}
    # task_ids: sorted
    assert bs.task_ids == ("task-001", "task-002")


def test_load_defaults_repetitions_budget(tmp_path: Path) -> None:
    """repetitions defaults to 1; budget_usd defaults to None when absent."""
    _write_toml(
        tmp_path,
        '[configs.only]\nplanner = "model-x"\n',
    )
    _add_task(tmp_path, "t1")
    bs = load_benchmark_set(tmp_path)
    assert bs.repetitions == 1
    assert bs.budget_usd is None


def test_load_task_ids_sorted(tmp_path: Path) -> None:
    """task_ids is always sorted regardless of filesystem enumeration order."""
    _write_toml(tmp_path, '[configs.c]\nplanner = "m"\n')
    for tid in ("task-003", "task-001", "task-002"):
        _add_task(tmp_path, tid)
    bs = load_benchmark_set(tmp_path)
    assert bs.task_ids == ("task-001", "task-002", "task-003")


def test_load_single_config_partial_roles(tmp_path: Path) -> None:
    """A config table with only some roles set is valid; unset roles are absent."""
    _write_toml(tmp_path, '[configs.partial]\nreviewer = "rev-model"\n')
    _add_task(tmp_path, "t1")
    bs = load_benchmark_set(tmp_path)
    assert bs.configs["partial"] == {"reviewer": "rev-model"}


def test_load_budget_usd_int_accepted(tmp_path: Path) -> None:
    """budget_usd accepts plain int (coerced to float)."""
    _write_toml(tmp_path, 'budget_usd = 10\n[configs.c]\nplanner = "m"\n')
    _add_task(tmp_path, "t1")
    bs = load_benchmark_set(tmp_path)
    assert bs.budget_usd == 10.0
    assert isinstance(bs.budget_usd, float)


def test_load_empty_input_md_ignored(tmp_path: Path) -> None:
    """An empty input.md is not counted as a valid task."""
    _write_toml(tmp_path, '[configs.c]\nplanner = "m"\n')
    # one empty task (should be ignored), one valid task
    task_empty = tmp_path / "tasks" / "empty-task"
    task_empty.mkdir(parents=True)
    (task_empty / "input.md").write_text("", encoding="utf-8")
    _add_task(tmp_path, "real-task")
    bs = load_benchmark_set(tmp_path)
    assert bs.task_ids == ("real-task",)


# ---------------------------------------------------------------------------
# Loader — fail-loud cases (unknown keys, bad types, missing files)
# ---------------------------------------------------------------------------


def test_load_fails_missing_toml(tmp_path: Path) -> None:
    """Missing benchmark.toml raises ValueError."""
    with pytest.raises(ValueError, match="benchmark.toml"):
        load_benchmark_set(tmp_path)


def test_load_fails_unknown_top_level_key(tmp_path: Path) -> None:
    """Unknown top-level key raises ValueError naming the key."""
    _write_toml(
        tmp_path,
        'unknown_key = true\n[configs.c]\nplanner = "m"\n',
    )
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="unknown_key"):
        load_benchmark_set(tmp_path)


def test_load_fails_matrix_key(tmp_path: Path) -> None:
    """[[benchmark.matrix]] produces top-level key 'benchmark' — rejected as unknown."""
    _write_toml(
        tmp_path,
        '[[benchmark.matrix]]\nplanner = "m"\n\n[configs.c]\nplanner = "m"\n',
    )
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="benchmark"):
        load_benchmark_set(tmp_path)


def test_load_fails_unknown_per_config_role(tmp_path: Path) -> None:
    """Unknown key inside [configs.<name>] raises ValueError naming the key."""
    _write_toml(
        tmp_path,
        '[configs.bad]\nplanner = "m"\nunknown_role = "x"\n',
    )
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="unknown_role"):
        load_benchmark_set(tmp_path)


def test_load_fails_repetitions_bool(tmp_path: Path) -> None:
    """repetitions = True (bool) is rejected even though bool is int subclass."""
    _write_toml(tmp_path, 'repetitions = true\n[configs.c]\nplanner = "m"\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="repetitions"):
        load_benchmark_set(tmp_path)


def test_load_fails_repetitions_string(tmp_path: Path) -> None:
    """repetitions = \"2\" (string) is rejected."""
    _write_toml(tmp_path, 'repetitions = "2"\n[configs.c]\nplanner = "m"\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="repetitions"):
        load_benchmark_set(tmp_path)


def test_load_fails_repetitions_zero(tmp_path: Path) -> None:
    """repetitions = 0 is rejected (must be >= 1)."""
    _write_toml(tmp_path, 'repetitions = 0\n[configs.c]\nplanner = "m"\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="repetitions"):
        load_benchmark_set(tmp_path)


def test_load_fails_budget_usd_zero(tmp_path: Path) -> None:
    """budget_usd = 0 is rejected (<= 0 not allowed)."""
    _write_toml(tmp_path, 'budget_usd = 0\n[configs.c]\nplanner = "m"\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="budget_usd"):
        load_benchmark_set(tmp_path)


def test_load_fails_budget_usd_negative(tmp_path: Path) -> None:
    """budget_usd = -1.0 is rejected."""
    _write_toml(tmp_path, 'budget_usd = -1.0\n[configs.c]\nplanner = "m"\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="budget_usd"):
        load_benchmark_set(tmp_path)


def test_load_fails_budget_usd_bool(tmp_path: Path) -> None:
    """budget_usd = true (bool) is rejected."""
    _write_toml(tmp_path, 'budget_usd = true\n[configs.c]\nplanner = "m"\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="budget_usd"):
        load_benchmark_set(tmp_path)


def test_load_fails_zero_configs(tmp_path: Path) -> None:
    """No [configs.*] tables raises ValueError."""
    _write_toml(tmp_path, "repetitions = 1\n")
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match="configs"):
        load_benchmark_set(tmp_path)


def test_load_fails_empty_tasks_tree(tmp_path: Path) -> None:
    """tasks/ with no non-empty input.md raises ValueError."""
    _write_toml(tmp_path, '[configs.c]\nplanner = "m"\n')
    # Create tasks dir but no valid input.md
    (tmp_path / "tasks").mkdir()
    with pytest.raises(ValueError, match="input.md"):
        load_benchmark_set(tmp_path)


def test_load_fails_no_tasks_dir(tmp_path: Path) -> None:
    """Missing tasks/ directory (or only empty files) raises ValueError."""
    _write_toml(tmp_path, '[configs.c]\nplanner = "m"\n')
    # No tasks/ directory at all
    with pytest.raises(ValueError, match="input.md"):
        load_benchmark_set(tmp_path)


# ---------------------------------------------------------------------------
# Loader — per-config role value type validation
# ---------------------------------------------------------------------------


def test_load_fails_role_value_int(tmp_path: Path) -> None:
    """planner = 123 (int) raises ValueError naming configs.<name>.planner."""
    _write_toml(tmp_path, "[configs.a]\nplanner = 123\n")
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match=r"configs\.a\.planner"):
        load_benchmark_set(tmp_path)


def test_load_fails_role_value_bool(tmp_path: Path) -> None:
    """reviewer = true (bool) raises ValueError naming configs.<name>.reviewer."""
    _write_toml(tmp_path, "[configs.a]\nreviewer = true\n")
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match=r"configs\.a\.reviewer"):
        load_benchmark_set(tmp_path)


def test_load_fails_role_value_float(tmp_path: Path) -> None:
    """rescue = 1.5 (float) raises ValueError naming configs.<name>.rescue."""
    _write_toml(tmp_path, "[configs.a]\nrescue = 1.5\n")
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match=r"configs\.a\.rescue"):
        load_benchmark_set(tmp_path)


def test_load_fails_role_value_empty_string(tmp_path: Path) -> None:
    """implementer = \"\" (empty string) raises ValueError naming the path."""
    _write_toml(tmp_path, '[configs.a]\nimplementer = ""\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match=r"configs\.a\.implementer"):
        load_benchmark_set(tmp_path)


def test_load_fails_role_value_whitespace_string(tmp_path: Path) -> None:
    """implementer = \"   \" (whitespace-only) raises ValueError."""
    _write_toml(tmp_path, '[configs.a]\nimplementer = "   "\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match=r"configs\.a\.implementer"):
        load_benchmark_set(tmp_path)


def test_load_fails_role_value_array(tmp_path: Path) -> None:
    """planner = [\"x\"] (array / non-scalar) raises ValueError."""
    _write_toml(tmp_path, '[configs.a]\nplanner = ["x"]\n')
    _add_task(tmp_path, "t1")
    with pytest.raises(ValueError, match=r"configs\.a\.planner"):
        load_benchmark_set(tmp_path)


# ---------------------------------------------------------------------------
# JSONL store — append_record / load_records / completed_triples
# ---------------------------------------------------------------------------


def _make_record(config: str, task: str, rep: int, outcome: str = "done") -> dict:
    return {
        "schema_version": 1,
        "config": config,
        "task": task,
        "repetition": rep,
        "outcome": outcome,
        "review_rounds": 1,
        "retry_count": 0,
        "rescue_count": 0,
        "scope_creep_count": 0,
        "wall_clock_sec": 42.0,
        "claude_cost_usd": 0.01,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:42+00:00",
    }


def test_append_record_creates_parent_and_file(tmp_path: Path) -> None:
    """append_record creates the parent directory and file on first call."""
    jsonl_path = tmp_path / "results" / "run.jsonl"
    assert not jsonl_path.parent.exists()
    record = _make_record("cfg-a", "task-001", 1)
    append_record(jsonl_path, record)
    assert jsonl_path.exists()
    assert jsonl_path.parent.is_dir()


def test_append_record_writes_compact_json_line(tmp_path: Path) -> None:
    """Each append_record call writes exactly one compact JSON line."""
    jsonl_path = tmp_path / "results.jsonl"
    record = _make_record("cfg-a", "task-001", 1)
    append_record(jsonl_path, record)
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json as _json

    assert _json.loads(lines[0]) == record
    # compact: no spaces around separators
    assert ", " not in lines[0]
    assert ": " not in lines[0]


def test_load_records_roundtrip_two_records(tmp_path: Path) -> None:
    """Two appended records round-trip via load_records in append order."""
    jsonl_path = tmp_path / "results.jsonl"
    r1 = _make_record("cfg-a", "task-001", 1)
    r2 = _make_record("cfg-b", "task-002", 1, outcome="deferred")
    append_record(jsonl_path, r1)
    append_record(jsonl_path, r2)
    records = load_records(jsonl_path)
    assert len(records) == 2
    assert records[0] == r1
    assert records[1] == r2


def test_load_records_skips_blank_lines(tmp_path: Path) -> None:
    """Blank lines in the JSONL file are silently skipped."""
    jsonl_path = tmp_path / "results.jsonl"
    r1 = _make_record("cfg-a", "task-001", 1)
    r2 = _make_record("cfg-a", "task-001", 2)
    import json as _json

    jsonl_path.write_text(
        _json.dumps(r1, separators=(",", ":")) + "\n\n   \n" + _json.dumps(r2, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    records = load_records(jsonl_path)
    assert records == [r1, r2]


def test_load_records_malformed_line_raises_with_path_and_lineno(tmp_path: Path) -> None:
    """A malformed JSON line raises ValueError naming the file path and 1-indexed line number."""
    jsonl_path = tmp_path / "results.jsonl"
    r1 = _make_record("cfg-a", "task-001", 1)
    import json as _json

    jsonl_path.write_text(
        _json.dumps(r1, separators=(",", ":")) + "\nnot-valid-json\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc_info:
        load_records(jsonl_path)
    msg = str(exc_info.value)
    assert str(jsonl_path) in msg
    assert "2" in msg  # 1-indexed line number


def test_completed_triples_done_records(tmp_path: Path) -> None:
    """completed_triples returns (config, task, rep) for done records."""
    records = [
        _make_record("cfg-a", "task-001", 1),
        _make_record("cfg-b", "task-001", 1),
    ]
    result = completed_triples(records)
    assert result == {("cfg-a", "task-001", 1), ("cfg-b", "task-001", 1)}


def test_completed_triples_error_counts_as_completed(tmp_path: Path) -> None:
    """A record with outcome='error' still counts as completed for resume."""
    records = [_make_record("cfg-a", "task-001", 1, outcome="error")]
    result = completed_triples(records)
    assert ("cfg-a", "task-001", 1) in result


def test_completed_triples_deferred_counts_as_completed(tmp_path: Path) -> None:
    """A record with outcome='deferred' still counts as completed for resume."""
    records = [_make_record("cfg-a", "task-001", 1, outcome="deferred")]
    result = completed_triples(records)
    assert ("cfg-a", "task-001", 1) in result


def test_completed_triples_empty(tmp_path: Path) -> None:
    """completed_triples of an empty list returns an empty set."""
    assert completed_triples([]) == set()


def test_completed_triples_multiple_repetitions(tmp_path: Path) -> None:
    """Each (config, task, rep) triple is tracked independently."""
    records = [
        _make_record("cfg-a", "task-001", 1),
        _make_record("cfg-a", "task-001", 2),
        _make_record("cfg-a", "task-001", 3, outcome="error"),
    ]
    result = completed_triples(records)
    assert result == {
        ("cfg-a", "task-001", 1),
        ("cfg-a", "task-001", 2),
        ("cfg-a", "task-001", 3),
    }


# ---------------------------------------------------------------------------
# Module invariants: stdlib-only imports, no side effects at import time
# ---------------------------------------------------------------------------


def test_benchmark_module_stdlib_only_imports() -> None:
    """benchmark.py must import only stdlib modules (zero runtime dependencies)."""
    import benchmark as _bm

    source = Path(_bm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    stdlib_names: frozenset[str] = frozenset(sys.stdlib_module_names)  # Python 3.10+

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top in stdlib_names, f"Non-stdlib import in benchmark.py: import {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module:
                top = node.module.split(".")[0]
                assert top in stdlib_names, f"Non-stdlib import in benchmark.py: from {node.module} import ..."


def test_benchmark_module_no_side_effects_at_import_time() -> None:
    """benchmark.py must not access the filesystem at import time.

    Verified two ways:
    1. The module was already imported at the top of this file (by `from benchmark
       import ...`) without raising or creating any files — if there were
       top-level I/O calls they would have fired during collection.
    2. AST scan: no top-level Expr/Assign/AugAssign statements call open(),
       Path(...).write_text/read_text/mkdir/unlink, or other filesystem-mutating
       operations outside of a function or class body.
    """
    import benchmark as _bm

    source = Path(_bm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    _IO_ATTRS = frozenset(
        {"open", "write_text", "write_bytes", "read_text", "read_bytes", "mkdir", "unlink", "rmdir", "touch"}
    )

    # Walk only top-level statements (skip function/class bodies)
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Inside functions/classes: side effects are intentional and expected
            continue
        for child in ast.walk(stmt):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name) and func.id == "open":
                    raise AssertionError("Found module-level open() call in benchmark.py")
                if isinstance(func, ast.Attribute) and func.attr in _IO_ATTRS:
                    raise AssertionError(f"Found module-level filesystem call .{func.attr}() in benchmark.py")

    # Public API present (sanity-check the already-imported module)
    assert hasattr(_bm, "BenchmarkSet")
    assert hasattr(_bm, "load_benchmark_set")
    assert hasattr(_bm, "BenchmarkRecord")
    assert hasattr(_bm, "append_record")
    assert hasattr(_bm, "load_records")
    assert hasattr(_bm, "completed_triples")
