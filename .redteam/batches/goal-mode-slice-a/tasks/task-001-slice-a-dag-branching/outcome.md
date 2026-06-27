# Outcome — Slice A: single-parent DAG manifest + task-on-task branching

## Goal
A batch may declare an optional `goal.json` manifest describing a single-parent
forest of task dependencies; the orchestrator runs each dependent task **stacked
on its parent's task branch** so the reviewed range and the draft-PR base are
exactly its delta, with fail-closed guards that prevent a moved or stale parent
from producing a wrong stacked PR. When `goal.json` is absent, batch behavior is
byte-for-byte identical to today (flat, sorted, independent). No auto-merge.

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

## Out of scope
- The goal → task **decomposer** and decomposition review (Slice B).
- **Ceilings enforcement** and goal-level done-criterion (Slice C); `ceilings` is parsed-and-ignored only.
- **Multi-parent / integration-branch** DAGs; any `depends_on` with ≥2 entries fails closed in v1.
- Changing the standalone `review` command (`cmd_review`) or any non-pipeline consumer.
- Auto-retarget of stacked PRs after a bottom-up merge (operator-manual in v1).
- Auto-merge or auto-delete of consumer branches; force-push.
- Engine-wide `[goal]` config surface in `config.toml` (the manifest itself is the surface; no new config key).
- Memoizing the freeze-guard `git rev-parse` call (read frequency is per-phase, not hot-loop; flagged in Risks if measurement says otherwise).

## Affected files
- `.redteam/workflows/orchestrator.py` — extend `TaskOutcome` literal with `"blocked_on_dependency"`; `process_batch` (manifest load + up-front validation via `object_pairs_hook` + topo-layered scheduler + transitive `blocked_on_dependency` skip); `process_task` (accept resolved-base / `base_is_parent` args, pin-before-branch reorder, ancestry check, plumb `repo` into `pinned_base_branch` call at the tier guard line ~1119); `_ensure_task_branch` (accept and act on a `base_is_parent` pull-skip arg); `_run_pipeline` (surface `blocked_on_dependency` results distinctly from `blocked_on_human_gate` / `deferred`).
- `.redteam/workflows/phase_runners/_base.py` — extend `pinned_base_branch` to require `repo: Path` and run the centralized freeze guard (re-resolve live parent tip, raise on SHA drift, no-op for root tasks); add a small `git rev-parse` helper used by the orchestrator pin step and by the guard.
- `.redteam/workflows/phase_runners/create_pr.py` — update the `pinned_base_branch(state)` call at line 167 to pass `repo` (`repo_root()` import already in scope from `_base`); no change to the pr-author prompt structure beyond consuming the resolved base.
- `.redteam/workflows/phase_runners/review_code.py` — update the `pinned_base_branch(state)` call at line 43 to pass `repo`.
- `.redteam/workflows/phase_runners/implement.py` — update all three `pinned_base_branch(state)` call sites (lines 172, 271, 390) to pass `repo`.
- `.redteam/workflows/phase_runners/write_test.py` — update the `pinned_base_branch(state)` call site (line 102) to pass `repo`.
- `.redteam/tests/test_base_branch_pin.py` — update existing direct-call test stubs (`base.pinned_base_branch({"base_branch": "develop"})`, etc.) to pass a `repo` Path argument so they exercise the new required signature without weakening the `#91` invariant they assert.
- `(new) .redteam/tests/test_goal_manifest_validation.py` — manifest schema + up-front fail-closed validation cases (cycle / unknown ref / self-dep / duplicate task ID detected pre-collapse / `len(depends_on) >= 2` / unknown task dir / malformed JSON; absent manifest leaves seeding untouched).
- `(new) .redteam/tests/test_goal_dag_scheduler.py` — topo-layered execution, `"blocked_on_dependency"` skip + transitive cascade when a parent is not `done`, independent-chain continuation, `_run_pipeline` summary surface, and the absent-manifest backward-compat path.
- `(new) .redteam/tests/test_goal_stacked_branching.py` — pin-before-branch ordering, ancestry check on a pre-existing wrong-base task branch (defers, never deletes), `_ensure_task_branch` pull-skip on the `base_is_parent` signal (and that a moved remote does not mutate the local parent), `create_pr` receives the parent branch as `--base`, review range is `parent...HEAD`.
- `(new) .redteam/tests/test_pinned_base_freeze_guard.py` — centralized accessor raises when the parent tip moved, asserted via at least one TDD `write_test` path AND the agent-pair `review_code` / `create_pr` paths so the "every reader fails closed automatically" property is grounded; root tasks (no SHA) → no-op.

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full project gate (ruff check + ruff format --check + pytest `.redteam/tests`).
- `.redteam/tests/test_base_branch_pin.py` — the `#91` pin-once + legacy-unpinned fail-closed invariant; must remain green after the `pinned_base_branch` signature change (stubs updated in-file as noted under "Affected files") and the orchestrator pin reorder.
- `.redteam/tests/test_agents_generic_prompts.py` and `.redteam/tests/test_install.py` — explicitly called out in the brief; both must remain green (no project- or stack-specific fingerprints introduced; installer file-class split untouched).

