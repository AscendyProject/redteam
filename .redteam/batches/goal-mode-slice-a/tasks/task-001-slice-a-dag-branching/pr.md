## What
A batch may declare an optional `goal.json` manifest describing a single-parent
forest of task dependencies; the orchestrator runs each dependent task **stacked
on its parent's task branch** so the reviewed range and the draft-PR base are
exactly its delta, with fail-closed guards that prevent a moved or stale parent
from producing a wrong stacked PR. When `goal.json` is absent, batch behavior is
byte-for-byte identical to today (flat, sorted, independent). No auto-merge.

## Why
Slice A of the goal-mode design (#94): give the harness an optional `goal.json`
manifest at the batch root so dependent tasks can be reviewed and PR'd as the
delta on top of their parent's task branch, instead of all tasks reviewing
against the project base. Security-adjacent because it extends the #91
`base_branch` pin to also cover parent-branch drift fail-closed — a moved/stale
parent must never silently produce a wrong stacked PR. Absent the manifest the
flat-batch behavior is unchanged, and the draft-PR stack remains the human
checkpoint (no auto-merge).

## Done-when
- [ ] `bash .redteam/scripts/verify.sh` passes (ruff check + ruff format --check + full pytest), with `test_agents_generic_prompts.py` and `test_install.py` green.
- [ ] An optional `goal.json` at `<batch-dir>/goal.json` is loaded and validated **inside `process_batch` before any task state is seeded or any task runs**; absent file → no manifest path taken, behavior unchanged.
- [ ] Manifest JSON is parsed via `json.loads(..., object_pairs_hook=...)` (or `json.JSONDecoder(object_pairs_hook=...)`) so that **duplicate `tasks.<id>` keys (and duplicate `depends_on` entries) are detected BEFORE Python's dict collapses them**; duplicate-key detection runs on the raw `(key, value)` pair stream, not on the post-parse dict.
- [ ] Manifest validation aborts the **whole** goal batch (fail closed, no task state seeded, no `process_task` call) on each of: unknown task ref in `depends_on`; self-dependency; duplicate task IDs (detected via the pairs hook above); a task key with no matching on-disk task dir; **`len(depends_on) >= 2`**; a dependency cycle (toposort back-edge); malformed JSON.
- [ ] Validation uses **stdlib only** (`json` + a hand-rolled toposort); no new non-stdlib imports are added to `orchestrator.py` or any new helper module.
- [ ] `ceilings` is parsed/stored on the manifest object but otherwise ignored in this slice (no enforcement code path reads it); a non-object `ceilings` value fails closed at validation, but the keys/values inside `ceilings` are accepted opaquely.
- [ ] When a manifest is present, `process_batch` runs tasks in topological layers: a task is dispatched only after its parent's `process_task` outcome is `"done"`; an independent root runs as today, in the existing `list_tasks` sort order within each layer.
- [ ] `TaskOutcome` (the `Literal` in `orchestrator.py`) is extended to add a new value `"blocked_on_dependency"`, alongside the existing `"done" | "blocked_on_human_gate" | "deferred" | "error"`. This is the chosen contract — NOT a `"deferred"` variant.
- [ ] A task whose parent's outcome is not `"done"` (`deferred` / `error` / `blocked_on_human_gate` / `blocked_on_dependency`) is **skipped** by the scheduler with the result string `"blocked_on_dependency"` recorded in the `process_batch` results dict; `process_task` is NOT invoked, and no writable phase runs for it. The skip cascades transitively through any deeper descendants in that chain.
- [ ] `_run_pipeline` reports `blocked_on_dependency` tasks distinctly from `blocked_on_human_gate` and `deferred`: it filters them out of `blocked` (so it does not return exit code 1 just because a chain was skipped) and prints an informational summary line naming the count (mirroring the existing `deferred` summary path).
- [ ] In `process_task`, the resolved base branch (parent's `<branch_prefix>/<parent_id>` for a dependent, else `cfg.project.base_branch`) is written to `state["base_branch"]` **before** `_ensure_task_branch` is invoked, and `_ensure_task_branch` is called with that pinned base (not a live config re-read).
- [ ] The scheduler passes the resolved parent (and the resolved-base / `base_is_parent` signal) into `process_task`; `process_task` does **not** re-read `goal.json` on its own.
- [ ] The `#91` pin-once + legacy-unpinned-with-writable-phase fail-closed invariant in `process_task` is preserved (`test_base_branch_pin.py` still passes; if the existing tests call `pinned_base_branch(state)` with one arg they are updated to pass a `repo` Path explicitly — the accessor signature requires it).
- [ ] For a dependent whose task branch already exists, the orchestrator runs `git merge-base --is-ancestor <pinned-base> <task-branch>` and **defers with a clear reason** (sets `last_failure_reason` and routes `next_phase = "deferred"`) when the branch does not descend from the pinned parent; the consumer's branch is never auto-deleted.
- [ ] At pin time, when the base is a parent task branch, `state["base_branch_sha"]` is recorded = parent branch tip SHA via a `git rev-parse` helper in `phase_runners/_base.py`. Root tasks (config base) record no `base_branch_sha`.
- [ ] `pinned_base_branch(state, repo)` (signature extended to require `repo: Path`) performs the freeze check inside the accessor: when `base_branch_sha` is recorded, it re-resolves the live parent tip and **raises (fails closed)** if the live SHA differs from the recorded SHA. Root tasks (no recorded SHA) → no-op, returns the pinned base unchanged.
- [ ] Every existing caller of `pinned_base_branch` (`orchestrator.py` line ~1119 tier-guard call, `phase_runners/create_pr.py` line 167, `phase_runners/review_code.py` line 43, `phase_runners/implement.py` lines 172/271/390, `phase_runners/write_test.py` line 102) passes `repo` explicitly; no caller is silently left on a one-arg call.
- [ ] The unconditional `git pull --ff-only origin <base>` in `_ensure_task_branch` (currently `orchestrator.py:777-782`) is **skipped** when the resolved base is a parent task branch, decided by an explicit `base_is_parent` argument threaded from `process_task` (which receives it from the scheduler), **not** by a `branch_prefix` string prefix check on `base_branch`.
- [ ] `create_pr` invokes the pr-author with the parent branch as `--base` for a dependent task (the same value the reviewer's `<base>...HEAD` range used).
- [ ] New tests under `.redteam/tests/` (see "Affected files") cover each invariant listed in "To be created" below and are runnable via `bash .redteam/scripts/verify.sh`.

## Verification
- Tests: `test_topo_layers_roots_first`, `test_topo_layers_multiple_roots_sorted`, `test_roots_run_first_dependents_after_parent_done`, `test_parent_deferred_blocks_child`, `test_parent_error_blocks_child`, `test_parent_blocked_on_human_gate_blocks_child`, `test_transitive_cascade`, `test_independent_chains_continue`, `test_dep_blocked_does_not_invoke_process_task`, `test_run_pipeline_dep_blocked_not_exit_1`, `test_run_pipeline_dep_blocked_summary_printed`, `test_dependent_task_receives_parent_branch_as_base`, `test_malformed_json_raises`, `test_duplicate_task_id_detected_pre_collapse`, `test_unknown_depends_on_ref_raises`, `test_self_dependency_raises`, `test_multi_parent_rejected`, `test_missing_task_dir_raises`, `test_cycle_detection_raises`, `test_ceilings_non_object_raises`, `test_valid_manifest_parses_ok`, `test_process_batch_invalid_manifest_no_state_seeded`, `test_process_batch_absent_manifest_flat_mode`, `test_existing_state_base_branch_mismatch_fails_closed`, `test_existing_state_missing_sha_fails_closed`, `test_base_branch_pinned_before_ensure_task_branch`, `test_base_branch_sha_recorded_at_pin_time`, `test_root_task_no_base_branch_sha`, `test_ancestry_check_defers_when_branch_wrong_base`, `test_ancestry_check_skipped_when_branch_does_not_exist`, `test_ensure_task_branch_pull_skipped_when_base_is_parent`, `test_ensure_task_branch_pull_issued_when_not_base_is_parent`, `test_create_pr_run_uses_pinned_base_from_state`, `test_freeze_guard_no_op_for_root_task`, `test_freeze_guard_no_op_when_sha_empty_string`, `test_freeze_guard_passes_when_sha_matches`, `test_freeze_guard_raises_when_sha_moved`, `test_freeze_guard_raises_when_rev_parse_fails`, `test_freeze_guard_triggered_from_review_code`, `test_freeze_guard_triggered_from_write_test`, `test_freeze_guard_triggered_from_create_pr`
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff covers the four Slice A surfaces: manifest schema + up-front validation in `process_batch`, layered scheduler with `blocked_on_dependency` cascade, pin-before-branch + ancestry guard in `process_task`, and centralized freeze guard inside `pinned_base_branch(state, repo)`.
- IR-001 (blocker, resolved): a dependent state whose `base_branch` no longer matches the scheduler-resolved parent, or whose `base_branch_sha` is missing, now fails closed in `process_task` instead of silently continuing on the stale pin.
- IR-002 (blocker, resolved): `verification.log` records `bash .redteam/scripts/verify.sh` finishing `[exit 0]` (ruff + ruff format --check + 350 pytest tests), and `state.json.verification.last_exit_code == 0`.
- IR-003 (major, resolved): the `create_pr` test now patches the worker adapter and invokes `create_pr_mod.run(...)`, asserting the captured prompt contains `--base redteam/task-parent` — discriminating against the pre-change one-arg `pinned_base_branch(state)` call.
- Boundary checks confirmed: duplicate task IDs caught via `object_pairs_hook` before dict collapse; cycle / unknown ref / self-dep / multi-parent / missing-dir all abort the whole batch before seeding; `_ensure_task_branch` pull-skip is decided on the explicit `base_is_parent` signal, not a branch-prefix string match; `_run_pipeline` separates `blocked_on_dependency` from `blocked_on_human_gate` and does not return exit 1 for a dependency-skipped chain.
- REVIEW_DECISION: APPROVED.

## Generated by
redteam / batch goal-mode-slice-a / task task-001-slice-a-dag-branching
