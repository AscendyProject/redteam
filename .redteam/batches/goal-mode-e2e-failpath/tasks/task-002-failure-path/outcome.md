# Outcome — Goal-mode E2E failure-path composition

## Goal
Extend the existing module `.redteam/tests/test_goal_mode_e2e.py` with failure-path
assertions for the goal-mode composed pipeline (parent-blocks-descendant,
`ceilings.max_tasks` mismatch aborts pre-seeding, and wrong-base /
moved-parent-tip fails closed through the **real** `process_task`), reusing the
module's existing helpers verbatim and adding no engine changes.

## Done-when
- [ ] `.redteam/tests/test_goal_mode_e2e.py` contains failure-path test(s)
      asserting that when the parent task's stub returns a non-`done` status
      (`deferred` or `error`), the dependent task is **never appended to the
      stub's `dispatch_order`** AND `process_batch`'s returned results dict has
      `results[<dependent_id>] == "blocked_on_dependency"` (mirroring
      `test_goal_dag_scheduler.test_parent_deferred_blocks_child` /
      `test_parent_error_blocks_child` /
      `test_dep_blocked_does_not_invoke_process_task`).
- [ ] `.redteam/tests/test_goal_mode_e2e.py` contains failure-path test(s)
      asserting that a `goal.json` whose `ceilings.max_tasks` does not match the
      task count causes `process_batch` to abort fail-closed: the stub's
      `dispatch_order` is empty, every entry in the returned results dict starts
      with `"error:"`, and no `state.json` is written under any task dir
      (mirroring
      `test_goal_ceilings_enforcement.test_task_count_exceeds_ceiling_process_batch_aborts_fail_closed`
      and `test_goal_manifest_validation.test_process_batch_invalid_manifest_no_state_seeded`).
- [ ] `.redteam/tests/test_goal_mode_e2e.py` contains a failure-path test that
      drives the **real** `orch.process_task` for the dependent task
      (i.e. `process_task` is NOT stubbed for the dependent), with only the git
      boundary mocked (`orch.subprocess.run` and/or `orch.git_rev_parse`,
      following the patterns in
      `test_goal_stacked_branching.test_existing_state_base_branch_mismatch_fails_closed`
      and `test_ancestry_check_defers_when_branch_wrong_base`). The parent's
      `process_task` MAY be stubbed to return `"done"` so the cascade reaches
      the dependent, but the dependent itself must flow through the real
      `process_task` so that the stacking-pin invariant inside
      `orchestrator.process_task` (the `base_branch_mismatch` branch around
      `orchestrator.py:1070` AND/OR the
      `dependent_branch_not_descended_from_parent` branch around
      `orchestrator.py:1109`) is the path that produces the failure. The test
      asserts:
        - The dependent's result is `"error"` (for `base_branch_mismatch`) or
          `"deferred"` (for `dependent_branch_not_descended_from_parent`) —
          not `"done"`, and not `"blocked_on_dependency"`.
        - The dependent's `state.json` has `last_failure_reason` equal to
          `"base_branch_mismatch"` or
          `"dependent_branch_not_descended_from_parent"` (whichever path the
          test arranges).
        - When the test invokes `orch._run_batch`, the returned `GoalStatus`
          has `complete is False` and `<dependent_id>` appears in
          `incomplete_ids`.
- [ ] The new failure-path tests **reuse** the module-level helpers
      `_simple_two_task_manifest`, `_make_e2e_batch`, and `_install_stub_workers`
      (or backward-compatible tweaks of them — see the next item); they do not
      copy-paste the temp-git-repo or stub-worker setup. The contract guarded
      by the existing `test_shared_helper_smoke` keeps passing (helpers remain
      module-level and callable; the two-task single-parent chain shape is
      unchanged).
