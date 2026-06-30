## What
Add a single integration test module that drives a real multi-task `goal.json`
through `orchestrator.process_batch`, composing manifest validation → DAG
schedule → stacking-pin → done-criterion as one unit on the happy path, and
exposes the shared scaffolding (temp-repo builder, stub-worker harness, minimal
manifest helper) that task-002 will import from the same module to add the
failure-path assertions.

## Why
Goal mode shipped as three independently unit-tested slices (manifest+DAG,
decomposer, ceilings+done-criterion) but no single test composes them end-to-end
through `process_batch`. The human asked for an integration test that exercises
manifest validation → schedule → stacking-pin → done-criterion as one unit on
the happy path, and that owns the shared scaffolding (temp-batch builder,
stub-worker harness, minimal manifest helper) so the follow-up task-002 can
import the same helpers to add the failure-path assertions without re-inventing
the fixture layer.

## Done-when
- [ ] New file `.redteam/tests/test_goal_mode_e2e.py` exists and is collected
      by `pytest` under `.redteam/tests/`.
- [ ] The new module defines three module-level helpers, importable by name
      from `test_goal_mode_e2e`: `_make_e2e_batch`, `_install_stub_workers`,
      `_simple_two_task_manifest`.
- [ ] `_simple_two_task_manifest()` returns a JSON string describing exactly
      one root task and one dependent task with `depends_on` pointing at the
      root (single-parent chain), parseable by `orchestrator._load_goal_manifest`.
- [ ] `_make_e2e_batch(tmp_path)` returns a `pathlib.Path` to a batch dir under
      `tmp_path` containing `tasks/<id>/input.md` for each task in the manifest
      plus a top-level `goal.json` written from `_simple_two_task_manifest()`
      (mirroring the `_make_batch` pattern in `test_goal_dag_scheduler.py` /
      `test_goal_done_criterion.py`).
- [ ] `_install_stub_workers(monkeypatch, orch)` monkeypatches `orch._seed_state`
      and `orch.process_task` so the scheduler / manifest-validation /
      done-criterion paths execute without invoking any real adapter or running
      any phase runner, AND records (a) the order task IDs were dispatched and
      (b) for each dispatched task the `resolved_base` and `base_is_parent`
      keyword arguments it was called with. The recorded data is reachable
      from the test body for assertions.
- [ ] A happy-path test in the same module asserts the dependent task's
      dispatch index is strictly greater than the parent task's dispatch index
      in the recorded order (parent runs before dependent).
- [ ] A happy-path test asserts that for the dependent task the stub captured
      `base_is_parent is True` and `resolved_base` equals
      `f"{branch_prefix}/{parent_task_id}"` where `branch_prefix` comes from
      the config the test seeds in the tmp batch (same convention used in
      `test_goal_dag_scheduler.test_dependent_task_receives_parent_branch_as_base`
      and `test_goal_stacked_branching.py`), and for the root task
      `base_is_parent is False`.
- [ ] A happy-path test drives `orch._run_batch(batch_dir)` (or equivalent)
      with all stubbed tasks returning `"done"` and asserts the returned
      `GoalStatus` has `complete is True`, `done_count == total == 2`, and
      `incomplete_ids == ()` (mirrors the assertions in
      `test_goal_done_criterion.test_run_batch_manifest_ran_returns_goal_status`).
- [ ] `bash .redteam/scripts/verify.sh` exits 0 with the new tests included.
- [ ] No files under `.redteam/workflows/`, `.redteam/config.toml`,
      `pyproject.toml`, `requirements*.txt`, or any non-test path are modified
      by this task.

## Verification
- Tests: `test_dispatch_ordering`, `test_stacking_pin_contract`, `test_done_criterion_complete`, `test_shared_helper_smoke`
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff is scoped to the single requested test-only file
  (`.redteam/tests/test_goal_mode_e2e.py`); no engine, adapter, installer,
  config, or dependency file is touched.
- All three required shared helpers (`_simple_two_task_manifest`,
  `_make_e2e_batch`, `_install_stub_workers`) are present at module scope and
  match the importable-by-task-002 contract pinned in `outcome.md`.
- Stub harness records dispatch order plus `resolved_base` / `base_is_parent`
  per task and stubs both `_seed_state` and `process_task`, so the composed
  scheduler path runs without invoking any real adapter or phase runner.
- Happy-path assertions cover parent-before-dependent ordering, the
  stacking-pin kwargs (`base_is_parent=True`, `resolved_base="redteam/{parent}"`),
  and `_run_batch`'s `GoalStatus(complete=True, done_count==total==2,
  incomplete_ids==())` — discriminating against the regressions the test was
  written to catch.
- Security checklist clean: no shell execution, reviewer write-capability,
  credential/logging, installer deletion, runtime dependency, or
  project-specific engine fingerprint changes.
- `REVIEW_DECISION: APPROVED` (reviewer relied on the recorded
  `verification.last_exit_code == 0` and `540 passed` in `verification.log`
  rather than re-running the gate from a read-only sandbox).

## Generated by
redteam / batch goal-mode-e2e / task task-001-happy-path
