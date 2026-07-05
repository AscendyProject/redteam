# Task 1 — Floor exemptions for decompose artifacts and #117↔#124 consistency (#136)

## Context

The first autonomous goal-mode run (`reviewer-cost-p3p5`) proved the pre-worker
fail-closed floors' threat model right but their scope model wrong: they
fail-closed on the harness's **own** artifacts inside the same batch. This
task closes issue **#136** by narrowing the floors' scope model with three
enumerated exemptions — no broader trust surface, no weakening of the sweep's
operator-WIP exclusion, no changes to the set-once baseline mechanism itself.

The floors live in `.redteam/workflows/phase_runners/implement.py`:

- `_floor_outside_scope(current_tracked, proj, task_dir, cwd)` — the pre-worker
  out-of-scope **tracked** floor (#91 Part A). Already exempts the current task
  dir and, per #124, top-level allowlisted sibling-task basenames
  (`state.json`, `outcome.md`, `pr.md`, and `*_review.md`) under the same
  batch's `tasks/` root.
- `_cross_run_trust_root_floor(state, task_dir, cwd, proj, current_untracked)`
  — the cross-run trust-root floor (#117). Its inner `_is_allowed(p)` currently
  honors only the task-dir exemption and the source/test scope roots — it does
  **not** honor the #124 sibling allowlist, which is the root of the recorded
  catch-22.

## What to change

All engine changes belong in
`.redteam/workflows/phase_runners/implement.py`; all tests belong under
`.redteam/tests/`. Do **not** touch `docs/`, any file outside
`.redteam/workflows/` and `.redteam/tests/`, or any adapter/prompt file. This
run predates the #137 fix, so a plan that declares Affected files outside
those two trees will self-lock at the pre-worker floor.

Implement three narrow, enumerated exemptions:

1. **Batch-root decompose artifacts (both floors).** In goal mode the batch
   root (`task_dir.parent.parent`, i.e. the `<batch>/` dir that owns the
   `tasks/` root) holds the harness's decompose decision trail:
   - `goal.md`
   - `goal.json`
   - `decompose_review.md`
   - `decompose_blocked.md`

   A path that lies **directly at the batch root** (relative-to-batch-root
   contains no `/`) whose basename is in that exact allowlist must be exempt
   in both `_floor_outside_scope` (#91 Part A) and
   `_cross_run_trust_root_floor` (#117), with the same rationale as the
   existing task-dir exemption: they are the harness's own artifacts, not
   operator WIP. This must be a **closed basename allowlist** — not a
   prefix trust of the batch root, not `.redteam/batches/**`, not arbitrary
   `*.md` at the batch root. Paths in batch-root subdirectories or with
   any other basename are **not** exempt.

2. **`input.md` added to the #124 sibling top-level basename allowlist.** A
   stacked child's scheduler run always sees its sibling's brief. Extend
   `_SIBLING_BASENAME_ALLOWLIST` in `_floor_outside_scope` (currently
   `{"state.json", "outcome.md", "pr.md"}`, with `*_review.md` handled
   separately via `fnmatch`) to include `"input.md"`. The existing structural
   guards must stay intact: top-level under the sibling task dir only
   (relative-to-sibling contains no `/`), same-batch `tasks/` root only,
   the current task's own dir is not a sibling.

3. **Fix #117 Check-2's inconsistency with #124 (the catch-22).** Today,
   `_cross_run_trust_root_floor._is_allowed` recognizes only
   `_in_task_dir(p)` and the scope roots. That means a sibling top-level
   allowlisted path — which `_floor_outside_scope` correctly tolerates —
   still gets stored in the set-once baseline, and Check-2 then fail-closes
   on that stored entry in the next process (self-lock). Pruning the stored
   entry instead breaks the sweep's operator-WIP exclusion (this is the
   catch-22 recorded in the #136 comment thread).

   Make `_cross_run_trust_root_floor`'s `_is_allowed` honor **exactly the
   same** allowlist that `_floor_outside_scope` uses after the changes in
   items 1 and 2:
   - task-dir POSIX-prefix exemption (unchanged);
   - scope roots (unchanged);
   - top-level sibling basename allowlist under the same batch's `tasks/`
     root, including `input.md` from item 2 and the `*_review.md`
     `fnmatch` pattern;
   - batch-root basename allowlist from item 1.

   Apply this to **both** Check-1 (live outside-scope untracked surface) and
   Check-2 (stored-baseline contents). To avoid drift between the two
   floors' definitions of "allowed", factor the shared predicate — e.g. a
   single `_is_harness_artifact(p, task_dir, cwd)` helper that both call —
   rather than duplicating the sibling and batch-root logic. Keep it
   stdlib-only.

   **Do not** relax anything else about Check-2: an outside-scope,
   non-allowlisted entry in a stored baseline must still trip the floor
   (adversarial baseline rewrite must still be caught).

## Files affected

- `.redteam/workflows/phase_runners/implement.py`
- `.redteam/tests/test_floor_decompose_and_sibling_exemptions.py` (new)

The Affected files section in `outcome.md` must list **exactly** those two
paths (prefix new files with `(new) `), and no others. Adding any docs/,
scripts/, or top-level file will self-lock this task at its own pre-worker
floor (that is the very self-lock #137 fixes; #137 is not fixed yet while
task 1 runs).

## Tests to add (new file, task-scoped)

Put the new tests in
`.redteam/tests/test_floor_decompose_and_sibling_exemptions.py`. Cover, at
minimum:

- `_floor_outside_scope` returns empty for a tracked path at the batch root
  whose basename is in `{goal.md, goal.json, decompose_review.md,
  decompose_blocked.md}` (one assertion per basename).
- `_floor_outside_scope` still returns the offending path for a tracked
  batch-root file whose basename is not in that allowlist (e.g. `notes.md`,
  `secrets.env`).
- `_floor_outside_scope` still returns the offending path for a
  batch-root-allowlisted **basename** placed in a batch-root
  **subdirectory** (structural guard: only directly at the batch root is
  exempt).
- `_floor_outside_scope` returns empty for a sibling-task top-level
  `input.md`, and still returns the offending path for
  `tasks/<sibling>/subdir/input.md` (buried is not exempt) and for
  `input.md` under a different batch's `tasks/` root.
- `_cross_run_trust_root_floor` Check-1 returns empty for a batch-root
  allowlisted basename and for a sibling top-level `input.md` in
  `current_untracked` (mirrors #91 Part A).
- `_cross_run_trust_root_floor` Check-2 returns empty when
  `state["implement_untracked_baseline"]` and
  `state["implement_tracked_baseline"]` contain only batch-root
  allowlisted basenames, sibling top-level allowlisted basenames, or
  in-task-dir/scope paths — this is the direct catch-22 regression.
- `_cross_run_trust_root_floor` **still fails** when a stored baseline
  contains an outside-scope, non-allowlisted path (the adversarial
  baseline-rewrite guard must survive — this is the security-boundary
  regression test).

Also keep every existing regression in place — e.g. `_floor_outside_scope`
still refuses genuine operator WIP outside scope; `test_sibling_task_floor_exemption.py`
and `test_baseline_trust_root_cross_run.py` must stay green.

## Constraints and non-goals

- The floors stay fail-closed. Every exemption is a narrow, closed
  enumeration (exact batch-root basenames; exact sibling top-level basenames
  + the `*_review.md` fnmatch that already exists). **No** broad prefix
  exemption, **no** "trust everything under `.redteam/batches/`", **no**
  weakening of the sweep's operator-WIP exclusion.
- Engine stays project-agnostic, stdlib-only, zero runtime deps.
- No changes to reviewer/worker adapters or prompts.
- No redesign of the set-once baseline mechanism or the commit sweep.
- Default behavior for non-goal-mode, non-stacked, in-scope-only tasks must
  be byte-identical (a task whose `task_dir` has no batch-root and no
  sibling `tasks/` sibling directory to speak of must produce the same
  offending set as today).
- `outcome.md` **must** include a parseable `## Verification` section
  containing a fenced ```yaml``` block invoking
  `bash .redteam/scripts/verify.sh` — the planner's `## Verification hooks`
  prose form does **not** parse (known #138 pitfall). Example:

  ```
  ## Verification

  ```yaml
  hooks:
    - bash .redteam/scripts/verify.sh
  ```
  ```

## Operator delegation

Narrow plan-level scope questions during this run are delegated to the
operator agent (prefer the narrowest exemption that unblocks the goal).
Weakening the floors' security boundary beyond the three exemptions
enumerated above is **not** delegated — that stops the run.
