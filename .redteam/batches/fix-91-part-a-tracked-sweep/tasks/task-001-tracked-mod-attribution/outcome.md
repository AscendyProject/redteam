# Outcome — Attribute tracked changes to the worker via a pre-worker tracked baseline + out-of-scope fail-closed (#91 Part A)

## Goal
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
      `:316-317` agent-pair and `:460-461` TDD — relocate by symbol), the
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

## Out of scope
- **#91 Part B** (pin `base_branch` end-to-end) — already merged
  (`274e02d`). This task only consumes the pinned value.
- The untracked baseline / #112 two-layer integrity gate — done. Only
  the tracked analog is added here; the untracked path is not
  refactored beyond what cleanly sharing the new helper's call site
  requires (e.g. wrapping both baseline calls in the same `try/except`
  and persisting once).
- **#117 adversarial-worker `state.json` poisoning** of the tracked
  baseline — a worker with `Write`/`Bash` can rewrite
  `state["implement_tracked_baseline"]` between rounds, masking
  arbitrary pre-edits. That is a TRUST-MODEL change reserved for a
  separate follow-up. This task assumes a non-adversarial operator on
  a dirty tree, exactly the same threat model #112's main fix drew.
- Any change to what `pr_author` stages at PR time. The task_dir
  decision trail is intentional and stays in the WIP commit exclusion
  (`_in_task_dir` filter).
- Content-hashing the tracked or untracked set to close the by-name
  overlap limitation. Documented and carried forward, not closed.
- Changing `_ensure_task_branch`'s stash/pop. The operator's WIP must
  still be preserved in the worktree (just not committed).