- [ ] If `_install_stub_workers` is widened (e.g. to accept per-task return
      statuses, or to stub only a subset of task IDs so the dependent can fall
      through to the real `orch.process_task`), the widening is backward
      compatible: the four existing happy-path tests (`test_dispatch_ordering`,
      `test_stacking_pin_contract`, `test_done_criterion_complete`,
      `test_shared_helper_smoke`) continue to pass without modification to
      their call sites.
- [ ] No file outside `.redteam/tests/test_goal_mode_e2e.py` is modified.
      Specifically, nothing under `.redteam/workflows/`, no other test file, no
      docs, no templates, no agent skeletons, no `CHANGELOG.md`, no `README.md`.
- [ ] `bash .redteam/scripts/verify.sh` passes (ruff + the full pytest suite
      over `.redteam/`, including both the pre-existing happy-path tests and
      the new failure-path tests in `test_goal_mode_e2e.py`).

## Out of scope
- Any modification under `.redteam/workflows/` (engine source). This task is
  test-only.
- Re-asserting happy-path behaviors already covered by the four existing tests
  in `test_goal_mode_e2e.py` (`test_dispatch_ordering`,
  `test_stacking_pin_contract`, `test_done_criterion_complete`,
  `test_shared_helper_smoke`).
- Invoking any real worker / reviewer adapter, any real model call, any real
  network or `gh` call. The stub-worker harness is mandatory for everything
  except the dependent's `process_task` in scenario 3 (which runs the real
  engine code with only the git subprocess boundary mocked).
- Adding a new third-party dependency (stdlib + pytest only).
- New fixtures or coverage unrelated to the three named failure modes.
- Changes to `CHANGELOG.md`, `README.md`, `docs/`, agent skeletons, prompts,
  templates, or any other file outside `.redteam/tests/test_goal_mode_e2e.py`.
- Renaming, deleting, or re-homing the module-level helpers in
  `test_goal_mode_e2e.py` (`_simple_two_task_manifest`, `_make_e2e_batch`,
  `_install_stub_workers`, `_PARENT_ID`, `_DEPENDENT_ID`) — they are an
  import contract.
- Exercising both `base_branch_mismatch` AND
  `dependent_branch_not_descended_from_parent` in the same task. The brief
  asks for **one** wrong-base / moved-parent-tip scenario in the composed
  pipeline; either failure path satisfies it. (Adding the second is allowed
  but not required.)

## Affected files
- `.redteam/tests/test_goal_mode_e2e.py` — extend with three failure-path
  tests covering the scenarios above; if and only if needed, make a
  backward-compatible widening of `_install_stub_workers` so the stub can
  return non-`done` per-task statuses and/or so a specific task (the
  dependent in scenario 3) can fall through to the real `orch.process_task`.
  The four existing happy-path tests must still pass without changes to their
  call sites.

## Verification

```yaml
commands:
  - "bash .redteam/scripts/verify.sh"
```

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full ruff + pytest suite over `.redteam/`.
- `python3 -m pytest .redteam/tests/test_goal_mode_e2e.py -q` — the existing
  happy-path tests in the same module (`test_dispatch_ordering`,
  `test_stacking_pin_contract`, `test_done_criterion_complete`,
  `test_shared_helper_smoke`) must remain green after the extension.
- `python3 -m pytest .redteam/tests/test_goal_dag_scheduler.py .redteam/tests/test_goal_manifest_validation.py .redteam/tests/test_goal_ceilings_enforcement.py .redteam/tests/test_goal_stacked_branching.py -q` —
  the unit-level fail-closed tests the new E2E tests mirror; these must not
  regress.

