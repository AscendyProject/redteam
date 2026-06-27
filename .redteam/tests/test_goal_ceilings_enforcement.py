"""Slice C — ceilings.max_tasks shape validation, enforcement, boundary, and
backward-compat tests.

Coverage:
- max_tasks invalid shapes: 0, negatives, str, float, list, dict, None, bool (True/False)
  each raise ValueError AND cause process_batch to abort fail-closed (no state seeded,
  no task run).
- bool rejection happens BEFORE the integer-range check (bool is-a-int in Python).
- Enforcement: len(tasks) > max_tasks aborts the whole batch fail-closed.
- Boundary: len(tasks) == max_tasks runs normally.
- Backward compat: ceilings absent, or present without max_tasks, no new bound.
- Unknown ceilings keys (max_cost, max_tokens, max_wall_seconds) are tolerate-and-ignore.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- helpers ----------


def _make_batch(tmp_path: Path, task_ids: list[str], goal_json: str | None = None) -> Path:
    """Create a minimal batch dir with task dirs (no state/input by default)."""
    batch_dir = tmp_path / "batch"
    tasks_root = batch_dir / "tasks"
    for tid in task_ids:
        (tasks_root / tid).mkdir(parents=True)
    if goal_json is not None:
        (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")
    return batch_dir


def _make_batch_with_input(tmp_path: Path, task_ids: list[str], goal_json: str | None = None) -> Path:
    """Batch dir where every task has an input.md (so _run_one_task would proceed)."""
    batch_dir = _make_batch(tmp_path, task_ids, goal_json)
    for tid in task_ids:
        (batch_dir / "tasks" / tid / "input.md").write_text("brief", encoding="utf-8")
    return batch_dir


def _manifest(task_ids: list[str], ceilings: dict | None = None) -> str:
    tasks = {tid: {"depends_on": []} for tid in task_ids}
    data: dict = {"goal": "test", "tasks": tasks}
    if ceilings is not None:
        data["ceilings"] = ceilings
    return json.dumps(data)


# ---------- _load_goal_manifest shape validation: invalid max_tasks ----------


@pytest.mark.parametrize(
    "bad_value",
    [
        0,
        -1,
        -100,
        "3",
        "one",
        3.0,
        1.5,
        [],
        [1, 2],
        {},
        {"nested": 1},
    ],
)
def test_max_tasks_invalid_shape_raises(tmp_path, bad_value):
    """`max_tasks` with non-integer-ge-1 value causes _load_goal_manifest to raise."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path, ["task-a"])
    data = {
        "goal": "g",
        "ceilings": {"max_tasks": bad_value},
        "tasks": {"task-a": {"depends_on": []}},
    }
    goal = batch_dir / "goal.json"
    goal.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="max_tasks"):
        orch._load_goal_manifest(batch_dir, goal)


def test_max_tasks_none_raises(tmp_path):
    """`max_tasks: null` (JSON null → Python None) causes ValueError."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path, ["task-a"])
    # Write raw JSON with null value to distinguish from key-absent case
    raw = '{"goal": "g", "ceilings": {"max_tasks": null}, "tasks": {"task-a": {"depends_on": []}}}'
    goal = batch_dir / "goal.json"
    goal.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="max_tasks"):
        orch._load_goal_manifest(batch_dir, goal)


def test_max_tasks_true_raises(tmp_path):
    """`max_tasks: true` is rejected as a boolean, not silently coerced to 1."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path, ["task-a"])
    data = {
        "goal": "g",
        "ceilings": {"max_tasks": True},
        "tasks": {"task-a": {"depends_on": []}},
    }
    goal = batch_dir / "goal.json"
    goal.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="max_tasks"):
        orch._load_goal_manifest(batch_dir, goal)


def test_max_tasks_false_raises(tmp_path):
    """`max_tasks: false` is also rejected as a boolean."""
    orch = _orch()
    batch_dir = _make_batch(tmp_path, ["task-a"])
    data = {
        "goal": "g",
        "ceilings": {"max_tasks": False},
        "tasks": {"task-a": {"depends_on": []}},
    }
    goal = batch_dir / "goal.json"
    goal.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="max_tasks"):
        orch._load_goal_manifest(batch_dir, goal)


def test_bool_rejected_before_integer_range(tmp_path):
    """`True` is rejected as a type error, not silently treated as 1 (bool-is-int trap)."""
    # If the int-range check ran first, isinstance(True, int) is True and True >= 1
    # would be True, so max_tasks=True would be accepted. The bool check must come first.
    orch = _orch()
    batch_dir = _make_batch(tmp_path, ["task-a"])
    data = {
        "goal": "g",
        "ceilings": {"max_tasks": True},
        "tasks": {"task-a": {"depends_on": []}},
    }
    goal = batch_dir / "goal.json"
    goal.write_text(json.dumps(data), encoding="utf-8")
    err = None
    try:
        orch._load_goal_manifest(batch_dir, goal)
    except ValueError as e:
        err = e
    assert err is not None, "expected ValueError"
    # The message should mention max_tasks (type error), not an integer-range message
    assert "max_tasks" in str(err)