- Hash- or HMAC-based tamper detection on the persisted tracked
  baseline (same class as #117 above).
- `verify_test` / `write_test` / `create_pr` / `review_code` runner
  changes — not touched.
- Migration of any prior in-flight `state.json`. Behavior only needs
  to be deterministic when the key is absent or already a `list`.
- Closing the bounded, **one-time pre-fix residual** (a task already
  mid-implement at deployment time whose first post-fix entry
  snapshots a pre-existing operator-modified tracked in-scope file
  into the new baseline) — same class as the residual #112 accepted;
  closes after the first post-fix `implement` entry per task.

## Affected files
- `.redteam/workflows/phase_runners/_base.py` — add
  `get_or_set_tracked_baseline(state, cwd, base_branch, *,
  _tracked_fn=None) -> set[str]` next to `get_or_set_untracked_baseline`
  (set-once read-or-snapshot of `state["implement_tracked_baseline"]`,
  returns a `set`, stores a sorted `list[str]`, does NOT persist,
  fail-closed on git error). If the implementer chooses option (b)
  above, also relocate `_tracked_changed_paths` here from
  `implement.py` (next to `untracked_files` / `commit_paths`) and
  update `implement.py`'s import.
- `.redteam/workflows/phase_runners/implement.py` —
    1. Replace each pre-worker site (currently `:316-317` agent-pair
       and `:460-461` TDD; relocate by symbol — `_run_agent_pair`,
       `run`) with the **fresh-probe → floor → set-once persist →
       single persist_state → worker** sequence specified in
       Done-when. On the floor-fail path return
       `PhaseResult(status="error", ...)` without persisting the
       tracked baseline. On the floor-pass path call
       `get_or_set_tracked_baseline(state, rr, base_branch,
       _tracked_fn=lambda *_: current_tracked_set)`, then a single
       `persist_state(task_dir, state)` covering both baselines. The
       existing `OSError / RuntimeError` fail-closed handler widens
       to wrap the tracked snapshot.
    2. Extend `_commit_worker_diff`'s signature to
       `_commit_worker_diff(task_dir, state, cwd, before_untracked,
       before_tracked)` and compute the tracked-half staging set as
       `set(_tracked_changed_paths(cwd, base_branch)) -
       before_tracked` (sorted) unioned with `sorted(new_untracked)`,
       then deduped and `_in_task_dir`-filtered exactly as today.
       Update both call sites (currently `:363` agent-pair and `:485`
       TDD; relocate by symbol) to pass the new argument.
    3. If option (b) is taken, drop the local
       `_tracked_changed_paths` definition and import it from
       `_base.py`. If option (a), leave it in place but re-export and
       have `_base.py`'s helper import it through a single seam — no
       duplicated bodies.
    4. Extend `_commit_worker_diff`'s docstring with the symmetric
       by-name overlap limitation paragraph for the tracked side
       (same prose register as `:151-157`).
- `(new) .redteam/tests/test_tracked_baseline_attribution.py` — new
  regression file under the project test dir, matching the project's
  `test_*.py` pattern. Sits next to
  `test_implement_untracked_baseline_pin.py`. The agent-pair
  implementer (or, in TDD mode, the test-author) writes the tests
  here — not under the task dir.

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### Notes
- `bash .redteam/scripts/verify.sh` is the project verify command (ruff
  check + ruff format `--check` + full pytest over `.redteam/`). It is
  the single command the orchestrator snapshots and runs at the
  verification step; every existing test below is part of that pytest
  run, so the single verify command covers them all.
- Existing tests that must continue to pass under the verify run (the
  ones most directly adjacent to this change — call them out so the
  reviewer notices any silent regression):
  - `pytest .redteam/tests/test_implement_untracked_baseline_pin.py -q`
    — every test stays green WITHOUT MODIFICATION (the #112 untracked
    baseline + two-layer gate is not weakened).
  - `pytest .redteam/tests/test_implementer_commit.py -q` — every test
    stays green WITHOUT MODIFICATION.
  - `pytest .redteam/tests/test_base_branch_pin.py -q` and
    `pytest .redteam/tests/test_pinned_base_freeze_guard.py -q` — #91
    Part B base-branch-pin guarantees stay green; the new helper reads
    the pinned value, never live config.
  - `pytest .redteam/tests/test_tdd_commit_discipline.py -q` — TDD
    commit discipline (#82) stays green; the mode-neutral fix must
    not regress it.
  - `pytest .redteam/tests/test_progress_surface.py -q` — `save_state`
    / `progress.md` surface stays green; this fix does not touch
    `_write_progress`.
  - `pytest .redteam/tests/test_agents_generic_prompts.py -q` — agent
    bodies stay project-agnostic; the engine stays project-agnostic.
- New tests (the test-writing phase defines exact names) live under
  `.redteam/tests/` matching `test_*.py`. Behavioral scope:
  - **Helper unit — key absent.** `get_or_set_tracked_baseline(state,
    cwd, base_branch)` with no `implement_tracked_baseline` key calls
    the fail-closed `_tracked_changed_paths` probe exactly once
    against the given `base_branch`, stores a sorted `list[str]` at
    `state["implement_tracked_baseline"]`, and returns the matching
    `set`. With `_tracked_fn` supplied, the supplied callable is used
    INSTEAD of the live probe (so the runner can avoid double-reading
    git after the floor probe). Assert the helper does NOT itself
    write `state.json`.
  - **Helper unit — key present (set-once).** With
    `state["implement_tracked_baseline"]` already a list and a
    DIFFERENT live tracked set on disk, the helper returns the
    stored list as a set, does NOT call the tracked probe, does NOT
    call `_tracked_fn` even if supplied, and does NOT mutate the
    stored value. Assert the set-once decision is independent of
    `implement_round_count`, `phases_completed`, and
    `verification.last_run_at` (varying those does not change the
    helper's decision — no legacy signal detector).
  - **Operator out-of-scope tracked WIP → fail closed AND no
    persisted baseline (PR-001 fix).** A tracked file modified vs
    the pinned base **OUTSIDE** `proj.source_dirs + proj.test_dir`
    is present BEFORE the worker. The runner returns
    `PhaseResult(status="error", ...)` whose feedback names the
    out-of-scope path and carries the "commit or stash your
    unrelated tracked WIP" intent (no `fatal:` / no git stderr
    leakage). The worker is NOT invoked; the out-of-scope file is
    NOT staged or committed; the operator's working-tree edits stay
    intact; AND `state["implement_tracked_baseline"]` is ABSENT on
    disk in `task_dir/state.json` after the call (no contaminated
    persistence). A follow-up call on the SAME `state` after the
    operator removes the out-of-scope edit succeeds: the floor
    passes, the helper takes a clean first-entry snapshot, and the
    worker is invoked — the PR-001 self-lock case. Cover BOTH
    `_run_agent_pair` and the TDD `run`.
  - **Out-of-scope floor uses the FRESH probe, not the stored
    baseline.** Seed `state["implement_tracked_baseline"]` to a
    list of in-scope paths only (a clean prior snapshot). Make the
    CURRENT tree contain a freshly-added out-of-scope tracked
    change. The runner still defers via `status="error"`
    (baseline-independent floor) and does NOT mutate the seeded
    baseline. Cover both implement paths.
  - **Operator in-scope pre-existing tracked mod is NOT attributed
    to the worker** when the worker does not touch it. A tracked
    source-or-test file modified by the operator before the worker;
    the worker is a no-op (or modifies a different file); after
    `_commit_worker_diff`, the operator-edited path is NOT in
    `git diff <base>...HEAD` / `impl_diff.patch`, and the worker's
    own change (if any) IS. The operator's edit remains in the
    worktree, uncommitted. Cover both paths.
  - **Worker's own tracked changes still land** (no over-broad
    subtraction). A clean tree at first entry; the worker modifies
    an existing tracked file under `proj.source_dirs` and creates a
    new file under `proj.test_dir`; after the WIP commit, both
    paths appear in `git diff <base>...HEAD` and in
    `impl_diff.patch`. Cover both paths.
  - **Set-once / resume — tracked baseline survives an interrupted
    round.** Round 1: the operator has a tracked in-scope pre-edit;
    the worker is invoked and exits non-zero AFTER `persist_state`
    has run. After the call, `state.json` on disk contains
    `implement_tracked_baseline` (a sorted `list[str]`) and it
    includes the operator's in-scope pre-edit. Round 2: re-enter
    `implement` with the on-disk state; the helper does NOT
    re-snapshot (key present); the in-scope pre-edit is still
    subtracted; the worker's round-2 tracked change lands in the
    WIP commit. Mirror the #112 `test_durable_preworker_flush_*`
    shape. Cover both `_run_agent_pair` and the TDD `run`.
  - **Clean-tree run unchanged.** A clean tree pre-worker (no
    operator modifications): the tracked baseline is empty after
    the snapshot, the out-of-scope floor passes trivially, and
    `_commit_worker_diff` stages exactly what it would have staged
    before this fix (the worker's tracked changes + new untracked,
    minus task-dir). A focused real-git test asserts the committed
    paths match the pre-fix expectation for at least one agent-pair
    and one TDD scenario.
  - **Pinned base usage (no live config bleed).** A test
    monkeypatching `phase_runners._base.pinned_base_branch` /
    `config.load_config` to raise still lets the helper run
    successfully when the caller passes the pinned string directly.
    Equivalently: a test that drives `_run_agent_pair` / `run`
    end-to-end asserts the helper observes the pinned `base_branch`
    from `state` rather than re-resolving it.
  - **`_commit_worker_diff` arg passthrough.** Calling
    `_commit_worker_diff` directly with an explicit `before_tracked`
    set containing an in-scope tracked path produces a commit that
    excludes that path even when it currently shows in
    `_tracked_changed_paths(cwd, base_branch)`, while still
    including paths NOT in `before_tracked`.
  - **Mode-neutrality / single-definition coverage.** A
    shared-helper-symbol assertion in the spirit of
    `test_shared_helper_symbols_exported_from_base` /
    `test_implement_module_imports_shared_helpers`: both
    `get_or_set_tracked_baseline` and (if relocated)
    `_tracked_changed_paths` resolve to the SAME function objects
    when imported via `phase_runners._base` and the bindings
    `phase_runners.implement` uses (one shared definition, not
    copies).
  - **No git stderr leakage / fail-closed on probe failure.** When
    the fresh tracked-diff probe raises `RuntimeError` (simulated
    via a monkeypatched `run_git_checked`), the runner returns
    `status="error"` with the existing "could not snapshot the
    working tree before implement" wording (or the analogous "could
    not verify commit integrity" path) and feedback that does NOT
    contain `fatal:` or any other git stderr substring (IR-002).
    `state["implement_tracked_baseline"]` is ABSENT on disk after
    this fail-closed path (no half-stored baseline). Cover both
    paths.

## Risks
- **Highest — dropping legitimate worker output.** An over-broad
  subtraction (e.g. forgetting to recompute `_tracked_changed_paths`
  at `_commit_worker_diff` time, or computing `now ∩ before` instead
  of `now − before`) would silently exclude a real worker change
  from the reviewed range, handing the reviewer a stale PR. The
  "worker-tracked-changes-still-land" and `_commit_worker_diff` arg
  passthrough tests are the guards; the documented by-name overlap
  residual must be carried forward verbatim in the
  `_commit_worker_diff` docstring.
- **Visible behavior change for dirty-tree runs.** A run that today
  silently sweeps the operator's pre-existing out-of-scope tracked
  WIP into the WIP commit will, after this fix, DEFER via
  `PhaseResult(status="error", ...)`. That is the intended, safer
  behavior, but it is a user-visible change — operators with a dirty
  tree outside `source_dirs + test_dir` will see the runner refuse
  until they commit or stash. Surfaced here so the human gate sees
  it before merge. (Mirrors the #112 user-visible behavior change
  for new untracked files outside scope.)
- **PR-001 self-lock — designed out, but worth a reviewer
  re-check.** The fix path is "floor on FRESH probe AND skip
  persistence on floor-fail" so a clean re-run is never blocked by
  a stale persisted baseline. If a future change accidentally moves
  the floor to run AFTER `get_or_set_tracked_baseline` /
  `persist_state`, OR sources the floor's check from the stored
  baseline rather than the fresh probe, the self-lock returns.
  The "Operator out-of-scope tracked WIP → fail closed AND no
  persisted baseline" test (specifically its follow-up clean re-run
  succeeds assertion) is the regression guard.
