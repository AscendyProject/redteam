"""Slice C — _compute_goal_status purity + correctness, _run_batch second-element
semantics, _run_pipeline goal-line surface, and process_batch wrapper backward-compat.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- helpers ----------


def _make_batch(tmp_path: Path, task_ids: list[str], goal_json: str | None = None) -> Path:
    batch_dir = tmp_path / "batch"
    tasks_root = batch_dir / "tasks"
    for tid in task_ids:
        td = tasks_root / tid
        td.mkdir(parents=True)
        (td / "input.md").write_text("brief", encoding="utf-8")
    if goal_json is not None:
        (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")
    return batch_dir


def _simple_manifest(task_ids: list[str]) -> str:
    tasks = {tid: {"depends_on": []} for tid in task_ids}
    return json.dumps({"goal": "test", "tasks": tasks})


# ---------- _compute_goal_status: pure function tests ----------


def test_compute_goal_status_all_done():
    orch = _orch()
    ids = ["task-a", "task-b"]
    results = {"task-a": "done", "task-b": "done"}
    gs = orch._compute_goal_status(results, ids)
    assert gs.complete is True
    assert gs.done_count == 2
    assert gs.total == 2
    assert gs.incomplete_ids == ()


def test_compute_goal_status_none_done():
    orch = _orch()
    ids = ["task-a", "task-b"]
    results = {"task-a": "error", "task-b": "deferred"}
    gs = orch._compute_goal_status(results, ids)
    assert gs.complete is False
    assert gs.done_count == 0
    assert gs.total == 2
    assert set(gs.incomplete_ids) == {"task-a", "task-b"}


def test_compute_goal_status_partial_done():
    orch = _orch()
    ids = ["task-a", "task-b", "task-c"]
    results = {"task-a": "done", "task-b": "error", "task-c": "done"}
    gs = orch._compute_goal_status(results, ids)
    assert gs.complete is False
    assert gs.done_count == 2
    assert gs.total == 3
    assert gs.incomplete_ids == ("task-b",)


def test_compute_goal_status_empty_list():
    orch = _orch()
    gs = orch._compute_goal_status({}, [])
    assert gs.complete is True
    assert gs.done_count == 0
    assert gs.total == 0
    assert gs.incomplete_ids == ()


def test_compute_goal_status_missing_result_key():
    """A task ID not in results is treated as incomplete (results.get(id) returns None)."""
    orch = _orch()
    ids = ["task-a", "task-b"]
    results = {"task-a": "done"}  # task-b absent
    gs = orch._compute_goal_status(results, ids)
    assert gs.complete is False
    assert gs.done_count == 1
    assert gs.incomplete_ids == ("task-b",)


# ---------- _compute_goal_status: one explicit case per non-done result string ----------


@pytest.mark.parametrize(
    "result_str",
    [
        "blocked_on_dependency",
        "deferred",
        "error",
        "blocked_on_human_gate",
        "no_input_md",
        "error: RuntimeError('boom')",  # f"error: {e!r}" shape from _run_one_task
    ],
)
def test_compute_goal_status_each_non_done_string(result_str):
    orch = _orch()
    ids = ["task-a"]
    results = {"task-a": result_str}
    gs = orch._compute_goal_status(results, ids)
    assert gs.complete is False
    assert gs.done_count == 0
    assert "task-a" in gs.incomplete_ids


def test_compute_goal_status_incomplete_ids_in_dispatch_order():
    """incomplete_ids are in manifest_task_ids order (not result-dict insertion order)."""
    orch = _orch()
    # Order the manifest_task_ids in a specific sequence
    ids = ["task-c", "task-a", "task-b"]
    results = {
        "task-a": "no_input_md",
        "task-b": "done",
        "task-c": "error: RuntimeError('boom')",
    }
    gs = orch._compute_goal_status(results, ids)
    assert gs.complete is False
    assert gs.done_count == 1
    # incomplete_ids should be in the order they appear in ids (task-c first, then task-a)
    assert gs.incomplete_ids == ("task-c", "task-a")


def test_compute_goal_status_mixed_case():
    """Mixed results: done + no_input_md + error-prefixed + deferred → correct status."""
    orch = _orch()
    ids = ["task-a", "task-b", "task-c", "task-d"]
    results = {
        "task-a": "done",
        "task-b": "no_input_md",
        "task-c": "error: RuntimeError('boom')",
        "task-d": "deferred",
    }
    gs = orch._compute_goal_status(results, ids)
    assert gs.complete is False
    assert gs.done_count == 1
    assert gs.total == 4
    assert gs.incomplete_ids == ("task-b", "task-c", "task-d")


# ---------- _compute_goal_status: GoalStatus type contract ----------


def test_goal_status_is_named_tuple():
    """GoalStatus exposes .complete, .done_count, .total, .incomplete_ids."""
    orch = _orch()
    gs = orch._compute_goal_status({"task-a": "done"}, ["task-a"])
    # All four attributes accessible
    _ = gs.complete
    _ = gs.done_count
    _ = gs.total
    _ = gs.incomplete_ids


# ---------- _run_batch second-element contract ----------


def test_run_batch_flat_mode_returns_none_status(tmp_path, monkeypatch):
    """No goal.json → _run_batch returns (results, None)."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    batch_dir = tmp_path / "batch"
    task_dir = batch_dir / "tasks" / "task-a"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("brief", encoding="utf-8")

    results, goal_status = orch._run_batch(batch_dir)
    assert goal_status is None
    assert results == {"task-a": "done"}