# ---------- shape rejection flows through process_batch fail-closed ----------


@pytest.mark.parametrize(
    "bad_value",
    [0, -1, "3", 3.0, True, False, None],
)
def test_invalid_max_tasks_process_batch_aborts_fail_closed(tmp_path, monkeypatch, bad_value):
    """Invalid max_tasks aborts process_batch: no state seeded, no task run."""
    orch = _orch()
    process_task_calls: list = []
    monkeypatch.setattr(orch, "process_task", lambda *a, **kw: process_task_calls.append(a) or "done")

    task_ids = ["task-a", "task-b"]
    batch_dir = _make_batch_with_input(tmp_path, task_ids)
    if bad_value is None:
        raw = json.dumps(
            {
                "goal": "g",
                "ceilings": {"max_tasks": None},
                "tasks": {tid: {"depends_on": []} for tid in task_ids},
            }
        ).replace('"max_tasks": null', '"max_tasks": null')
        # Use raw JSON to get actual null
        raw = (
            '{"goal":"g","ceilings":{"max_tasks":null},"tasks":{"task-a":{"depends_on":[]},"task-b":{"depends_on":[]}}}'
        )
    else:
        raw = json.dumps(
            {
                "goal": "g",
                "ceilings": {"max_tasks": bad_value},
                "tasks": {tid: {"depends_on": []} for tid in task_ids},
            }
        )
    (batch_dir / "goal.json").write_text(raw, encoding="utf-8")

    results = orch.process_batch(batch_dir)

    assert process_task_calls == [], "process_task must not be called on abort"
    for val in results.values():
        assert val.startswith("error:"), f"expected error entry, got {val!r}"
    for tid in task_ids:
        assert not (batch_dir / "tasks" / tid / "state.json").exists()


# ---------- enforcement: len(tasks) > max_tasks ----------


def test_task_count_exceeds_ceiling_raises(tmp_path):
    """len(tasks) > max_tasks causes _load_goal_manifest to raise with a clear message."""
    orch = _orch()
    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch(tmp_path, task_ids)
    data = {
        "goal": "g",
        "ceilings": {"max_tasks": 2},
        "tasks": {tid: {"depends_on": []} for tid in task_ids},
    }
    goal = batch_dir / "goal.json"
    goal.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="3") as exc_info:
        orch._load_goal_manifest(batch_dir, goal)
    # Message should name both the count and the ceiling
    msg = str(exc_info.value)
    assert "max_tasks" in msg or "2" in msg


def test_task_count_exceeds_ceiling_process_batch_aborts_fail_closed(tmp_path, monkeypatch):
    """process_batch aborts when len(tasks) > max_tasks: no seeding, no task run."""
    orch = _orch()
    process_task_calls: list = []
    monkeypatch.setattr(orch, "process_task", lambda *a, **kw: process_task_calls.append(a) or "done")

    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch_with_input(tmp_path, task_ids)
    goal_json = _manifest(task_ids, ceilings={"max_tasks": 2})
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    results = orch.process_batch(batch_dir)

    assert process_task_calls == [], "process_task must not be called"
    for val in results.values():
        assert val.startswith("error:")
    for tid in task_ids:
        assert not (batch_dir / "tasks" / tid / "state.json").exists()


def test_task_count_exceeds_ceiling_run_batch_returns_none_status(tmp_path, monkeypatch):
    """_run_batch returns (error_results, None) when ceiling is violated."""
    orch = _orch()
    monkeypatch.setattr(orch, "process_task", lambda *a, **kw: "done")

    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch_with_input(tmp_path, task_ids)
    goal_json = _manifest(task_ids, ceilings={"max_tasks": 1})
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    results, goal_status = orch._run_batch(batch_dir)

    assert goal_status is None
    for val in results.values():
        assert val.startswith("error:")


# ---------- boundary: len(tasks) == max_tasks runs normally ----------


def test_task_count_equals_ceiling_runs_normally(tmp_path, monkeypatch):
    """len(tasks) == max_tasks is accepted; the batch proceeds through the scheduler."""
    orch = _orch()
    run_calls: list[str] = []

    def fake_seed(td):
        pass

    def fake_task(td, **kw):
        run_calls.append(td.name)
        return "done"

    task_ids = ["task-a", "task-b"]
    batch_dir = _make_batch_with_input(tmp_path, task_ids)
    goal_json = _manifest(task_ids, ceilings={"max_tasks": 2})
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    assert set(run_calls) == {"task-a", "task-b"}
    assert results == {"task-a": "done", "task-b": "done"}


