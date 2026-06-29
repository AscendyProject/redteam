## What
On a dirty-tree run, the operator's pre-existing *tracked* modifications must
not be swept into the task's WIP commit / PR. The implement runner captures a
pre-worker **tracked baseline** (`_tracked_changed_paths(cwd, pinned base)`)
once per task — symmetrically with the #112 untracked baseline — durably
persists it to `state.json` BEFORE the worker runs, then in
`_commit_worker_diff` stages only `now − before_tracked` for the tracked half
(unchanged `+ new_untracked` for the untracked half, same task-dir filter).
A current pre-worker tracked-diff probe whose paths fall OUTSIDE
`proj.source_dirs + proj.test_dir` causes the phase to **fail closed**
(return `PhaseResult(status="error", ...)`) WITHOUT persisting any baseline,
telling the operator to commit or stash their unrelated tracked WIP — so a
clean re-run after the operator stashes is NOT locked out by a stale
persisted baseline (the PR-001 self-lock fix).

## Why
Resolves #91 Part A. Today `_commit_worker_diff` stages every tracked change
vs the pinned base, so on a dirty-tree run the operator's pre-existing
tracked WIP is attributed to the worker and ends up in the reviewed PR. This
is the tracked-file mirror of the bug #112 closed for untracked files, and
the fix is the symmetric mirror of that solution: a pre-worker tracked
baseline + an out-of-scope fail-closed floor that defers cleanly so a
follow-up clean re-run is not self-locked.

## Done-when
- [ ] `bash .redteam/scripts/verify.sh` exits 0 (ruff check + ruff format
      `--check` + full pytest), including every test currently in
      `.redteam/tests/test_implement_untracked_baseline_pin.py`,
      `.redteam/tests/test_implementer_commit.py`,
      `.redteam/tests/test_base_branch_pin.py`,
      `.redteam/tests/test_pinned_base_freeze_guard.py`,
      `.redteam/tests/test_tdd_commit_discipline.py`, and
      `.redteam/tests/test_progress_surface.py`.
- [ ] A new shared helper `get_or_set_tracked_baseline(state, cwd,
      base_branch, *, _tracked_fn=None) -> set[str]` is exported from
      `.redteam/workflows/phase_runners/_base.py`. **Set-once semantics keyed
      solely on the PRESENCE of `state["implement_tracked_baseline"]` as a
      `list` — no other prior-run signal (`implement_round_count`,
      `phases_completed`, `verification.last_run_at`,
      `task_dir/impl_diff.patch` existence, "commit beyond pinned base", etc.)
      is consulted.** Given a `state` dict that already contains a `list` at
      that key, the helper returns the stored list as a `set`, does NOT call
      the tracked-diff probe, and does NOT mutate the stored value. Otherwise
      it calls the tracked probe exactly once against the **pinned**
      `base_branch` (the one the caller already obtained via
      `pinned_base_branch(state, repo)`), stores the result back as a sorted
      `list[str]`, and returns the matching set. The helper itself does NOT
      persist — its caller is responsible for `persist_state(task_dir, state)`,
      mirroring `get_or_set_untracked_baseline`. The `_tracked_fn` keyword is
      the same internal patchable seam pattern `get_or_set_untracked_baseline`
      uses (so a test that patches the caller-module's binding reaches the
      right function AND so the runner can hand in an already-computed fresh
      probe to avoid a redundant git call). Stdlib-only; NUL-safe;
      `-c core.quotepath=false`; **fail-closed** on git error (raises
      `RuntimeError`, omits stderr — IR-002).
