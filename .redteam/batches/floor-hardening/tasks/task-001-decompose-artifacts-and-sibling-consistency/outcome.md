# Outcome — Floor exemptions for decompose artifacts and #117↔#124 consistency (#136)

## Goal
Narrow the pre-worker fail-closed floors in `implement.py` with three enumerated
exemptions — batch-root decompose artifacts, `input.md` on the #124 sibling
allowlist, and #117 Check-2 honoring the same allowlist as `_floor_outside_scope`
— so a goal-mode run stops self-locking on the harness's own artifacts while the
adversarial baseline-rewrite guard stays intact.

## Done-when
- [ ] `bash .redteam/scripts/verify.sh` passes (ruff + ruff format + full pytest, `-x`).
- [ ] `.redteam/workflows/phase_runners/implement.py` defines a single shared
      allowlist predicate (e.g. `_is_harness_artifact(p, task_dir, cwd)`) used by
      both `_floor_outside_scope` and `_cross_run_trust_root_floor` so their
      "allowed" definitions cannot drift.
- [ ] `_floor_outside_scope` treats a **tracked** path directly at the batch
      root (`task_dir.parent.parent`, no `/` in the relative-to-batch-root
      remainder) whose basename is exactly one of `goal.md`, `goal.json`,
      `decompose_review.md`, `decompose_blocked.md` as exempt, and treats any
      other basename at the batch root — or the same basenames in a batch-root
      subdirectory — as still offending.
- [ ] `_floor_outside_scope`'s `_SIBLING_BASENAME_ALLOWLIST` includes
      `"input.md"` in addition to `state.json`, `outcome.md`, `pr.md` (with
      `*_review.md` continuing to match via the existing `fnmatch` clause),
      and the existing structural guards (top-level only under sibling task
      dir, same-batch `tasks/` root only, current task's own dir is not a
      sibling) remain in place.
- [ ] `_cross_run_trust_root_floor`'s inner `_is_allowed` honors the exact same
      four-part allowlist (task-dir POSIX-prefix, scope roots, batch-root
      basename allowlist, sibling top-level basename allowlist including
      `input.md` + `*_review.md` fnmatch) for **both** Check-1 (live
      `current_untracked` surface) and Check-2 (stored
      `implement_untracked_baseline` / `implement_tracked_baseline` contents).
- [ ] Check-2 still returns the offending path when a stored baseline contains
      an outside-scope, non-allowlisted path (the security-boundary regression
      — adversarial baseline rewrite is still caught).
- [ ] `pytest .redteam/tests/test_floor_decompose_and_sibling_exemptions.py -q`
      passes, with tests covering each behavior enumerated in the brief.
