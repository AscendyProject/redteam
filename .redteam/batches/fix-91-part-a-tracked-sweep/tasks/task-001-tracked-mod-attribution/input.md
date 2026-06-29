# Attribute tracked changes to the worker: pre-worker tracked baseline + out-of-scope fail-closed (#91 Part A)

## Goal
On a **dirty-tree** run, the operator's pre-existing *tracked* modifications must
NOT be swept into the task's WIP commit / PR. Today `_commit_worker_diff` stages
`_tracked_changed_paths(cwd, base_branch)` — ALL tracked changes vs the pinned
base — with no attribution to who made them. `_ensure_task_branch` stashes the
operator's local changes and **pops them back onto the task branch**
(`orchestrator.py:916-990`, steps 1 + 5), so any pre-existing operator tracked
WIP is in the worktree during phases and gets committed as if the worker wrote it
(`implement.py:133-185`, `_commit_worker_diff` → `_tracked_changed_paths`
`:113-130`). This is the **tracked-file mirror of the bug #112 fixed for
untracked files** — and the fix is the symmetric mirror of that solution.

After the fix, only the changes the **worker** actually made (tracked or newly
created) land in the commit/PR; the operator's pre-existing tracked WIP stays in
the worktree, untouched and uncommitted, exactly as the `before_untracked`
baseline already does for untracked scratch.

## What to build — mirror the #112 untracked baseline, for tracked changes
#112 already established the pattern this should copy 1:1:
- `get_or_set_untracked_baseline(state, cwd, ...)` + `persist_state(task_dir, state)`
  are called **before the worker** in BOTH implement paths (`implement.py:316-317`
  agent-pair, `:460-461` TDD); `_commit_worker_diff` stages
  `untracked_files(cwd) - before_untracked` so operator scratch is never swept
  (`implement.py:175`).