- **Adversarial #117 residual — explicitly OUT OF SCOPE.** A worker
  with `Write` / `Bash` can rewrite
  `state["implement_tracked_baseline"]` between rounds to inject
  paths into the baseline, masking arbitrary pre-edits from the
  commit step. Layer 1 of the post-commit gate
  (`_uncommitted_scope_files`, baseline-INDEPENDENT) still catches
  uncommitted source/test residuals; out-of-scope poisoning is the
  same class as the #112 PR-002 adversarial residual and is
  reserved for the follow-up trust-model issue.
- **Documented by-name overlap (carried forward).** The tracked
  baseline is a snapshot by NAME. If the operator pre-edits a
  tracked in-scope file AND the worker then ALSO modifies that
  same path, the path is in `before_tracked`, so
  `now − before_tracked` excludes the worker's change to it — the
  symmetric mirror of the #112 untracked-side limitation at
  `implement.py:151-157`. The out-of-scope floor removes the most
  damaging case (the file is out-of-scope) but the residual
  in-scope overlap remains. Closing it would require
  content-hashing the whole tracked set, judged disproportionate.
  The human can override at the gate if the tracked overlap is
  judged common enough to warrant content-hashing — that would
  expand the scope of this task.
- **Helper-location choice (relocate vs re-import
  `_tracked_changed_paths`).** Option (a) (re-import the existing
  `implement._tracked_changed_paths`) and option (b) (move to
  `_base.py`) are both viable. Option (b) is the cleaner mirror of
  the #112 setup (`untracked_files` lives in `_base.py`) and is
  the preferred default; option (a) is the smaller diff. The
  implementer picks one and stays within it; the human can pick at
  the gate. Either way, only one definition survives.