def test_run_batch_manifest_aborted_slice_a_cause_returns_none_status(tmp_path, monkeypatch):
    """Slice A manifest error → _run_batch returns (error_results, None)."""
    orch = _orch()
    process_task_calls: list = []
    monkeypatch.setattr(orch, "process_task", lambda *a, **kw: process_task_calls.append(a) or "done")

    batch_dir = _make_batch(tmp_path, ["task-a"])
    (batch_dir / "goal.json").write_text("{bad json", encoding="utf-8")

    results, goal_status = orch._run_batch(batch_dir)
    assert goal_status is None
    assert process_task_calls == []
    for val in results.values():
        assert val.startswith("error:")


def test_run_batch_manifest_aborted_ceiling_cause_returns_none_status(tmp_path, monkeypatch):
    """Ceiling violation → _run_batch returns (error_results, None)."""
    orch = _orch()
    monkeypatch.setattr(orch, "process_task", lambda *a, **kw: "done")

    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch(tmp_path, task_ids)
    goal_json = json.dumps(
        {
            "goal": "g",
            "ceilings": {"max_tasks": 2},
            "tasks": {tid: {"depends_on": []} for tid in task_ids},
        }
    )
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    results, goal_status = orch._run_batch(batch_dir)
    assert goal_status is None
    for val in results.values():
        assert val.startswith("error:")


def test_run_batch_manifest_ran_returns_goal_status(tmp_path, monkeypatch):
    """Successful manifest run → _run_batch returns (results, GoalStatus)."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    task_ids = ["task-a", "task-b"]
    batch_dir = _make_batch(tmp_path, task_ids, goal_json=_simple_manifest(task_ids))

    results, goal_status = orch._run_batch(batch_dir)
    assert goal_status is not None
    assert goal_status.complete is True
    assert goal_status.done_count == 2
    assert goal_status.total == 2
    assert goal_status.incomplete_ids == ()


def test_run_batch_manifest_ran_incomplete_returns_goal_status(tmp_path, monkeypatch):
    """Manifest ran with a failure → GoalStatus reflects incomplete tasks."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)

    task_ids = ["task-a", "task-b"]
    batch_dir = _make_batch(tmp_path, task_ids, goal_json=_simple_manifest(task_ids))

    def fake_task(td, **kw):
        return "done" if td.name == "task-a" else "error"

    monkeypatch.setattr(orch, "process_task", fake_task)

    results, goal_status = orch._run_batch(batch_dir)
    assert goal_status is not None
    assert goal_status.complete is False
    assert goal_status.done_count == 1
    assert "task-b" in goal_status.incomplete_ids


def test_run_batch_single_pass_load_goal_manifest_called_once(tmp_path, monkeypatch):
    """_load_goal_manifest is called exactly once per _run_batch invocation."""
    orch = _orch()
    call_count = [0]
    original = orch._load_goal_manifest

    def counting_load(batch_dir, goal_path):
        call_count[0] += 1
        return original(batch_dir, goal_path)

    monkeypatch.setattr(orch, "_load_goal_manifest", counting_load)
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    task_ids = ["task-a"]
    batch_dir = _make_batch(tmp_path, task_ids, goal_json=_simple_manifest(task_ids))
    orch._run_batch(batch_dir)

    assert call_count[0] == 1, "_load_goal_manifest must be called exactly once"


# ---------- process_batch wrapper backward-compat ----------


