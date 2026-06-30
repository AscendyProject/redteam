# task-002 — Goal-mode E2E failure-path composition

## Context

The happy-path goal-mode E2E composition test
(`.redteam/tests/test_goal_mode_e2e.py`) already exists on `main`: it
provides the shared scaffolding — the temp-git-repo builder, the
stub-worker harness that drives `process_batch` without invoking any real
adapter, and the multi-task `goal.json` helper — plus the **happy-path**
assertions (parent-before-dependent ordering, stacking-pin of the
dependent on the parent branch, done-criterion completes when all tasks
are `done`).

This task extends that module with the **failure-path** assertions,
**reusing — not duplicating —** the existing helpers.

## Scope

Extend `.redteam/tests/test_goal_mode_e2e.py` (already on `main`) with the
**failure-path** assertions, reusing its helpers verbatim:

1. **Deferred / failed parent blocks descendants.** Build a two-task chain
   (one root + one dependent), make the stub worker report the parent as
   deferred / failed (rather than `done`), drive `process_batch`, and
   assert:
   - The dependent task is **never dispatched** (check the stub's recorded
     dispatch list).
   - The dependent's final state is `blocked_on_dependency` (mirror the
     transitive-cascade / blocked-skip assertions in
     `test_goal_dag_scheduler.py`).

2. **`ceilings.max_tasks` mismatch aborts before any seeding.** Build a
   `goal.json` whose declared `ceilings.max_tasks` does not match the number
   of tasks present in the manifest, drive `process_batch`, and assert:
   - The batch aborts (the orchestrator surfaces the manifest validation
     failure, in the same way `test_goal_ceilings_enforcement.py` /
     `test_goal_manifest_validation.py` expect).
   - **No task is seeded** and **no stub worker is dispatched** (the abort
     happens before any per-task work).

3. **Moved parent-tip / wrong-base reused branch fails closed.** Reproduce
   the stacking-pin invariant from `test_goal_stacked_branching.py` in the
   composed pipeline: when the dependent's reused branch points at a base
   that no longer matches the parent's current tip (i.e. the parent tip
   moved, or the branch was created from a wrong base), the run must fail
   closed rather than silently producing a wrong-stacked dependent.

## Constraints (do not relax)

- **Test-only.** Do not touch `.redteam/workflows/` or any other source
  directory. All changes stay in `.redteam/tests/test_goal_mode_e2e.py`.
- **No real model calls.** Continue to use the stub-worker harness already
  installed in the module; do not invoke any adapter.
- **Stdlib + pytest only.** No new third-party dependency.
- **Project-agnostic.** No project- or stack-specific fingerprints beyond
  what the existing goal-mode tests already assume.
- **Reuse the existing helpers verbatim.** Import / call the module-level
  scaffolding already in `test_goal_mode_e2e.py` (`_make_e2e_batch` /
  `_install_stub_workers` / `_simple_two_task_manifest` or whatever names
  it uses). Do **not** copy-paste the temp-git-repo or stub-worker setup
  into new helpers.
- **No CHANGELOG / docs / engine changes.** Strictly an extension of the
  existing test module.

## Non-goals

- Re-asserting the happy-path behaviours already covered.
- Refactoring the existing helpers beyond the minimum needed to accommodate
  the failure scenarios (e.g. allowing the stub to return non-`done`
  statuses if it hard-coded `done`). Such tweaks must stay backward-
  compatible with the existing happy-path tests.
- Adding new third-party tooling, new fixtures unrelated to these three
  failure modes, or unrelated coverage.

## Verification

- `bash .redteam/scripts/verify.sh` passes, with **both** the existing
  happy-path tests and the new failure-path tests in
  `test_goal_mode_e2e.py` green.