- **Persist call collapsing.** The two pre-worker baseline
  snapshots (untracked + tracked) should share a SINGLE
  `persist_state` call (one disk write, both keys present). If the
  implementer instead emits two `persist_state` calls, behavior is
  still correct but the `updated_at` timestamp and any future
  parity test would see two writes; the implementer should prefer
  one call for clarity. Surface here so the reviewer doesn't flag a
  single-write refactor as scope creep.
- **Dogfooding hazard (operational).** This fix is dogfooded
  through the same `implement` runner it changes. Run on a CLEAN
  tree to avoid the very dirty-tree case under change biting the
  dogfood. A mid-flight interrupt on the new test file could touch
  the (now-fixed) #112 hazard — operational note only.
- **Reviewed-range / commit-integrity security boundary.** Same
  class as #112 / #50 / #39. **plan_review FIRST before any code
  lands.** Do not direct-edit; this must clear the cross-provider
  adversarial reviewer.
- **Line numbers in the brief.** Cited line numbers (`:113-130`,
  `:133-185`, `:151-157`, `:316-317`, `:363`, `:460-461`, `:485`,
  `orchestrator.py:916-990`) are from current `main` and may drift;
  the implementer should re-locate by symbol
  (`_tracked_changed_paths`, `_commit_worker_diff`,
  `_run_agent_pair`, `run`, `get_or_set_untracked_baseline`,
  `persist_state`, `pinned_base_branch`, `_ensure_task_branch`),
  not by absolute line.