def test_process_batch_returns_dict(tmp_path, monkeypatch):
    """process_batch still returns dict[str, str] — the thin wrapper contract."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    task_ids = ["task-a", "task-b"]
    batch_dir = _make_batch(tmp_path, task_ids, goal_json=_simple_manifest(task_ids))

    results = orch.process_batch(batch_dir)
    assert isinstance(results, dict)
    assert results == {"task-a": "done", "task-b": "done"}


def test_process_batch_slice_a_equality_assertions(tmp_path, monkeypatch):
    """Existing Slice A equality assertions continue to pass via the thin wrapper."""
    orch = _orch()
    run_order: list[str] = []

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        run_order.append(td.name)
        return "done"

    goal = json.dumps(
        {
            "goal": "test",
            "tasks": {
                "task-a": {"depends_on": []},
                "task-b": {"depends_on": ["task-a"]},
            },
        }
    )
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)
    assert run_order.index("task-a") < run_order.index("task-b")
    assert results["task-a"] == "done"
    assert results["task-b"] == "done"


# ---------- _run_pipeline goal-line surface ----------


def _capture_pipeline(orch, batch_dir):
    """Run _run_pipeline and capture stdout as a list of lines."""
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        orch._run_pipeline(batch_dir, label="start")
    return buf.getvalue().splitlines()


def test_run_pipeline_flat_mode_no_goal_line(tmp_path, monkeypatch):
    """Flat mode (no goal.json): no GOAL COMPLETE / GOAL INCOMPLETE line emitted."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    batch_dir = tmp_path / "batch"
    task_dir = batch_dir / "tasks" / "task-a"
    task_dir.mkdir(parents=True)
    (task_dir / "input.md").write_text("brief", encoding="utf-8")

    lines = _capture_pipeline(orch, batch_dir)
    for line in lines:
        assert not line.startswith("GOAL COMPLETE"), f"unexpected goal line: {line!r}"
        assert not line.startswith("GOAL INCOMPLETE"), f"unexpected goal line: {line!r}"


def test_run_pipeline_manifest_aborted_slice_a_no_goal_line(tmp_path, monkeypatch):
    """Manifest-aborted (Slice A cause): no goal-level line is emitted."""
    orch = _orch()
    monkeypatch.setattr(orch, "process_task", lambda *a, **kw: "done")

    batch_dir = _make_batch(tmp_path, ["task-a"])
    (batch_dir / "goal.json").write_text("{bad json", encoding="utf-8")

    lines = _capture_pipeline(orch, batch_dir)
    for line in lines:
        assert not line.startswith("GOAL COMPLETE"), f"unexpected: {line!r}"
        assert not line.startswith("GOAL INCOMPLETE"), f"unexpected: {line!r}"


def test_run_pipeline_manifest_aborted_ceiling_no_goal_line(tmp_path, monkeypatch):
    """Manifest-aborted (ceiling cause): no goal-level line is emitted."""
    orch = _orch()
    monkeypatch.setattr(orch, "process_task", lambda *a, **kw: "done")

    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch(tmp_path, task_ids)
    goal_json = json.dumps(
        {
            "goal": "g",
            "ceilings": {"max_tasks": 1},
            "tasks": {tid: {"depends_on": []} for tid in task_ids},
        }
    )
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    lines = _capture_pipeline(orch, batch_dir)
    for line in lines:
        assert not line.startswith("GOAL COMPLETE"), f"unexpected: {line!r}"
        assert not line.startswith("GOAL INCOMPLETE"), f"unexpected: {line!r}"


_GOAL_COMPLETE_RE = re.compile(
    r"^GOAL COMPLETE — draft-PR stack ready for human review \(\d+/\d+ tasks done; not merged\)$"
)
_GOAL_INCOMPLETE_RE = re.compile(r"^GOAL INCOMPLETE — \d+/\d+ done; incomplete: \S+(?:, \S+)*$")


def test_run_pipeline_manifest_ran_complete_line(tmp_path, monkeypatch):
    """Manifest-ran, all done: exactly one GOAL COMPLETE line after per-task output."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    task_ids = ["task-a", "task-b"]
    batch_dir = _make_batch(tmp_path, task_ids, goal_json=_simple_manifest(task_ids))

    lines = _capture_pipeline(orch, batch_dir)
    goal_lines = [ln for ln in lines if ln.startswith("GOAL")]
    assert len(goal_lines) == 1, f"expected 1 goal line, got {goal_lines}"
    assert _GOAL_COMPLETE_RE.match(goal_lines[0]), f"format mismatch: {goal_lines[0]!r}"
    # Must contain 2/2
    assert "2/2" in goal_lines[0]


def test_run_pipeline_manifest_ran_complete_line_is_last_non_empty(tmp_path, monkeypatch):
    """GOAL COMPLETE line appears AFTER per-task result lines."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    task_ids = ["task-a"]
    batch_dir = _make_batch(tmp_path, task_ids, goal_json=_simple_manifest(task_ids))

    lines = [ln for ln in _capture_pipeline(orch, batch_dir) if ln.strip()]
    # The GOAL line must be the last non-empty line
    assert lines[-1].startswith("GOAL COMPLETE"), f"last line: {lines[-1]!r}"