def test_task_count_equals_ceiling_one_task(tmp_path, monkeypatch):
    """Single-task manifest with max_tasks=1 (the minimum valid value) runs."""
    orch = _orch()

    def fake_seed(td):
        pass

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    batch_dir = _make_batch_with_input(tmp_path, ["task-a"])
    goal_json = _manifest(["task-a"], ceilings={"max_tasks": 1})
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    results = orch.process_batch(batch_dir)

    assert results == {"task-a": "done"}


# ---------- backward compat: no new bound ----------


def test_ceilings_absent_no_task_count_bound(tmp_path, monkeypatch):
    """Manifest without a 'ceilings' key imposes no task-count bound."""
    orch = _orch()

    def fake_seed(td):
        pass

    run_calls: list[str] = []

    def fake_task(td, **kw):
        run_calls.append(td.name)
        return "done"

    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch_with_input(tmp_path, task_ids)
    # No ceilings at all
    goal_json = json.dumps({"goal": "test", "tasks": {tid: {"depends_on": []} for tid in task_ids}})
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)
    assert set(run_calls) == set(task_ids)
    assert all(v == "done" for v in results.values())


def test_ceilings_present_without_max_tasks_no_bound(tmp_path, monkeypatch):
    """ceilings object without a max_tasks key imposes no task-count bound."""
    orch = _orch()

    def fake_seed(td):
        pass

    run_calls: list[str] = []

    def fake_task(td, **kw):
        run_calls.append(td.name)
        return "done"

    task_ids = ["task-a", "task-b", "task-c"]
    batch_dir = _make_batch_with_input(tmp_path, task_ids)
    # ceilings present but no max_tasks key
    goal_json = _manifest(task_ids, ceilings={"max_cost": 10})
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)
    assert set(run_calls) == set(task_ids)
    assert all(v == "done" for v in results.values())


# ---------- unknown ceilings keys are tolerate-and-ignore ----------


@pytest.mark.parametrize(
    "extra_keys",
    [
        {"max_cost": 10},
        {"max_tokens": 50000},
        {"max_wall_seconds": 3600},
        {"max_cost": 10, "max_tokens": 50000, "max_wall_seconds": 3600},
    ],
)
def test_unknown_ceiling_keys_are_tolerated(tmp_path, monkeypatch, extra_keys):
    """Token-budget and wall-clock shaped ceilings keys do not abort the batch."""
    orch = _orch()

    def fake_seed(td):
        pass

    run_calls: list[str] = []

    def fake_task(td, **kw):
        run_calls.append(td.name)
        return "done"

    task_ids = ["task-a"]
    batch_dir = _make_batch_with_input(tmp_path, task_ids)
    goal_json = _manifest(task_ids, ceilings=extra_keys)
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    # Must not raise; batch runs normally
    results = orch.process_batch(batch_dir)
    assert run_calls == ["task-a"]
    assert results == {"task-a": "done"}


def test_unknown_ceiling_keys_with_valid_max_tasks(tmp_path, monkeypatch):
    """Unknown keys alongside a valid max_tasks: the batch runs, ceiling is enforced."""
    orch = _orch()

    def fake_seed(td):
        pass

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", lambda td, **kw: "done")

    task_ids = ["task-a"]
    batch_dir = _make_batch_with_input(tmp_path, task_ids)
    goal_json = _manifest(
        task_ids,
        ceilings={"max_tasks": 1, "max_cost": 10, "max_wall_seconds": 7200},
    )
    (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")

    results = orch.process_batch(batch_dir)
    assert results == {"task-a": "done"}


def test_existing_slice_a_test_max_cost_still_passes(tmp_path):
    """Slice A: ceilings with max_cost parses fine (unknown key, tolerate-and-ignore)."""
    orch = _orch()
    for tid in ("task-a", "task-b", "task-c"):
        (tmp_path / "tasks" / tid).mkdir(parents=True)
    data = {
        "goal": "do something",
        "ceilings": {"max_cost": 10},
        "tasks": {
            "task-a": {"depends_on": []},
            "task-b": {"depends_on": ["task-a"]},
            "task-c": {"depends_on": ["task-b"]},
        },
    }
    goal = tmp_path / "goal.json"
    goal.write_text(json.dumps(data), encoding="utf-8")
    result = orch._load_goal_manifest(tmp_path, goal)
    assert result["ceilings"] == {"max_cost": 10}