- [ ] The tracked-diff probe used by the helper is **`_tracked_changed_paths`
      (the existing fail-closed NUL-safe symbol currently at
      `phase_runners.implement._tracked_changed_paths`)**. The implementer
      either:
        (a) re-imports the existing symbol from `_base.py` so the helper and
            `_commit_worker_diff` share one definition, or
        (b) moves `_tracked_changed_paths` from `implement.py` to `_base.py`
            (preferred — it sits next to `untracked_files`) and has
            `implement.py` import it from `_base.py`.
      Whichever path is taken, **only one definition survives** (no duplicated
      private copy in `implement.py`), and a grep for `def
      _tracked_changed_paths` in `.redteam/workflows/` finds it exactly once.
      Signature and behavior are byte-identical to today: returns a NUL-safe
      `list[str]` of paths changed on the branch (committed + unstaged +
      staged) vs the pinned `base_branch`, fail-closed on any git error.
- [ ] **Pre-worker ordering — the fail-closed floor runs on a FRESH probe
      BEFORE the baseline is persisted (PR-001 fix).** Replacing the existing
      pre-worker site in BOTH `_run_agent_pair` and the TDD `run`
      (`.redteam/workflows/phase_runners/implement.py`, currently near
      `:316-317` agent-pair and `:460-461` TDD; relocate by symbol), the
      ordering is, exactly:
        1. **Single fresh tracked probe.** Call
           `_tracked_changed_paths(rr, base_branch)` exactly once into a
           local `current_tracked: list[str]` (and the matching
           `current_tracked_set: set[str]`). This is the only tracked-diff
           probe this entry — the helper and `_commit_worker_diff` are
           wired so a second probe at this stage is unnecessary.
        2. **Out-of-scope floor on the FRESH probe.** Compute the subset
           of `current_tracked_set` that falls OUTSIDE
           `proj.source_dirs + proj.test_dir` using the SAME
           replace-backslash-then-trailing-slash root normalization
           `_uncommitted_scope_files` / `_uncommitted_outside_scope_files`
           use (reuse the helper / lift the inline `_root` helper to a
           module-private utility — pick ONE; do not duplicate the prose).
           If that subset is non-empty: return
           `PhaseResult(status="error", feedback=..., log=..., diff="")`,
           **WITHOUT calling `get_or_set_tracked_baseline` and WITHOUT
           writing `state["implement_tracked_baseline"]` and WITHOUT
           calling `persist_state(task_dir, state)`** for this contaminated
           entry. The feedback (i) names the out-of-scope tracked paths,
           (ii) carries the "refusing to sweep operator tracked WIP into
           the task commit; commit or stash your unrelated tracked WIP
           before re-running" intent, (iii) does NOT leak git stderr
           (IR-002), and (iv) makes clear this is a DEFER-style
           fail-closed routed through the existing orchestrator
           `status="error"` retry/defer path (no new orchestrator
           branch). **Because the persistence step is skipped on a
           contaminated entry, a subsequent clean re-run (operator
           stashed/committed) finds `implement_tracked_baseline` still
           absent and snapshots the fresh, clean tree — fixing the
           PR-001 self-lock.**
        3. **Floor-pass → set-once persistence.** Call
           `get_or_set_tracked_baseline(state, rr, base_branch,
           _tracked_fn=lambda *_: current_tracked_set)`. The
           `_tracked_fn` seam ensures the helper does NOT re-probe git
           on the key-absent first-entry path (it stores the already
           computed `current_tracked` — a sorted list of it — into
           `state["implement_tracked_baseline"]`); on the key-present
           resume path the helper still returns the stored list as a
           set without touching `_tracked_fn` (so an operator who has
           changed the tree between rounds keeps the original baseline
           — the set-once contract).
        4. **Single persist for both baselines.** Wrap steps 1–3 + the
           existing `get_or_set_untracked_baseline(...)` call in ONE
           `try / except (OSError, RuntimeError)` block, then call
           `persist_state(task_dir, state)` exactly once covering BOTH
           `implement_untracked_baseline` and
           `implement_tracked_baseline`. The existing fail-closed
           handler keeps its "could not snapshot the working tree
           before implement" feedback shape and does not leak stderr.
        5. **Worker invocation.** Only after the floor passes AND the
           single persist succeeds is
           `get_worker_adapter(state).invoke(...)` called.
      On the floor-fail path the orchestrator is handed a normal
      `status="error"` PhaseResult (it routes through the existing
      retry/defer ladder; no orchestrator change is in scope).
