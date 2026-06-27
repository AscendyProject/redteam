"""Slice A — goal.json manifest schema + up-front fail-closed validation.

Tests that _load_goal_manifest raises on every invalid input and that
process_batch aborts the whole batch without seeding or running any task
when the manifest is invalid.
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
    """Create a minimal batch dir with empty task dirs (no state/input)."""
    batch_dir = tmp_path / "batch"
    tasks_root = batch_dir / "tasks"
    for tid in task_ids:
        (tasks_root / tid).mkdir(parents=True)
    if goal_json is not None:
        (batch_dir / "goal.json").write_text(goal_json, encoding="utf-8")
    return batch_dir


# ---------- _load_goal_manifest unit tests ----------


def test_malformed_json_raises():
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        bad = dp / "goal.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ValueError):
            orch._load_goal_manifest(dp, bad)


def test_duplicate_task_id_detected_pre_collapse():
    """object_pairs_hook detects duplicate 'tasks' keys BEFORE Python's dict collapses them."""
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        (dp / "tasks" / "task-a").mkdir(parents=True)
        # JSON with duplicate task-a key inside 'tasks'
        raw = '{"goal": "g", "tasks": {"task-a": {"depends_on": []}, "task-a": {"depends_on": []}}}'
        goal = dp / "goal.json"
        goal.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate key"):
            orch._load_goal_manifest(dp, goal)


def test_unknown_depends_on_ref_raises():
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        (dp / "tasks" / "task-a").mkdir(parents=True)
        data = {"goal": "g", "tasks": {"task-a": {"depends_on": ["nonexistent"]}}}
        goal = dp / "goal.json"
        goal.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="unknown task"):
            orch._load_goal_manifest(dp, goal)


def test_self_dependency_raises():
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        (dp / "tasks" / "task-a").mkdir(parents=True)
        data = {"goal": "g", "tasks": {"task-a": {"depends_on": ["task-a"]}}}
        goal = dp / "goal.json"
        goal.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="self-dependency"):
            orch._load_goal_manifest(dp, goal)


def test_multi_parent_rejected():
    """len(depends_on) >= 2 fails closed."""
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        for tid in ("task-a", "task-b", "task-c"):
            (dp / "tasks" / tid).mkdir(parents=True)
        data = {
            "goal": "g",
            "tasks": {
                "task-a": {"depends_on": []},
                "task-b": {"depends_on": []},
                "task-c": {"depends_on": ["task-a", "task-b"]},
            },
        }
        goal = dp / "goal.json"
        goal.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="single-parent"):
            orch._load_goal_manifest(dp, goal)


def test_missing_task_dir_raises():
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        # Only create task-a dir, but manifest references task-b too
        (dp / "tasks" / "task-a").mkdir(parents=True)
        data = {
            "goal": "g",
            "tasks": {
                "task-a": {"depends_on": []},
                "task-b": {"depends_on": ["task-a"]},  # task-b dir doesn't exist
            },
        }
        goal = dp / "goal.json"
        goal.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="no on-disk directory"):
            orch._load_goal_manifest(dp, goal)


def test_cycle_detection_raises():
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        for tid in ("task-a", "task-b"):
            (dp / "tasks" / tid).mkdir(parents=True)
        data = {
            "goal": "g",
            "tasks": {
                "task-a": {"depends_on": ["task-b"]},
                "task-b": {"depends_on": ["task-a"]},
            },
        }
        goal = dp / "goal.json"
        goal.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="cycle"):
            orch._load_goal_manifest(dp, goal)


def test_ceilings_non_object_raises():
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        (dp / "tasks" / "task-a").mkdir(parents=True)
        data = {"goal": "g", "ceilings": "not-an-object", "tasks": {"task-a": {"depends_on": []}}}
        goal = dp / "goal.json"
        goal.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="ceilings"):
            orch._load_goal_manifest(dp, goal)


def test_valid_manifest_parses_ok():
    """A valid single-parent manifest parses without error."""
    orch = _orch()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        for tid in ("task-a", "task-b", "task-c"):
            (dp / "tasks" / tid).mkdir(parents=True)
        data = {
            "goal": "do something",
            "ceilings": {"max_cost": 10},
            "tasks": {
                "task-a": {"depends_on": []},
                "task-b": {"depends_on": ["task-a"]},
                "task-c": {"depends_on": ["task-b"]},
            },
        }
        goal = dp / "goal.json"
        goal.write_text(json.dumps(data), encoding="utf-8")
        result = orch._load_goal_manifest(dp, goal)
        assert result["deps"]["task-a"] is None
        assert result["deps"]["task-b"] == "task-a"
        assert result["deps"]["task-c"] == "task-b"
        assert result["ceilings"] == {"max_cost": 10}


# ---------- process_batch integration: fail-closed when manifest is invalid ----------


def test_process_batch_invalid_manifest_no_state_seeded(tmp_path, monkeypatch):
    """Invalid goal.json aborts the batch: no task state is seeded, process_task never called."""
    orch = _orch()
    process_task_calls: list = []
    monkeypatch.setattr(orch, "process_task", lambda *a, **kw: process_task_calls.append(a) or "done")

    batch_dir = _make_batch(tmp_path, ["task-a"], goal_json='{"bad json":')
    results = orch.process_batch(batch_dir)

    # process_task never invoked
    assert process_task_calls == []
    # All entries are error strings
    for val in results.values():
        assert val.startswith("error:")
    # No state.json seeded in any task dir
    assert not (batch_dir / "tasks" / "task-a" / "state.json").exists()


def test_process_batch_absent_manifest_flat_mode(tmp_path, monkeypatch):
    """No goal.json → flat mode: same task order and process_task invocations as before."""
    orch = _orch()
    visited: list[str] = []

    def fake_seed(td):
        pass

    def fake_task(td, **kw):
        visited.append(td.name)
        return "done"

    batch_dir = _make_batch(tmp_path, ["task-b", "task-a"])
    # Give each task an input.md so seeding is triggered
    for tid in ("task-a", "task-b"):
        (batch_dir / "tasks" / tid / "input.md").write_text("brief", encoding="utf-8")

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    # Both tasks ran (flat mode)
    assert set(visited) == {"task-a", "task-b"}
    # Sorted lexicographic order (list_tasks behavior)
    assert visited == sorted(visited)
    assert results == {"task-a": "done", "task-b": "done"}
    # No base_is_parent kwarg passed in flat mode (fake_task accepts **kw and kw is empty)