def test_run_pipeline_manifest_ran_incomplete_line(tmp_path, monkeypatch):
    """Manifest-ran, some failed: exactly one GOAL INCOMPLETE line with IDs."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)

    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch(tmp_path, task_ids, goal_json=_simple_manifest(task_ids))

    def fake_task(td, **kw):
        return "done" if td.name == "task-a" else "error"

    monkeypatch.setattr(orch, "process_task", fake_task)

    lines = _capture_pipeline(orch, batch_dir)
    goal_lines = [ln for ln in lines if ln.startswith("GOAL")]
    assert len(goal_lines) == 1, f"expected 1 goal line, got {goal_lines}"
    assert _GOAL_INCOMPLETE_RE.match(goal_lines[0]), f"format mismatch: {goal_lines[0]!r}"
    assert "task-b" in goal_lines[0]
    assert "task-c" in goal_lines[0]
    assert "1/3" in goal_lines[0]


def test_run_pipeline_incomplete_names_no_input_md_and_error_prefix(tmp_path, monkeypatch):
    """GOAL INCOMPLETE line names tasks with no_input_md AND error-prefixed results."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)

    # task-a: done, task-b: no_input_md (remove its input.md), task-c: error from process_task
    batch_dir = tmp_path / "batch"
    tasks_root = batch_dir / "tasks"
    for tid in ("task-a", "task-b", "task-c"):
        (tasks_root / tid).mkdir(parents=True)
    # Only task-a and task-c get input.md; task-b has neither state nor input → no_input_md
    (tasks_root / "task-a" / "input.md").write_text("brief", encoding="utf-8")
    (tasks_root / "task-c" / "input.md").write_text("brief", encoding="utf-8")

    goal_json = json.dumps(
        {
            "goal": "test",
            "tasks": {
                "task-a": {"depends_on": []},
                "task-b": {"depends_on": []},
                "task-c": {"depends_on": []},
            },
        }
    )
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    def fake_task(td, **kw):
        if td.name == "task-a":
            return "done"
        raise RuntimeError("boom")

    monkeypatch.setattr(orch, "process_task", fake_task)

    lines = _capture_pipeline(orch, batch_dir)
    goal_lines = [ln for ln in lines if ln.startswith("GOAL")]
    assert len(goal_lines) == 1
    line = goal_lines[0]
    assert line.startswith("GOAL INCOMPLETE"), f"unexpected: {line!r}"
    # Both task-b (no_input_md) and task-c (error-prefixed) must appear
    assert "task-b" in line, f"task-b (no_input_md) not in: {line!r}"
    assert "task-c" in line, f"task-c (error-prefixed) not in: {line!r}"
    assert "task-a" not in line.split("incomplete:")[-1], "done task must not appear"


def test_run_pipeline_incomplete_ids_in_topo_order(tmp_path, monkeypatch):
    """incomplete_ids in the GOAL INCOMPLETE line are in _topo_layers dispatch order."""
    orch = _orch()
    monkeypatch.setattr(orch, "_seed_state", lambda td: None)

    # Linear chain: task-a → task-b → task-c; task-a fails → task-b/c blocked
    goal_json = json.dumps(
        {
            "goal": "test",
            "tasks": {
                "task-a": {"depends_on": []},
                "task-b": {"depends_on": ["task-a"]},
                "task-c": {"depends_on": ["task-b"]},
            },
        }
    )
    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch(tmp_path, task_ids, goal_json=goal_json)

    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "error")

    lines = _capture_pipeline(orch, batch_dir)
    goal_lines = [ln for ln in lines if ln.startswith("GOAL INCOMPLETE")]
    assert len(goal_lines) == 1
    # All three tasks are incomplete; task-a ran and errored; task-b/c blocked
    # They should appear in topo order: task-a, task-b, task-c
    incomplete_part = goal_lines[0].split("incomplete:")[-1].strip()
    ids_in_line = [x.strip() for x in incomplete_part.split(",")]
    assert ids_in_line.index("task-a") < ids_in_line.index("task-b")
    assert ids_in_line.index("task-b") < ids_in_line.index("task-c")