### To be created (the test-writing phase will define exact test names)
Tests under `.redteam/tests/` covering:
- Manifest validation aborts the **whole** batch (no task state seeded, no `process_task` call) on each of: cycle, unknown `depends_on` ref, self-dependency, **duplicate task IDs in the raw JSON (asserted via `object_pairs_hook` semantics, not post-parse dict state)**, task key with no matching on-disk task dir, `len(depends_on) >= 2`, malformed JSON.
- Manifest absent → `process_batch` behavior is byte-for-byte unchanged (same task order, same per-task seeding semantics, same state.json shape, same `_run_pipeline` exit code).
- Topo-layered scheduling runs roots first, then dependents only after the parent's task outcome is `"done"`.
- A task whose parent is `deferred` / `error` / `blocked_on_human_gate` / `blocked_on_dependency` is recorded as `"blocked_on_dependency"` in the `process_batch` results dict and skipped; descendants of that task in the same chain also cascade to `"blocked_on_dependency"`; other independent chains in the same batch continue.
- `_run_pipeline` distinguishes `"blocked_on_dependency"` from `"blocked_on_human_gate"` and `"deferred"` in its summary output and its exit code (a chain skipped due to an upstream defer does not by itself cause exit code 1).
- `process_task` writes `state["base_branch"]` (and, for dependents, `state["base_branch_sha"]`) **before** `_ensure_task_branch` is invoked, and `_ensure_task_branch` is called with that pinned value plus the `base_is_parent` argument.
- A pre-existing dependent task branch whose tip is NOT a descendant of the pinned parent fails closed (deferred with a clear `last_failure_reason`) and the local branch is not deleted.
- The centralized freeze guard inside `pinned_base_branch` raises when the parent tip moved after `implement`, including from the TDD `write_test` reader (not only agent-pair phases) and at both `review_code` and `create_pr` read sites.
- A moved remote parent-task branch does NOT mutate the local parent (pull is skipped — the explicit `base_is_parent` signal, not a `branch_prefix` string match).
- `create_pr` invokes the pr-author with the parent branch as `--base`; the review-code prompt's diff range is `parent...HEAD` for a dependent.
- Root tasks: no `base_branch_sha` recorded, freeze guard is a no-op, behavior unchanged.

## Risks
- **Umbrella design doc absent.** The brief cites `docs/decisions/2026-06-27-goal-mode-design.md` as the accepted design, but `Glob` finds no such file in the repo (only `2026-06-17-reviewer-transport-and-subagent.md` exists). The brief itself encodes every Slice A requirement, so this outcome treats the brief as the contract; the gate must decide whether the design doc lands in the same PR / a precursor PR / explicitly out-of-scope.
- **`pinned_base_branch` signature is now required `(state, repo)`.** This is the chosen path (no overload / no default `repo=None`) — it forces every caller to be explicit and prevents a silent fall-back to a stale or wrong repo. The cost is that the seven listed call sites + the `test_base_branch_pin.py` stubs all change in lockstep. The gate must confirm this is preferable to a `repo: Path | None = None` default.
- **`base_is_parent` plumbing through `_ensure_task_branch`.** The current `_ensure_task_branch` signature is `(task_id, repo, branch_prefix, base_branch)`. The plan adds a 5th keyword-only argument `base_is_parent: bool = False` — the minimum-surface plumbing. Flag at the gate if a different shape (e.g. a `ResolvedBase` value object) is preferred.
- **Subprocess cost in the accessor.** Centralizing `git rev-parse` inside `pinned_base_branch` adds a subprocess call per pin read. Per-phase read frequency is low (a handful per task), but if measurement shows a hot loop the plan does NOT memoize in this slice — memoization is a deliberate follow-up and is listed as Out of scope.
- **`ceilings` schema is opaque.** The brief says "parsed/stored but not enforced." The plan validates only that `ceilings` (if present) is a JSON object; its keys/values are not type-checked. Decide at the gate whether Slice A should also reject obviously-malformed ceilings (e.g. non-numeric values) or whether that strictness belongs to Slice C.
- **Backward-compat surface is large.** Every existing batch test exercises the absent-manifest path; the new test_goal_dag_scheduler.py absent-manifest assertion is the targeted backstop, but the broader `verify.sh` run is what proves no flat-mode regression.