- [ ] `pytest .redteam/tests/test_sibling_task_floor_exemption.py .redteam/tests/test_baseline_trust_root_cross_run.py -q`
      still passes, with ONLY the minimal targeted updates: assertions /
      parametrizations that pin the OLD refusal of the now-exempt paths
      (`input.md` as a sibling top-level basename — e.g. the `bad_name`
      parametrizations — and the four batch-root decompose-artifact
      basenames) are updated to the new expectation. Every other refusal
      case in those files stays byte-identical and green (they remain the
      security-boundary regression suite). [Operator amendment under the
      goal's delegation clause: the original "unmodified" wording was
      self-contradictory — those tests pin the exact semantics this
      approved plan changes; the implementer correctly refused to proceed
      (3 attempts). No exemption beyond the goal's enumerated set is added.]
- [ ] The engine remains stdlib-only (no new imports outside
      `pathlib` / `fnmatch` / other stdlib modules already used in
      `implement.py`).

## Out of scope
- Any change to `_uncommitted_scope_files`, `_uncommitted_outside_scope_files`,
  `_commit_worker_diff`, or the sweep's operator-WIP exclusion semantics.
- Any change to the set-once baseline mechanism itself (persistence,
  key names, or reset behavior).
- Any change to reviewer/worker adapters, prompts, agent skeletons, or the
  orchestrator/state machine.
- Adding a broad prefix exemption (e.g. "trust everything under
  `.redteam/batches/`"), or accepting arbitrary `*.md` at the batch root.
- Fixing #137 (planner-declared Affected files vs pre-worker floor) or #138
  (planner `## Verification hooks` heading vs parser) — this task is scoped to
  #136 only.
- Any doc, script, workflow, or top-level file change (would self-lock this
  task at its own pre-worker floor).

## Affected files
- `.redteam/workflows/phase_runners/implement.py` — add shared allowlist
  predicate; extend `_floor_outside_scope` with the batch-root basename
  allowlist and `input.md` on the sibling basename allowlist; route
  `_cross_run_trust_root_floor._is_allowed` through the shared predicate.
- `(new) .redteam/tests/test_floor_decompose_and_sibling_exemptions.py` — the
  task-scoped regression suite for the three exemptions and the Check-2
  security-boundary guard; written by the agent-pair implementer (this repo
  runs in agent-pair mode — the implementer writes the tests).

## Verification

```yaml
hooks:
  - bash .redteam/scripts/verify.sh
```

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full suite (ruff check + ruff format
  check + `pytest .redteam/tests -x --tb=short`).
- `pytest .redteam/tests/test_sibling_task_floor_exemption.py -q` — #124
  sibling exemption regression.
- `pytest .redteam/tests/test_baseline_trust_root_cross_run.py -q` — #117
  cross-run baseline trust-root floor regression.

### To be created (the implementer will define exact test names)
Tests under `.redteam/tests/test_floor_decompose_and_sibling_exemptions.py`
covering:

- `_floor_outside_scope` returns empty for a tracked batch-root path whose
  basename is exactly `goal.md`, `goal.json`, `decompose_review.md`, or
  `decompose_blocked.md` (one assertion per basename).
- `_floor_outside_scope` still returns the offending path for a tracked
  batch-root file whose basename is NOT in that allowlist (e.g. `notes.md`,
  `secrets.env`).
- `_floor_outside_scope` still returns the offending path for a batch-root
  allowlisted basename placed in a batch-root subdirectory (structural
  guard: only directly at the batch root is exempt).
- `_floor_outside_scope` returns empty for a sibling-task top-level
  `input.md`, and still returns the offending path for
  `tasks/<sibling>/subdir/input.md` (buried is not exempt) and for
  `input.md` under a different batch's `tasks/` root.
- `_cross_run_trust_root_floor` Check-1 returns empty when `current_untracked`
  contains only batch-root allowlisted basenames and a sibling top-level
  `input.md` (mirrors #91 Part A behavior for the untracked surface).
- `_cross_run_trust_root_floor` Check-2 returns empty when
  `state["implement_untracked_baseline"]` and
  `state["implement_tracked_baseline"]` contain only batch-root allowlisted
  basenames, sibling top-level allowlisted basenames, or
  in-task-dir/scope paths (the direct #136 catch-22 regression).
- `_cross_run_trust_root_floor` still returns the offending path when a
  stored baseline contains an outside-scope, non-allowlisted path (the
  adversarial baseline-rewrite security-boundary regression).
- Default-path preservation: for a task whose `task_dir` has no batch-root
  siblings and no `tasks/` sibling directory, both floors return the same
  offending set as before the change (byte-identical default behavior for
  non-goal-mode, non-stacked tasks).

## Risks
- Two floors (`_floor_outside_scope` and `_cross_run_trust_root_floor`)
  currently compute `task_rel`, `tasks_rel`, `_in_task_dir`, and `scope_roots`
  independently; factoring the shared allowlist predicate touches both
  functions and must not accidentally change the offending set on the default
  (non-goal-mode) path — the "default behavior byte-identical" done-when item
  is the guard, but reviewers should look for accidental scope creep in the
  refactor.
- The brief forbids listing docs/scripts/top-level files under Affected files
  because #137 is not yet fixed; if the implementer discovers during
  implementation that a shared helper genuinely belongs in `_base.py` (rather
  than kept private to `implement.py`), that would expand Affected files and
  self-lock. Plan holds the helper inside `implement.py`; if that turns out to
  be wrong, stop and update `outcome.md` (do not silently expand).
- The parseable `## Verification` yaml block is required by the input brief to
  work around #138; the "### Existing" / "### To be created" subsections are
  kept as prose for reviewer readability and are not part of the parsed
  contract.
