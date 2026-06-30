"""Integration test — goal-mode E2E happy-path composition.

Drives a two-task goal.json through orchestrator.process_batch / _run_batch,
composing manifest validation → DAG schedule → stacking-pin → done-criterion
as one unit on the happy path.

Shared scaffolding (_make_e2e_batch, _install_stub_workers, _simple_two_task_manifest)
is module-level so task-002 can import it directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_WF = Path(__file__).resolve().parents[1] / "workflows"
if str(_WF) not in sys.path:
    sys.path.insert(0, str(_WF))


def _orch():
    import _engine

    return _engine.orchestrator()


# ---------- task IDs used in the shared two-task manifest ----------

_PARENT_ID = "task-alpha"
_DEPENDENT_ID = "task-beta"

# ---------- shared scaffolding (task-002 imports these by name) ----------


def _simple_two_task_manifest() -> str:
    """Return a JSON string: one root task and one dependent task (single-parent chain)."""
    tasks = {
        _PARENT_ID: {"depends_on": []},
        _DEPENDENT_ID: {"depends_on": [_PARENT_ID]},
    }
    return json.dumps({"goal": "e2e-happy-path", "tasks": tasks})


def _make_e2e_batch(tmp_path: Path) -> Path:
    """Build a minimal batch dir containing tasks/<id>/input.md and goal.json.

    Also seeds .redteam/config.toml with branch_prefix = "redteam" so that
    _run_batch can resolve the stacking-pin branch without touching the real repo.

    The caller must monkeypatch orch.repo_root to return the returned batch_dir.
    """
    batch_dir = tmp_path / "e2e_batch"
    tasks_root = batch_dir / "tasks"
    for tid in (_PARENT_ID, _DEPENDENT_ID):
        td = tasks_root / tid
        td.mkdir(parents=True)
        (td / "input.md").write_text("brief", encoding="utf-8")
    (batch_dir / "goal.json").write_text(_simple_two_task_manifest(), encoding="utf-8")
    # Minimal config so _run_batch's load_config(repo_root()) resolves branch_prefix
    (batch_dir / ".redteam").mkdir(exist_ok=True)
    (batch_dir / ".redteam" / "config.toml").write_text('[project]\nbranch_prefix = "redteam"\n', encoding="utf-8")
    return batch_dir


def _install_stub_workers(monkeypatch, orch) -> SimpleNamespace:
    """Monkeypatch orch._seed_state and orch.process_task with no-op stubs.

    Returns a SimpleNamespace with:
      .dispatch_order — list[str] of task IDs in the order process_task was called.
      .calls          — dict[str, dict] mapping each task ID to the kwargs it received:
                        {"resolved_base": ..., "base_is_parent": ...}

    All stubs return "done", so the done-criterion and dependency cascade see a
    fully-successful run.
    """
    recorded = SimpleNamespace(dispatch_order=[], calls={})

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        recorded.dispatch_order.append(td.name)
        recorded.calls[td.name] = {
            "resolved_base": resolved_base,
            "base_is_parent": base_is_parent,
        }
        return "done"

    monkeypatch.setattr(orch, "_seed_state", fake_seed)
    monkeypatch.setattr(orch, "process_task", fake_task)
    return recorded


# ---------- happy-path tests ----------


def test_dispatch_ordering(tmp_path, monkeypatch):
    """Dependent task is dispatched strictly after the parent reports 'done'."""
    orch = _orch()
    batch_dir = _make_e2e_batch(tmp_path)
    recorded = _install_stub_workers(monkeypatch, orch)
    monkeypatch.setattr(orch, "repo_root", lambda: batch_dir)

    orch.process_batch(batch_dir)

    parent_idx = recorded.dispatch_order.index(_PARENT_ID)
    dependent_idx = recorded.dispatch_order.index(_DEPENDENT_ID)
    assert parent_idx < dependent_idx, (
        f"Expected {_PARENT_ID!r} dispatched before {_DEPENDENT_ID!r}; order was {recorded.dispatch_order}"
    )


def test_stacking_pin_contract(tmp_path, monkeypatch):
    """Dependent receives base_is_parent=True and resolved_base=branch_prefix/parent_id;
    root receives base_is_parent=False.

    Setup mirrors test_goal_dag_scheduler.test_dependent_task_receives_parent_branch_as_base:
    seed .redteam/config.toml in the batch dir and monkeypatch orch.repo_root.
    """
    orch = _orch()
    batch_dir = _make_e2e_batch(tmp_path)
    recorded = _install_stub_workers(monkeypatch, orch)
    monkeypatch.setattr(orch, "repo_root", lambda: batch_dir)

    orch.process_batch(batch_dir)

    root_call = recorded.calls[_PARENT_ID]
    dep_call = recorded.calls[_DEPENDENT_ID]

    assert root_call["base_is_parent"] is False, "root task must not be flagged as base_is_parent"
    assert dep_call["base_is_parent"] is True, "dependent task must have base_is_parent=True"
    assert dep_call["resolved_base"] == f"redteam/{_PARENT_ID}", (
        f"expected 'redteam/{_PARENT_ID}', got {dep_call['resolved_base']!r}"
    )


def test_done_criterion_complete(tmp_path, monkeypatch):
    """With all stubs returning 'done', _run_batch reports GoalStatus with
    complete=True, done_count==total==2, incomplete_ids==()."""
    orch = _orch()
    batch_dir = _make_e2e_batch(tmp_path)
    _install_stub_workers(monkeypatch, orch)
    monkeypatch.setattr(orch, "repo_root", lambda: batch_dir)

    _results, goal_status = orch._run_batch(batch_dir)

    assert goal_status is not None, "_run_batch must return a GoalStatus (not None) for a valid manifest run"
    assert goal_status.complete is True
    assert goal_status.done_count == 2
    assert goal_status.total == 2
    assert goal_status.incomplete_ids == ()


def test_shared_helper_smoke():
    """_simple_two_task_manifest, _make_e2e_batch, _install_stub_workers are present
    at module scope and callable — smoke contract for task-002's import."""
    assert callable(_simple_two_task_manifest)
    assert callable(_make_e2e_batch)
    assert callable(_install_stub_workers)

    # Manifest must parse and describe exactly one root + one dependent (single-parent chain)
    parsed = json.loads(_simple_two_task_manifest())
    assert "tasks" in parsed
    assert len(parsed["tasks"]) == 2
    roots = [tid for tid, cfg in parsed["tasks"].items() if not cfg["depends_on"]]
    dependents = [tid for tid, cfg in parsed["tasks"].items() if cfg["depends_on"]]
    assert len(roots) == 1, f"expected exactly 1 root task, got {roots}"
    assert len(dependents) == 1, f"expected exactly 1 dependent task, got {dependents}"
    assert roots[0] in parsed["tasks"][dependents[0]]["depends_on"], (
        "dependent's depends_on must reference the root task"
    )