### To be created (the test-writing phase will define exact test names)
- New tests inside `.redteam/tests/test_goal_mode_e2e.py` covering:
  1. **Parent-deferred / parent-error blocks descendant in the composed
     pipeline.** Two-task single-parent chain via `_make_e2e_batch` +
     `_simple_two_task_manifest`. Stub configured so the parent returns a
     non-`done` status (`deferred` and/or `error`). After `process_batch`,
     assert the dependent ID is absent from the stub's recorded
     `dispatch_order` AND `results[<dependent_id>] == "blocked_on_dependency"`.
  2. **`ceilings.max_tasks` mismatch aborts before any seeding.** Write a
     `goal.json` whose `ceilings.max_tasks` does not equal the number of tasks
     in the manifest, then call `process_batch`. Assert the stub's
     `dispatch_order` is empty, every value in the returned results dict
     starts with `"error:"`, and no `state.json` was created under any task
     dir.
  3. **Wrong-base / moved-parent-tip dependent fails closed through the real
     `process_task`.** Drive a two-task chain through `process_batch` /
     `_run_batch` such that the dependent's `process_task` is the **real**
     engine code (not the stub), the parent is stubbed to return `"done"`,
     and the dependent's pre-seeded `state.json` (or the git mock) makes the
     stacking-pin invariant fail closed inside `orchestrator.process_task`.
     Only the git boundary (`orch.subprocess.run` and/or
     `orch.git_rev_parse`) is mocked, following the patterns in
     `test_goal_stacked_branching.test_existing_state_base_branch_mismatch_fails_closed`
     and `test_ancestry_check_defers_when_branch_wrong_base`. Assert the
     dependent's result is `"error"` or `"deferred"` (not `"done"`, not
     `"blocked_on_dependency"`), the dependent's persisted
     `last_failure_reason` is `"base_branch_mismatch"` or
     `"dependent_branch_not_descended_from_parent"`, and the `GoalStatus`
     returned by `_run_batch` has `complete is False` with `<dependent_id>`
     in `incomplete_ids`.

## Risks
- The current `_install_stub_workers` hard-codes every stub to return `"done"`
  and stubs `process_task` for ALL tasks. Scenarios 1 and 3 therefore need a
  backward-compatible widening (per-task return statuses for scenario 1, and a
  way to leave the dependent's `process_task` unstubbed for scenario 3) — or
  a sibling helper. The brief explicitly permits the former ("such tweaks must
  stay backward-compatible with the existing happy-path tests"). The
  implementer must pick one path and keep the four existing happy-path tests
  passing without changes to their call sites.
- Scenario 3 chooses between two real-engine failure branches:
  (a) **`base_branch_mismatch`** — pre-seed the dependent's `state.json` with
  `base_branch="main"` (a stale flat base) and `base_branch_sha="sha_old"`,
  so the `elif base_is_parent:` branch around `orchestrator.py:1070` returns
  `"error"` immediately; or
  (b) **`dependent_branch_not_descended_from_parent`** — mock
  `orch.subprocess.run` so the `git rev-parse --verify <task_branch>`
  succeeds and the `git merge-base --is-ancestor` returns non-zero, so the
  ancestry check around `orchestrator.py:1109` returns `"deferred"`.
  Either satisfies the brief; option (a) is the lighter-touch reproduction of
  the moved-parent-tip case (the dependent's pinned base no longer matches
  what the scheduler resolves). The implementer picks one and the test
  comment must name which `last_failure_reason` it asserts.
- The brief calls the helpers "_make_e2e_batch / _install_stub_workers /
  _simple_two_task_manifest **or whatever names it uses**" — those are the
  exact names actually present in `test_goal_mode_e2e.py` today (verified by
  reading the file). No rename is needed.
- `_run_batch` is the only path that returns a `GoalStatus`; `process_batch`
  discards it. The `GoalStatus.complete is False` assertion in scenario 3
  therefore must call `orch._run_batch` directly (the existing
  `test_done_criterion_complete` shows the pattern).
- The agent-definition section header is `## Verification hooks`, while the
  Codex `plan_review` rubric requires a `## Verification` section with a
  fenced `yaml` block. This outcome satisfies the rubric (the canonical
  contract the reviewer enforces) and keeps the existing/to-be-created
  subsections nested underneath as `### Existing` / `### To be created`.
