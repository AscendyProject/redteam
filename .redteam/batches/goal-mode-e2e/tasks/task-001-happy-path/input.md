# task-001 — Goal-mode E2E happy-path composition (owns shared scaffolding)

## Context (inherited from goal-mode-e2e)

Goal mode shipped as three independently **unit-tested** slices:

- Slice A — `goal.json` DAG manifest + task-on-task branching
  (see `.redteam/tests/test_goal_manifest_validation.py`,
  `test_goal_dag_scheduler.py`, `test_goal_stacked_branching.py`).
- Slice B — decomposer (see `test_goal_decomposer.py`).
- Slice C — ceilings + done-criterion (see `test_goal_ceilings_enforcement.py`,
  `test_goal_done_criterion.py`).

There is no single integration test that drives the slices *composed together*:
a real `goal.json` flowing through `process_batch`'s layered scheduler, with
manifest validation → schedule → stacking-pin → ceilings → done-criterion
exercised as one unit. This task opens that gap by creating the integration
test module and the shared scaffolding it (and task-002) will use, and
populating it with the **happy-path** assertions.

## Scope

Create a new test module — suggested path
`.redteam/tests/test_goal_mode_e2e.py` — that:

1. **Owns the shared scaffolding** that task-002 (failure-path) will reuse:
   - A helper that builds a `tmp_path` git repo wired enough for
     `process_batch` to run (mirror the setup pattern used by the existing
     `.redteam/tests/test_goal_dag_scheduler.py` / `test_goal_stacked_branching.py`
     fixtures — e.g. the `_orch()` import shim, `_make_batch` writing
     `tasks/<id>/input.md` files plus a top-level `goal.json`, and any
     git-init the existing tests do).
   - A stub-worker harness: monkeypatch the phase dispatch (e.g. the
     orchestrator's `process_task` / `_seed_state` or the underlying phase
     runners) so the scheduler / manifest validation / ceiling / done-criterion
     code paths run **without invoking any real adapter**. The stub should
     record the order tasks were dispatched and the `resolved_base` /
     `base_is_parent` arguments it was called with, so the assertions can
     inspect them.
   - A helper that emits a minimal multi-task `goal.json` (one root + one
     dependent, single-parent) for the happy path.

2. **Happy-path assertions** (one or more focused tests in the same module):
   - The dependent task is scheduled **only after** its parent reports `done`
     (assert against the recorded dispatch order).
   - When the dependent runs, its **base is pinned to the parent's branch**
     (the stacking-pin from Slice A — assert against the `resolved_base` /
     `base_is_parent` the stub captured, matching the convention in
     `test_goal_stacked_branching.py`).
   - The goal **done-criterion reports complete** once all tasks are `done`
     (mirror the assertions in `test_goal_done_criterion.py` over the final
     `state.json` / orchestrator return).

3. Keep the shared helpers **module-level and named clearly** (e.g.
   `_make_e2e_batch`, `_install_stub_workers`, `_simple_two_task_manifest`),
   because task-002 will import / reuse them directly from the same module —
   not re-implement them.

## Constraints (inherited — do not relax)

- **Test-only.** Do not touch `.redteam/workflows/` or any other source
  directory. The deliverable is the new test module under `.redteam/tests/`.
- **No real model calls.** All worker / phase dispatch must be stubbed via
  monkeypatch; the scenario runs in a `tmp_path` git repo.
- **Stdlib + pytest only.** No new third-party dependency. No additions to
  `pyproject.toml` / `requirements*.txt`.
- **Project-agnostic.** No project- or stack-specific fingerprints in the test
  beyond what the existing goal-mode tests already assume.
- **Reuse, don't reinvent.** Mirror the fixture / helper patterns already used
  by `test_goal_manifest_validation.py`, `test_goal_dag_scheduler.py`,
  `test_goal_ceilings_enforcement.py`, `test_goal_done_criterion.py`, and
  `test_goal_stacked_branching.py` (the `_orch()` shim, `_make_batch`-style
  builder, `_simple_manifest`-style JSON helper, etc.).
- **No CHANGELOG / docs / engine changes.** This is strictly the new test
  module.

## Non-goals

- Adding failure-path assertions (deferred parent → `blocked_on_dependency`,
  `ceilings.max_tasks` mismatch abort, moved parent-tip / wrong-base fail-
  closed). Those belong to task-002, which depends on this task's
  scaffolding and adds them in the same module.
- Refactoring any of the existing slice-level goal-mode tests.
- Introducing new abstractions, fixtures, or helpers beyond what task-001
  and task-002 directly need.

## Verification

- `bash .redteam/scripts/verify.sh` (the project's gate — `ruff` + `pytest`
  over `.redteam/`) passes, with the new happy-path tests in
  `test_goal_mode_e2e.py` passing.
