"""Regression tests for #136 — batch-root decompose artifact exemptions and
#117/#124 consistency via the shared _is_harness_artifact predicate.

Covers _floor_outside_scope (tracked-path floor) and _cross_run_trust_root_floor
(Check-1 live-surface + Check-2 stored-baseline contents):
- Batch-root decompose artifact exemption (one per basename)
- Non-allowlisted batch-root basename still trips
- Allowlisted basename in batch-root subdirectory still trips
- Sibling top-level input.md exempt; buried input.md not; cross-batch not
- Check-1: batch-root + sibling input.md exempt in live untracked surface
- Check-2: allowlisted stored-baseline contents pass (the #136 catch-22 fix)
- Adversarial: outside-scope non-allowlisted stored baseline still caught
- Default-path preservation: non-goal-mode task layout unchanged
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _impl():
    import _engine

    return _engine.implement()


_PROJ = SimpleNamespace(
    source_dirs=["app/"],
    test_dir="tests/",
    context_file="docs/ctx.md",
    base_branch="main",
)

_BATCH_ROOT_BASENAMES = ["goal.md", "goal.json", "decompose_review.md", "decompose_blocked.md"]


def _make_task_layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Return (cwd, batch_root, task_dir, sibling_dir).

    Layout:
      cwd/
        app/
        tests/
        .redteam/batches/b/        <- batch_root
          tasks/
            task-001/              <- sibling_dir
            task-002/              <- task_dir (current task)
    """
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "app").mkdir()
    (cwd / "tests").mkdir()
    batch_root = cwd / ".redteam" / "batches" / "b"
    batch_root.mkdir(parents=True)
    tasks_dir = batch_root / "tasks"
    tasks_dir.mkdir()
    sibling_dir = tasks_dir / "task-001"
    sibling_dir.mkdir()
    task_dir = tasks_dir / "task-002"
    task_dir.mkdir()
    return cwd, batch_root, task_dir, sibling_dir


# =============================================================================
# 1. _floor_outside_scope: batch-root decompose artifact exempt (one per basename)
# =============================================================================


@pytest.mark.parametrize("basename", _BATCH_ROOT_BASENAMES)
def test_floor_batch_root_decompose_artifact_exempt(tmp_path, basename):
    """Tracked batch-root decompose artifact does NOT trip _floor_outside_scope."""
    impl = _impl()
    cwd, batch_root, task_dir, _sib = _make_task_layout(tmp_path)

    artifact_posix = (batch_root / basename).relative_to(cwd).as_posix()
    result = impl._floor_outside_scope({artifact_posix}, _PROJ, task_dir, cwd)

    assert result == set(), f"{basename} at batch root should be exempt, got {result}"


# =============================================================================
# 2. _floor_outside_scope: non-allowlisted batch-root basename still trips
# =============================================================================


@pytest.mark.parametrize("bad_basename", ["notes.md", "secrets.env", "README.md"])
def test_floor_batch_root_non_allowlisted_still_trips(tmp_path, bad_basename):
    """Non-allowlisted basename at batch root still trips _floor_outside_scope."""
    impl = _impl()
    cwd, batch_root, task_dir, _sib = _make_task_layout(tmp_path)

    artifact_posix = (batch_root / bad_basename).relative_to(cwd).as_posix()
    result = impl._floor_outside_scope({artifact_posix}, _PROJ, task_dir, cwd)

    assert artifact_posix in result, f"{bad_basename} at batch root should still trip floor"


# =============================================================================
# 3. _floor_outside_scope: allowlisted basename in batch-root subdirectory still trips
# =============================================================================


@pytest.mark.parametrize("basename", _BATCH_ROOT_BASENAMES)
def test_floor_batch_root_allowlisted_in_subdir_still_trips(tmp_path, basename):
    """Allowlisted basename buried in a batch-root subdirectory still trips floor."""
    impl = _impl()
    cwd, batch_root, task_dir, _sib = _make_task_layout(tmp_path)

    subdir = batch_root / "subdir"
    subdir.mkdir()
    artifact_posix = (subdir / basename).relative_to(cwd).as_posix()
    result = impl._floor_outside_scope({artifact_posix}, _PROJ, task_dir, cwd)

    assert artifact_posix in result, f"{basename} in batch-root subdir should still trip floor"


# =============================================================================
# 4. _floor_outside_scope: sibling top-level input.md exempt; buried / cross-batch not
# =============================================================================


def test_floor_sibling_input_md_exempt(tmp_path):
    """Sibling task top-level input.md does NOT trip _floor_outside_scope."""
    impl = _impl()
    cwd, _br, task_dir, sibling_dir = _make_task_layout(tmp_path)

    artifact_posix = (sibling_dir / "input.md").relative_to(cwd).as_posix()
    result = impl._floor_outside_scope({artifact_posix}, _PROJ, task_dir, cwd)

    assert result == set(), f"sibling top-level input.md should be exempt, got {result}"


