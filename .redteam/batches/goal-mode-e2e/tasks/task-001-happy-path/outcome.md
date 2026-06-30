# Outcome — Goal-mode E2E happy-path composition (owns shared scaffolding)

## Goal
Add a single integration test module that drives a real multi-task `goal.json`
through `orchestrator.process_batch`, composing manifest validation → DAG
schedule → stacking-pin → done-criterion as one unit on the happy path, and
exposes the shared scaffolding (temp-repo builder, stub-worker harness, minimal
manifest helper) that task-002 will import from the same module to add the
failure-path assertions.

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

## Out of scope
- Failure-path assertions (deferred parent → `blocked_on_dependency`,
  `ceilings.max_tasks` mismatch abort, moved parent-tip / wrong-base fail-
  closed). Those belong to task-002, which reuses the helpers from this module.
- Any change under `.redteam/workflows/` (engine source) or to any other
  existing test file.
- New abstractions, fixtures, plugins, or `conftest.py` additions beyond the
  three named module-level helpers required by task-002.
- Adding any third-party dependency (must be stdlib + pytest only).
- Refactoring or "tidying" the existing goal-mode slice tests.
- Modifying `CHANGELOG.md`, `README.md`, `docs/`, or any other documentation.

## Affected files
- `(new) .redteam/tests/test_goal_mode_e2e.py` — the new integration test
  module; owns the shared scaffolding (`_make_e2e_batch`,
  `_install_stub_workers`, `_simple_two_task_manifest`) and the happy-path
  assertions.

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### Notes
- `bash .redteam/scripts/verify.sh` is the project verify command from
  `.redteam/config.toml [project] verify_command` (ruff check +
  `ruff format --check` + full pytest over `.redteam/`). It is the single
  command the orchestrator snapshots and runs at the verification step;
  every existing slice-level goal-mode test below is part of that pytest run,
  so the one verify command covers them all alongside the new module.

### Existing (must continue to pass under the verify run)
- `.redteam/tests/test_goal_dag_scheduler.py` — DAG schedule + stacking-pin
  contract this module composes.
- `.redteam/tests/test_goal_stacked_branching.py` — stacking-pin convention
  the happy-path assertion mirrors.
- `.redteam/tests/test_goal_done_criterion.py` — `GoalStatus` return shape
  the happy-path assertion mirrors.
- `.redteam/tests/test_goal_manifest_validation.py` — manifest validation
  that runs inside `process_batch`.
- `.redteam/tests/test_goal_ceilings_enforcement.py` — ceilings path that
  also runs inside `process_batch`.
- `.redteam/tests/test_goal_decomposer.py` — decomposer slice (unrelated to
  the composed path here, but must stay green under the same verify run).

### To be created (the test-writing phase — here, the agent-pair implementer — defines exact test names)
Tests in `.redteam/tests/test_goal_mode_e2e.py` must cover, on a two-task
single-parent manifest driven through `orchestrator.process_batch` /
`_run_batch` with stubbed `_seed_state` + `process_task`:
- **Dispatch ordering** — the dependent task is dispatched strictly after the
  parent reports `"done"` (assert against the recorded dispatch-order list).
- **Stacking-pin contract** — the dependent's stubbed `process_task` receives
  `base_is_parent=True` and `resolved_base == f"{branch_prefix}/{parent_id}"`;
  the root receives `base_is_parent=False`. Setup mirrors
  `test_goal_dag_scheduler.test_dependent_task_receives_parent_branch_as_base`
  (seed `.redteam/config.toml` in the tmp batch dir, monkeypatch `orch.repo_root`).
- **Done-criterion** — with all stubbed tasks returning `"done"`, the
  entry point that surfaces `GoalStatus` (currently `_run_batch`) reports
  `complete is True`, `done_count == total == 2`, `incomplete_ids == ()`
  (mirrors `test_goal_done_criterion.test_run_batch_manifest_ran_returns_goal_status`).
- **Shared-helper smoke** — `_make_e2e_batch`, `_install_stub_workers`, and
  `_simple_two_task_manifest` are present at module scope and callable, so
  task-002 can rely on them as the shared API.

## Risks
- The brief lists `_make_e2e_batch`, `_install_stub_workers`, and
  `_simple_two_task_manifest` as the helper names task-002 will import; these
  names are pinned in `Done-when` so task-002's import contract is stable. If
  the implementer prefers different names, that breaks task-002 and must come
  back through replanning, not be renamed silently.
- The `resolved_base` value is constructed as `f"{branch_prefix}/{parent_id}"`
  per the convention in `test_goal_dag_scheduler.test_dependent_task_receives_parent_branch_as_base`,
  which requires the test to seed a minimal `.redteam/config.toml` inside the
  tmp batch dir AND monkeypatch `orch.repo_root` to point at it. The
  implementer must mirror that setup exactly; deviating would either skip the
  pin assertion or invent a parallel config-loading shape.
- The brief allows monkeypatching either `process_task` (the path taken by the
  existing dag-scheduler / done-criterion tests) or the underlying phase
  runners. The existing pattern is `process_task`; this outcome assumes that
  path for the stub harness. If the implementer instead stubs phase runners,
  the `_install_stub_workers` recorder contract still must capture
  `resolved_base` / `base_is_parent` per dispatched task, which is non-trivial
  below the `process_task` boundary — flag back if attempting it.
- Naming the file `test_goal_mode_e2e.py` is a suggestion in the brief, not a
  hard contract; this outcome pins it because task-002's `input.md` already
  references that module name. Changing the path requires updating task-002's
  brief, not just this file.
