"""Slice A — topo-layered DAG scheduler: ordering, blocked_on_dependency skip,
transitive cascade, independent-chain continuation, _run_pipeline exit code, and
absent-manifest backward compat.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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


def _simple_manifest(deps: dict[str, list[str]]) -> str:
    tasks = {tid: {"depends_on": parents} for tid, parents in deps.items()}
    return json.dumps({"goal": "test", "tasks": tasks})


# ---------- _topo_layers unit tests ----------


def test_topo_layers_roots_first():
    orch = _orch()
    deps = {"a": None, "b": "a", "c": "b"}
    layers = orch._topo_layers(deps)
    assert layers[0] == ["a"]
    assert layers[1] == ["b"]
    assert layers[2] == ["c"]


def test_topo_layers_multiple_roots_sorted():
    orch = _orch()
    deps = {"task-b": None, "task-a": None, "task-c": "task-a"}
    layers = orch._topo_layers(deps)
    assert layers[0] == ["task-a", "task-b"]  # sorted
    assert layers[1] == ["task-c"]


# ---------- process_batch topo scheduling ----------


def test_roots_run_first_dependents_after_parent_done(tmp_path, monkeypatch):
    """Roots run in layer 0; dependents run only after their parent is done."""
    orch = _orch()
    run_order: list[str] = []

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        run_order.append(td.name)
        return "done"

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    assert run_order.index("task-a") < run_order.index("task-b")
    assert results["task-a"] == "done"
    assert results["task-b"] == "done"


def test_parent_deferred_blocks_child(tmp_path, monkeypatch):
    """A child is blocked_on_dependency when its parent is deferred (not done)."""
    orch = _orch()

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        if td.name == "task-a":
            return "deferred"
        return "done"

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    assert results["task-a"] == "deferred"
    assert results["task-b"] == "blocked_on_dependency"


def test_parent_error_blocks_child(tmp_path, monkeypatch):
    """A child is blocked_on_dependency when its parent returns error."""
    orch = _orch()

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        if td.name == "task-a":
            return "error"
        return "done"

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    assert results["task-b"] == "blocked_on_dependency"


def test_parent_blocked_on_human_gate_blocks_child(tmp_path, monkeypatch):
    """A child is blocked_on_dependency when parent is blocked_on_human_gate."""
    orch = _orch()

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        if td.name == "task-a":
            return "blocked_on_human_gate"
        return "done"

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    assert results["task-b"] == "blocked_on_dependency"


def test_transitive_cascade(tmp_path, monkeypatch):
    """Grandparent deferred → grandchild also blocked_on_dependency (transitive)."""
    orch = _orch()

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        if td.name == "task-a":
            return "deferred"
        # task-b would also be blocked, so process_task shouldn't be called for it
        return "done"  # pragma: no cover

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"], "task-c": ["task-b"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b", "task-c"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    assert results["task-a"] == "deferred"
    assert results["task-b"] == "blocked_on_dependency"
    assert results["task-c"] == "blocked_on_dependency"


def test_independent_chains_continue(tmp_path, monkeypatch):
    """One chain blocked does not stop an independent chain."""
    orch = _orch()
    ran: list[str] = []

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        ran.append(td.name)
        if td.name == "task-x":
            return "deferred"
        return "done"

    # Two independent chains: x→y and a→b
    goal = _simple_manifest(
        {
            "task-x": [],
            "task-y": ["task-x"],
            "task-a": [],
            "task-b": ["task-a"],
        }
    )
    batch_dir = _make_batch(tmp_path, ["task-x", "task-y", "task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    results = orch.process_batch(batch_dir)

    # x-chain blocked
    assert results["task-x"] == "deferred"
    assert results["task-y"] == "blocked_on_dependency"
    # a-chain continues
    assert results["task-a"] == "done"
    assert results["task-b"] == "done"
    assert "task-a" in ran
    assert "task-b" in ran


def test_dep_blocked_does_not_invoke_process_task(tmp_path, monkeypatch):
    """process_task is NOT called for a blocked_on_dependency task."""
    orch = _orch()
    called: list[str] = []

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        called.append(td.name)
        return "deferred"

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    orch.process_batch(batch_dir)

    # process_task called for task-a but NOT task-b (blocked)
    assert "task-a" in called
    assert "task-b" not in called


# ---------- _run_pipeline exit code ----------


def test_run_pipeline_dep_blocked_not_exit_1(tmp_path, monkeypatch):
    """_run_pipeline returns exit code 0 when the only non-done tasks are
    blocked_on_dependency (not blocked_on_human_gate), matching the deferred behavior."""
    orch = _orch()

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        if td.name == "task-a":
            return "deferred"
        return "done"

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    rc = orch._run_pipeline(batch_dir, label="test")
    assert rc == 0  # dep_blocked alone doesn't cause exit 1


def test_run_pipeline_dep_blocked_summary_printed(tmp_path, monkeypatch, capsys):
    """_run_pipeline prints the blocked_on_dependency summary line."""
    orch = _orch()

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        if td.name == "task-a":
            return "deferred"
        return "done"

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    orch._run_pipeline(batch_dir, label="test")
    captured = capsys.readouterr()
    assert "blocked_on_dependency" in captured.err


# ---------- scheduler passes correct base args ----------


def test_dependent_task_receives_parent_branch_as_base(tmp_path, monkeypatch):
    """process_task for a dependent task gets resolved_base = parent branch and base_is_parent=True."""
    orch = _orch()
    received: dict = {}

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        received[td.name] = {"resolved_base": resolved_base, "base_is_parent": base_is_parent}
        return "done"

    goal = _simple_manifest({"task-a": [], "task-b": ["task-a"]})
    batch_dir = _make_batch(tmp_path, ["task-a", "task-b"], goal_json=goal)
    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)

    # Need a real config for branch_prefix. Use a minimal one.
    (tmp_path / "batch" / ".redteam").mkdir(exist_ok=True)
    monkeypatch.setattr(orch, "repo_root", lambda: tmp_path / "batch")
    (tmp_path / "batch" / ".redteam" / "config.toml").write_text(
        '[project]\nbranch_prefix = "redteam"\n', encoding="utf-8"
    )

    orch.process_batch(batch_dir)

    # task-a: root task, no parent
    assert received["task-a"]["base_is_parent"] is False
    # task-b: dependent on task-a → base is redteam/task-a
    assert received["task-b"]["base_is_parent"] is True
    assert received["task-b"]["resolved_base"] == "redteam/task-a"