- [ ] **Resume / key-present behavior.** When
      `state["implement_tracked_baseline"]` is already a `list` (a prior
      entry's clean snapshot survived to disk), the helper returns the
      stored value as a `set` without re-probing AND the runner still
      runs step 2 (the floor on a FRESH probe) so a NEWLY-introduced
      out-of-scope tracked file between rounds defers correctly. The
      floor's decision is therefore baseline-INDEPENDENT — it inspects
      the CURRENT tree, not the persisted baseline, which is what makes
      the PR-001 self-lock impossible.
- [ ] `_commit_worker_diff` in `.redteam/workflows/phase_runners/implement.py`
      accepts the new `before_tracked` argument (signature becomes
      `_commit_worker_diff(task_dir, state, cwd, before_untracked,
      before_tracked)`; callers in both paths pass the same in-memory set
      they got from `get_or_set_tracked_baseline`). The tracked half of the
      staging set is computed as
      `set(_tracked_changed_paths(cwd, base_branch)) - before_tracked`
      (sorted for determinism), then unioned with `new_untracked = current
      − before_untracked`, then filtered with the existing `_in_task_dir`
      check, then deduplicated in a stable order — preserving today's
      "staged via NUL-delimited literal pathspec" / "fail-closed on any
      git failure" semantics. The untracked half (`new_untracked`) is
      UNCHANGED.
- [ ] **In-memory set, no re-read.** Both implement paths pass the SAME
      in-memory `before_tracked` `set` to `_commit_worker_diff` that the
      helper returned. `_commit_worker_diff` does NOT re-read
      `state["implement_tracked_baseline"]` from disk for the subtraction
      (same-round TOCTOU safety / IR-006). The post-commit two-layer
      integrity gate (`_uncommitted_scope_files` Layer 1 +
      `_uncommitted_outside_scope_files` Layer 2) is **unchanged in
      behavior**: this fix is additive and must not weaken either layer.
- [ ] **No legacy prior-run signal detection** is introduced. The implement
      runners do NOT branch on `implement_round_count`, on
      `phases_completed`, on `verification.last_run_at`, on
      `task_dir/impl_diff.patch` existence, or on "commit beyond pinned
      base" as a way to refuse fresh-snapshotting a key-absent state.
      Key-absent legacy state simply takes the first-entry tracked
      snapshot via the helper above (matching the #112 ruling for the
      untracked side).
- [ ] **state.template.json is NOT updated** to seed
      `implement_tracked_baseline`. The key stays absent on a fresh task
      so the helper's "missing → first-entry snapshot" branch is the
      only initializer (no `[]` or `null` default that would either
      sweep pre-existing tracked operator WIP into the commit or force
      a fragile `None`-vs-`list` special case at every caller). Mirrors
      the #112 PR-003 decision verbatim.
- [ ] **`_ensure_task_branch` is NOT modified.** The orchestrator's
      stash/pop step (`orchestrator.py:916-990`, steps 1+5) keeps
      preserving the operator's local changes in the worktree exactly
      as today — this fix attributes them correctly at commit time, it
      does not change where they live.
- [ ] **Pinned base usage is preserved.** Both the new baseline and the
      staging-set probe read `pinned_base_branch(state, rr)` (the #91
      Part B pin), never live config. A grep for `proj.base_branch` or
      `load_config` inside the new helper finds nothing; the helper
      takes `base_branch: str` as an argument and the callers pass the
      pinned value they already resolved.
- [ ] **Mode-neutral.** Both `_run_agent_pair` and the TDD `run`
      exercise the identical code path: same helper, same persist call,
      same out-of-scope floor with the same fresh-probe-then-persist
      ordering, same `_commit_worker_diff` argument list. The
      single-symbol shared-helper coverage assertions
      (`test_shared_helper_symbols_exported_from_base` /
      `test_implement_module_imports_shared_helpers` style) are
      extended to cover the new tracked symbol.
- [ ] **The documented by-name overlap limitation is CARRIED FORWARD
      in `_commit_worker_diff`'s docstring** explicitly for the tracked
      side, in the same prose register as the existing untracked-side
      comment at `implement.py:151-157`: when the operator modified a
      tracked file in place BEFORE the worker AND the worker then
      *also* modifies that same file, the path is in `before_tracked`,
      so `now − before_tracked` excludes the worker's change to it.
      The docstring notes the out-of-scope floor already removes the
      most damaging case (an operator pre-edit OUTSIDE scope) and the
      residual is an in-scope tracked file the operator pre-edited —
      itself anomalous since the worker is scoped to outcome.md's
      Affected files. Closing it by content-hashing the whole tracked
      set is judged disproportionate; this is a documentation update,
      not a behavior change.
- [ ] Engine stays **project-agnostic** (no project- or stack-specific
      fingerprints leak into `_base.py` / `implement.py`) and
      **stdlib-only** (no new pip dependency).

## Verification
- Tests: test_get_or_set_tracked_baseline_key_absent, test_get_or_set_tracked_baseline_key_absent_live_probe, test_get_or_set_tracked_baseline_does_not_persist, test_get_or_set_tracked_baseline_key_present_set_once, test_get_or_set_tracked_baseline_idempotent_under_prior_run_signals, test_out_of_scope_tracked_fails_closed_no_baseline_agent_pair, test_out_of_scope_tracked_fails_closed_no_baseline_tdd, test_pr001_self_lock_clean_rerun_after_stash_agent_pair, test_out_of_scope_floor_fresh_probe_agent_pair, test_out_of_scope_floor_fresh_probe_tdd, test_in_scope_preexisting_tracked_not_attributed_agent_pair, test_in_scope_preexisting_tracked_not_attributed_tdd, test_worker_tracked_changes_still_land_agent_pair, test_worker_tracked_changes_still_land_tdd, test_durable_preworker_flush_tracked_agent_pair, test_durable_preworker_flush_tracked_tdd, test_clean_tree_run_unchanged_agent_pair, test_clean_tree_run_unchanged_tdd, test_pinned_base_no_live_config_bleed, test_commit_worker_diff_before_tracked_excludes_preexisting, test_mode_neutrality_single_definition, test_fail_closed_probe_failure_no_stderr_leakage_agent_pair, test_fail_closed_probe_failure_no_stderr_leakage_tdd (in `.redteam/tests/test_tracked_baseline_attribution.py`)
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff summary: adds `get_or_set_tracked_baseline` and relocates the single `_tracked_changed_paths` definition into `_base.py`; rewires both `_run_agent_pair` and the TDD `run` to do fresh-probe → out-of-scope floor → set-once persist → single `persist_state` → worker; extends `_commit_worker_diff(task_dir, state, cwd, before_untracked, before_tracked)` and subtracts the baseline.
- IR-001 (blocker, resolved): `bash .redteam/scripts/verify.sh` now exits 0 — 516 tests passed, ruff + format check + pytest green.
- IR-002 (major, resolved): `_commit_worker_diff` carries the explicit `before_tracked` contract; both call sites pass the same in-memory set returned before worker invocation; the direct-arg passthrough regression covers it.
- IR-003 (major, resolved): the post-commit Layer 1 integrity gate (`_uncommitted_scope_files`) stays baseline-independent — in-scope pre-edits still block on stale worktree state.
- New tests are discriminating against pre-change behavior (the helper did not exist, the pre-worker floor and no-persist self-lock would fail before this change, `_commit_worker_diff` was 4-arg). No new pip dep; no project-specific fingerprints; shell-free subprocess; PR-001/PR-002 plan-review blockers resolved.
- REVIEW_DECISION: APPROVED.

## Generated by
redteam / batch fix-91-part-a-tracked-sweep / task task-001-tracked-mod-attribution