def test_floor_sibling_buried_input_md_still_trips(tmp_path):
    """input.md buried in a sibling subdir still trips _floor_outside_scope."""
    impl = _impl()
    cwd, _br, task_dir, sibling_dir = _make_task_layout(tmp_path)

    subdir = sibling_dir / "subdir"
    subdir.mkdir()
    artifact_posix = (subdir / "input.md").relative_to(cwd).as_posix()
    result = impl._floor_outside_scope({artifact_posix}, _PROJ, task_dir, cwd)

    assert artifact_posix in result, "buried sibling input.md should still trip floor"


def test_floor_cross_batch_input_md_still_trips(tmp_path):
    """input.md under a different batch's tasks/ root still trips _floor_outside_scope."""
    impl = _impl()
    cwd, _br, task_dir, _sib = _make_task_layout(tmp_path)

    other_task = cwd / ".redteam" / "batches" / "other-batch" / "tasks" / "task-001"
    other_task.mkdir(parents=True)
    artifact_posix = (other_task / "input.md").relative_to(cwd).as_posix()
    result = impl._floor_outside_scope({artifact_posix}, _PROJ, task_dir, cwd)

    assert artifact_posix in result, "cross-batch input.md should still trip floor"


# =============================================================================
# 5. _cross_run_trust_root_floor Check-1: batch-root + sibling input.md exempt
# =============================================================================


def test_cross_run_check1_batch_root_and_sibling_input_md_exempt(tmp_path):
    """Check-1: current_untracked with only batch-root allowlisted basenames and
    a sibling top-level input.md returns no offending paths."""
    impl = _impl()
    cwd, batch_root, task_dir, sibling_dir = _make_task_layout(tmp_path)

    goal_posix = (batch_root / "goal.md").relative_to(cwd).as_posix()
    sibling_input_posix = (sibling_dir / "input.md").relative_to(cwd).as_posix()

    state: dict = {}
    result = impl._cross_run_trust_root_floor(state, task_dir, cwd, _PROJ, {goal_posix, sibling_input_posix})

    assert result == set(), f"batch-root + sibling input.md should be exempt in Check-1, got {result}"


# =============================================================================
# 6. _cross_run_trust_root_floor Check-2: allowlisted stored-baseline contents exempt
# =============================================================================


def test_cross_run_check2_allowlisted_stored_baselines_exempt(tmp_path):
    """Check-2: stored baselines with only batch-root allowlisted, sibling top-level
    allowlisted, and in-scope paths → no offending paths (the #136 catch-22 fix)."""
    impl = _impl()
    cwd, batch_root, task_dir, sibling_dir = _make_task_layout(tmp_path)

    goal_json_posix = (batch_root / "goal.json").relative_to(cwd).as_posix()
    decompose_posix = (batch_root / "decompose_review.md").relative_to(cwd).as_posix()
    sibling_input_posix = (sibling_dir / "input.md").relative_to(cwd).as_posix()
    sibling_state_posix = (sibling_dir / "state.json").relative_to(cwd).as_posix()
    in_scope_posix = "app/module.py"

    state = {
        "implement_untracked_baseline": [goal_json_posix, sibling_input_posix, sibling_state_posix, in_scope_posix],
        "implement_tracked_baseline": [decompose_posix],
    }
    result = impl._cross_run_trust_root_floor(state, task_dir, cwd, _PROJ, set())

    assert result == set(), f"allowlisted stored baseline contents should be exempt, got {result}"


# =============================================================================
# 7. _cross_run_trust_root_floor: adversarial baseline still caught (security boundary)
# =============================================================================


def test_cross_run_adversarial_baseline_still_caught(tmp_path):
    """Security-boundary regression: outside-scope non-allowlisted path in stored
    baseline is still caught (adversarial baseline rewrite guard)."""
    impl = _impl()
    cwd, _br, task_dir, _sib = _make_task_layout(tmp_path)

    state = {"implement_untracked_baseline": ["scratch/leaked.txt"]}
    result = impl._cross_run_trust_root_floor(state, task_dir, cwd, _PROJ, set())

    assert "scratch/leaked.txt" in result, "adversarial baseline entry must still be caught"


# =============================================================================
# 8. Default-path preservation: non-goal-mode task (no siblings, no batch-root artifacts)
# =============================================================================


def test_floor_default_path_non_goal_mode_unchanged(tmp_path):
    """For a task with no sibling task dirs and no batch-root decompose artifacts,
    both floors behave identically to before — outside-scope paths still trip."""
    impl = _impl()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "app").mkdir()
    (cwd / "tests").mkdir()
    task_dir = cwd / ".redteam" / "batches" / "simple" / "tasks" / "task-001"
    task_dir.mkdir(parents=True)

    outside_path = "README.md"

    # _floor_outside_scope: outside-scope tracked path still trips.
    result_floor = impl._floor_outside_scope({outside_path}, _PROJ, task_dir, cwd)
    assert outside_path in result_floor, "_floor_outside_scope: outside-scope path should still trip"

    # _cross_run_trust_root_floor: outside-scope in live surface + stored baseline still trips.
    state = {"implement_untracked_baseline": [outside_path]}
    result_cross = impl._cross_run_trust_root_floor(state, task_dir, cwd, _PROJ, {outside_path})
    assert outside_path in result_cross, "_cross_run_trust_root_floor: outside-scope path should still trip"