- The helpers live in `_base.py`: `get_or_set_untracked_baseline:843`,
  `persist_state:826`, `untracked_files:696`, `utc_now:820`. The baseline is
  **set-once by KEY PRESENCE** (durable across resume; no signal detection — the
  #112 lesson).

Build the tracked analog:
1. A **pre-worker tracked baseline**: capture `_tracked_changed_paths(cwd,
   base_branch)` (the set of tracked paths already changed vs the pinned base
   BEFORE the worker runs) into `state` via a new set-once helper (e.g.
   `get_or_set_tracked_baseline`), persisted with `persist_state` at the SAME
   pre-worker point as the untracked baseline, in BOTH implement paths. Set-once
   by key presence (survives an interrupted/resumed round), mirroring #112 —
   absolutely **no prior-run signal detection**.
2. In `_commit_worker_diff`, stage tracked paths = `_tracked_changed_paths(now)
   − before_tracked` (then `+ new_untracked` as today, with the same task-dir
   filter). Operator's pre-existing tracked mods are subtracted; the worker's own
   tracked changes (new this round) are kept.
3. **Out-of-scope fail-closed floor (the security floor):** if the pre-worker
   tracked baseline contains any path **outside** the project scope
   (`proj.source_dirs` + `proj.test_dir`; same roots `_uncommitted_scope_files`
   computes at `implement.py:241`), the run must **fail closed** (return a
   PhaseResult error → the orchestrator defers) with a clear message telling the
   operator to commit or stash their unrelated tracked WIP before running —
   rather than silently proceeding on a contaminated tree. This is the issue's
   explicit alternative, used here as a floor on top of the baseline subtraction,
   not a replacement (mirrors the #112 baseline-independent floor).

## The known limitation to carry forward (be explicit, like #112 did)
The baseline is a snapshot by **NAME**. If the operator modified a tracked file
in place BEFORE the worker AND the worker then *also* modifies that same file,
the path is in `before_tracked`, so `now − before_tracked` would exclude the
worker's change to it. #112 documented the exact symmetric limitation for
untracked files (`implement.py:151-157`) and judged closing it (content-hashing
the whole set) disproportionate. **Carry the same documented limitation here**
unless plan_review decides the tracked overlap is common enough to warrant
content-hashing — call that out, do not silently leave it undocumented. (The
out-of-scope fail-closed floor already removes the most damaging case; the
residual overlap is an in-scope tracked file the operator pre-edited, which is
itself anomalous since the worker is scoped to outcome.md's Affected files.)

## Constraints
- **Reviewed-range / commit-integrity security boundary — plan_review FIRST
  before any code.** (Same class as #112 / #50 / #39.)
- Engine stays project-agnostic and **stdlib-only (zero runtime deps)**.
- Mirror the existing helpers' style exactly (`get_or_set_untracked_baseline` /
  `persist_state` / `_tracked_changed_paths`): set-once by key presence, NUL-safe,
  `core.quotepath=false`, fail-closed on git errors (raise, not partial read).
- Preserve the pinned `base_branch` usage (#91 Part B, already merged) — the
  baseline and the staging set both read the pinned base, never live config.
- Mode-neutral: both agent-pair and TDD implement paths get the same treatment
  (TDD inherits the same behavior today — #82, no regression).
- Do not weaken the #112 two-layer untracked integrity gate or the `new_untracked`
  staging; this is additive for tracked attribution.
- Don't change `_ensure_task_branch`'s stash/pop — the operator's WIP must still
  be preserved in the worktree (just not committed).

## Out of scope
- **#91 Part B** (pin `base_branch` end-to-end) — already merged (`274e02d`).
- The untracked baseline / #112 two-layer gate — done; only ADD the tracked
  analog, don't refactor the untracked path beyond what sharing a helper cleanly
  requires.
- `#117` adversarial-worker `state.json` poisoning of the baseline — that
  trust-boundary residual is a separate issue; this task assumes a non-adversarial
  operator (the realistic dirty-tree case), same threat model as #112's main fix.
- Any change to what the pr-author stages at PR time (the task_dir decision trail
  is intentional).

## Affected files
- `.redteam/workflows/phase_runners/_base.py` — new `get_or_set_tracked_baseline`
  helper alongside `get_or_set_untracked_baseline`. (Confirm the exact signature
  in plan_review — it needs the pinned `base_branch` and a fail-closed tracked-diff
  probe; consider whether `_tracked_changed_paths` moves/shares here or stays in
  implement.py.)
- `.redteam/workflows/phase_runners/implement.py` — capture + persist the tracked
  baseline pre-worker in BOTH paths (next to `before_untracked`, `:316-317` /
  `:460-461`); subtract it in `_commit_worker_diff` (`:133-185`); add the
  out-of-scope fail-closed check. Re-locate by symbol, not line number.
- New/extended test under `.redteam/tests/` (e.g.
  `test_tracked_baseline_attribution.py`).

## Verification
- `bash .redteam/scripts/verify.sh` (ruff check + ruff format --check + full
  pytest) stays green; no existing test regresses (esp. the #112 untracked-baseline
  tests and the integrity-gate tests).
- New deterministic tests (drive real git in a tmp repo or monkeypatch the git
  probes, matching the existing `test_install` / baseline test style; no model
  calls):
  1. **Operator out-of-scope tracked WIP → fail closed.** A tracked file modified
     vs base outside source/test scope present before the worker → implement
     returns an error/defer, and that file is NOT staged/committed.
  2. **Operator in-scope pre-existing tracked mod is not attributed to the
     worker** when the worker doesn't touch it (subtracted via the baseline) —
     OR, per the chosen design, document/test the locked behavior.
  3. **Worker's own tracked changes still land.** Files the worker modifies/creates
     this round (not in the pre-worker baseline) are committed and appear in
     `impl_diff.patch` / the reviewed range — the fix must not drop legitimate
     worker output.
  4. **Set-once / resume.** The tracked baseline is captured once by key presence
     and survives a simulated interrupted-then-resumed round (mirror the #112
     baseline-pin test).
  5. **Clean-tree run unchanged.** With a clean tree pre-worker (the normal case),
     behavior is byte-identical to today (baseline empty → nothing subtracted).

## Risks
- **Dropping legitimate worker output (highest):** an over-broad subtraction or
  scope check could exclude a file the worker really changed → a stale reviewed
  range / incomplete PR. Test 3 is the guard; the name-based overlap limitation
  must be documented (above).
- **Behavior change for dirty-tree runs:** the fail-closed floor makes a run that
  previously (silently, wrongly) swept operator WIP now DEFER. That is the
  intended, safer behavior, but it is a visible change — surface it in outcome.md
  Risks so the gate sees it.
- **Set-once durability vs adversarial poisoning:** the set-once-by-key-presence
  baseline can be poisoned by a worker that writes `state.json` — explicitly the
  #117 residual, out of scope here (same boundary #112 drew).
- Exact line numbers are from current `main` and may shift; locate by symbol.
- Dogfooded through the same implement/commit path it changes; run on a CLEAN
  tree to avoid the very dirty-tree case under change biting the dogfood. A
  mid-flight interrupt on the new test file could touch the (now-fixed) #112
  hazard — operational note only.
