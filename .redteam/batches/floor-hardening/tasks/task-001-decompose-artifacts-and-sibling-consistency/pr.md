## What
Narrow the pre-worker fail-closed floors in `implement.py` with three enumerated
exemptions — batch-root decompose artifacts, `input.md` on the #124 sibling
allowlist, and #117 Check-2 honoring the same allowlist as `_floor_outside_scope`
— so a goal-mode run stops self-locking on the harness's own artifacts while the
adversarial baseline-rewrite guard stays intact.

## Why
The first autonomous goal-mode run (`reviewer-cost-p3p5`) surfaced #136: the
pre-worker floors' threat model is right but their scope model is wrong — they
fail-closed on the harness's *own* artifacts inside the same batch (goal-mode
decompose files at the batch root, and sibling-task `input.md` under the shared
`tasks/` root). The recorded catch-22 is that #117 Check-2 didn't honor the same
sibling allowlist #124 already gave `_floor_outside_scope`, so a stacked child
would either self-lock on the stored baseline entry or lose the sweep's
operator-WIP exclusion. This task closes #136 with three narrow, enumerated
exemptions — no broader trust surface, no weakening of the sweep, no baseline
mechanism changes.

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

## Verification
- Tests: test_floor_batch_root_decompose_artifact_exempt, test_floor_batch_root_non_allowlisted_still_trips, test_floor_batch_root_allowlisted_in_subdir_still_trips, test_floor_sibling_input_md_exempt, test_floor_sibling_buried_input_md_still_trips, test_floor_cross_batch_input_md_still_trips, test_cross_run_check1_batch_root_and_sibling_input_md_exempt, test_cross_run_check2_allowlisted_stored_baselines_exempt, test_cross_run_adversarial_baseline_still_caught, test_floor_default_path_non_goal_mode_unchanged
- Verify command: `bash .redteam/scripts/verify.sh` ✅ (698 passed)

## Code review summary
- Diff summary: adds a single shared `_is_harness_artifact(p, task_dir, cwd, scope_roots)` predicate in `implement.py`; `_floor_outside_scope` and `_cross_run_trust_root_floor._is_allowed` both route through it, so the two floors cannot drift on "allowed".
- IR-001 (blocker, resolved): `input.md` added to `_SIBLING_BASENAME_ALLOWLIST`; batch-root decompose artifacts narrowly allowlisted (exact basenames only, no subdirs).
- IR-002 (blocker, resolved): `_cross_run_trust_root_floor` now uses the same predicate for **both** Check-1 (live `current_untracked`) and Check-2 (stored `implement_*_baseline`), closing the #117↔#124 catch-22.
- IR-003 (major, resolved): new regression suite `test_floor_decompose_and_sibling_exemptions.py` (10 tests) covers batch-root exemptions, non-allowlisted refusals, sibling `input.md`, Check-1, Check-2, adversarial baseline preservation, and default-path byte-identical behavior.
- Scope discipline: change is limited to `implement.py` + targeted tests; no adapter/installer/verification-snapshot/prompt regression; the allowlist is a closed enumeration (no broad `.redteam/batches/` prefix trust).
- Adversarial baseline-rewrite guard is preserved — an outside-scope, non-allowlisted path in a stored baseline still trips Check-2.
- REVIEW_DECISION: APPROVED.

## Generated by
redteam / batch floor-hardening / task task-001-decompose-artifacts-and-sibling-consistency
