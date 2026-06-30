"""Integration test — goal-mode E2E composition (happy-path + failure-path).

Drives a two-task goal.json through orchestrator.process_batch / _run_batch,
composing manifest validation → DAG schedule → stacking-pin → done-criterion
as one unit.

Happy-path:  test_dispatch_ordering, test_stacking_pin_contract,
             test_done_criterion_complete, test_shared_helper_smoke.
Failure-path (task-002): test_parent_deferred_blocks_descendant_e2e,
             test_ceilings_max_tasks_mismatch_aborts_e2e,
             test_stacking_pin_base_mismatch_fails_closed_e2e.

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


def _install_stub_workers(monkeypatch, orch, *, task_results=None, real_process_task_ids=None) -> SimpleNamespace:
    """Monkeypatch orch._seed_state and orch.process_task with no-op stubs.

    Optional keyword args (both default to None for backward compat with happy-path tests):
      task_results          — dict mapping task_id → result string to return; tasks absent
                              from it return "done" (same as the original all-"done" stub).
      real_process_task_ids — set of task IDs to route through the REAL orch.process_task
                              instead of the stub; captured before patching so the real
                              function is available even after monkeypatching.

    Returns a SimpleNamespace with:
      .dispatch_order — list[str] of task IDs dispatched through the STUB (not real).
      .calls          — dict[str, dict] mapping stub-dispatched IDs to the kwargs received:
                        {"resolved_base": ..., "base_is_parent": ...}

    All stub-dispatched tasks return "done" unless overridden via task_results.
    """
    recorded = SimpleNamespace(dispatch_order=[], calls={})
    _real_process_task = orch.process_task  # capture before monkeypatching

    def fake_seed(td):
        pass

    def fake_task(td, *, resolved_base=None, base_is_parent=False, **kw):
        if real_process_task_ids and td.name in real_process_task_ids:
            return _real_process_task(td, resolved_base=resolved_base, base_is_parent=base_is_parent, **kw)
        recorded.dispatch_order.append(td.name)
        recorded.calls[td.name] = {
            "resolved_base": resolved_base,
            "base_is_parent": base_is_parent,
        }
        result = "done"
        if task_results and td.name in task_results:
            result = task_results[td.name]
        return result

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


# ---------- failure-path tests (task-002) ----------


def test_parent_deferred_blocks_descendant_e2e(tmp_path, monkeypatch):
    """Deferred parent blocks the dependent in the composed pipeline.

    Stub the parent to return 'deferred'; assert the dependent is never dispatched
    and process_batch reports 'blocked_on_dependency' for it.
    Mirrors test_goal_dag_scheduler.test_parent_deferred_blocks_child.
    """
    orch = _orch()
    batch_dir = _make_e2e_batch(tmp_path)
    recorded = _install_stub_workers(monkeypatch, orch, task_results={_PARENT_ID: "deferred"})
    monkeypatch.setattr(orch, "repo_root", lambda: batch_dir)

    results = orch.process_batch(batch_dir)

    assert _DEPENDENT_ID not in recorded.dispatch_order, (
        f"Expected {_DEPENDENT_ID!r} NOT dispatched through the stub; dispatch_order was {recorded.dispatch_order}"
    )
    assert results[_DEPENDENT_ID] == "blocked_on_dependency", (
        f"Expected 'blocked_on_dependency' for {_DEPENDENT_ID!r}, got {results[_DEPENDENT_ID]!r}"
    )


def test_ceilings_max_tasks_mismatch_aborts_e2e(tmp_path, monkeypatch):
    """ceilings.max_tasks=1 with 2 tasks aborts process_batch fail-closed.

    No task is dispatched (dispatch_order is empty), every entry in the returned
    results dict starts with 'error:', and no state.json is written under any task dir.
    Mirrors test_goal_ceilings_enforcement.test_task_count_exceeds_ceiling_process_batch_aborts_fail_closed
    and test_goal_manifest_validation.test_process_batch_invalid_manifest_no_state_seeded.
    """
    orch = _orch()
    batch_dir = _make_e2e_batch(tmp_path)
    # Overwrite goal.json with a mismatched ceiling: max_tasks=1 but 2 tasks are declared
    manifest = json.loads(_simple_two_task_manifest())
    manifest["ceilings"] = {"max_tasks": 1}
    (batch_dir / "goal.json").write_text(json.dumps(manifest), encoding="utf-8")

    recorded = _install_stub_workers(monkeypatch, orch)
    monkeypatch.setattr(orch, "repo_root", lambda: batch_dir)

    results = orch.process_batch(batch_dir)

    assert recorded.dispatch_order == [], (
        f"Expected no tasks dispatched after ceiling abort; got {recorded.dispatch_order}"
    )
    for val in results.values():
        assert val.startswith("error:"), f"Expected 'error:' prefix in results, got {val!r}"
    for tid in (_PARENT_ID, _DEPENDENT_ID):
        state_path = batch_dir / "tasks" / tid / "state.json"
        assert not state_path.exists(), (
            f"state.json must NOT be seeded for {tid!r} when the batch aborts; found {state_path}"
        )


def test_stacking_pin_base_mismatch_fails_closed_e2e(tmp_path, monkeypatch):
    """Dependent with stale state.base_branch fails closed through the real process_task.

    The parent is stubbed to return 'done' so the cascade reaches the dependent.
    The dependent's pre-seeded state.json has base_branch='main' (a stale flat base),
    but the scheduler resolves its base to 'redteam/task-alpha' (the parent branch).
    The real orch.process_task detects the mismatch at the stacking-pin invariant
    (~orchestrator.py:1070, base_branch_mismatch branch) and returns 'error'.

    Git subprocess boundary is NOT mocked here: the mismatch check fires before any
    git call, so no real git repo is needed.

    Asserts:
    - results[_DEPENDENT_ID] == 'error'  (not 'done', not 'blocked_on_dependency')
    - state.json last_failure_reason == 'base_branch_mismatch'
    - _run_batch GoalStatus: complete=False, _DEPENDENT_ID in incomplete_ids
    """
    orch = _orch()
    batch_dir = _make_e2e_batch(tmp_path)
    monkeypatch.setattr(orch, "repo_root", lambda: batch_dir)

    # Pre-seed the dependent's state.json with a stale flat base that mismatches
    # the scheduler's resolved_base of 'redteam/task-alpha'.
    stale_state = {
        "task_id": _DEPENDENT_ID,
        "mode": "agent-pair",
        "phase": "created",
        "phases_completed": [],
        "next_phase": "plan_outcome",
        "retries": {},
        "max_retries_per_phase": 2,
        "base_branch": "main",  # stale flat base — mismatch with 'redteam/task-alpha'
        "base_branch_sha": "sha_old",
    }
    dep_dir = batch_dir / "tasks" / _DEPENDENT_ID
    (dep_dir / "state.json").write_text(json.dumps(stale_state), encoding="utf-8")

    # Stub only the parent to return "done"; route the dependent through the real process_task.
    _install_stub_workers(
        monkeypatch,
        orch,
        task_results={_PARENT_ID: "done"},
        real_process_task_ids={_DEPENDENT_ID},
    )

    # Use _run_batch to get GoalStatus for the complete/incomplete assertions.
    results, goal_status = orch._run_batch(batch_dir)

    # Dependent must fail closed (not "done", not "blocked_on_dependency")
    assert results[_DEPENDENT_ID] == "error", f"Expected 'error' for {_DEPENDENT_ID!r}, got {results[_DEPENDENT_ID]!r}"
    # Persisted last_failure_reason must name the stacking-pin failure path
    saved = json.loads((dep_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["last_failure_reason"] == "base_branch_mismatch", (
        f"Expected last_failure_reason='base_branch_mismatch', got {saved.get('last_failure_reason')!r}"
    )
    # GoalStatus must reflect incomplete (the batch did not fully succeed)
    assert goal_status is not None, "_run_batch must return a GoalStatus for a valid manifest run"
    assert goal_status.complete is False, "GoalStatus.complete must be False when dependent failed"
    assert _DEPENDENT_ID in goal_status.incomplete_ids, (
        f"Expected {_DEPENDENT_ID!r} in incomplete_ids, got {goal_status.incomplete_ids!r}"
    )
