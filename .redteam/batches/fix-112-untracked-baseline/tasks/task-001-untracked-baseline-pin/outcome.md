# Outcome — Pin the implement untracked baseline once per task (#112)

## Goal
Capture the implementer's "before-untracked" baseline ONCE per task at the first
`implement` entry, **durably persist it to `state.json` BEFORE the worker is
invoked**, and re-use it on every later round (including after an orchestrator
crash/kill/relaunch BETWEEN the worker creating an untracked file and
`_commit_worker_diff` running) — so a NEW file the implementer created in a
prior, interrupted round is correctly re-attributed, committed, and lands in
the reviewed `<base>...HEAD` range instead of dead-ending at `deferred`. A
user's genuine pre-task untracked scratch keeps being excluded, and the
post-commit integrity gate is upgraded to a TWO-LAYER union: the existing
source/test floor (baseline-INDEPENDENT, unchanged) AND a new check for stray
new files OUTSIDE `source_dirs`/`test_dir` (baseline-RELATIVE, with the same
`_in_task_dir` + gitignored exclusions `_commit_worker_diff` applies), so the
realistic non-adversarial #112 surface (a migration/config/fixture left
uncommitted) is no longer silently passed over.

## Done-when
- [ ] `bash .redteam/scripts/verify.sh` exits 0 (ruff check + ruff format
      --check + full pytest).
- [ ] A new pure-persistence helper `persist_state(task_dir, state) -> None`
      is exported from `.redteam/workflows/phase_runners/_base.py`. It performs
      the **same atomic state-write** that `orchestrator.save_state` does
      today — set `state["updated_at"] = utc_now()` (the same `utc_now()`
      indirection both writers share), write `state.json.tmp`, then
      `os.replace` to `state.json` — and NOTHING ELSE. It does NOT render
      `progress.md` (progress.md stays orchestrator-only). Pure stdlib (no new
      runtime dependency).
- [ ] `orchestrator.save_state` is refactored to delegate the atomic write
      to `phase_runners._base.persist_state`, then call `_write_progress`
      exactly as today (wrapped in the existing best-effort `except Exception`
      swallow). For a given `state` dict and a frozen `utc_now`, the bytes
      `orchestrator.save_state` writes to `state.json` are byte-identical to
      the bytes a separate `persist_state` call writes, and `save_state` still
      produces a `progress.md` file. A test under `.redteam/tests/` asserts
      both halves of that parity using a monkeypatched `utc_now` so the
      `updated_at` stamp is deterministic.
- [ ] The existing
      `test_save_state_persists_even_if_progress_render_fails` in
      `.redteam/tests/test_progress_surface.py` still passes UNCHANGED:
      monkeypatching `orchestrator._write_progress` to raise must still leave
      `state.json` written and `save_state` non-raising.
- [ ] A new shared helper
      `get_or_set_untracked_baseline(state, cwd) -> set[str]` is exported
      from `.redteam/workflows/phase_runners/_base.py`. **Set-once semantics
      keyed solely on the PRESENCE of `state["implement_untracked_baseline"]`
      as a list — NO other prior-run signal is consulted.** Given a `state`
      dict that already contains a `list` at
      `state["implement_untracked_baseline"]`, the helper returns that list
      as a `set`, does NOT call `untracked_files(cwd)`, and does NOT mutate
      the stored value even when the live untracked set differs from the
      persisted one. Otherwise it calls `untracked_files(cwd)` exactly
      once, stores the result back into `state["implement_untracked_baseline"]`
      as a sorted `list[str]`, and returns the matching set. The helper itself
      does NOT persist — its caller is responsible for the durable flush, so
      persistence is explicit at the call site (mirroring the IR-006
      pre-worker discipline).
- [ ] BOTH `_run_agent_pair` and the TDD `run` path in
      `.redteam/workflows/phase_runners/implement.py` obtain `before_untracked`
      from `get_or_set_untracked_baseline(state, rr)` BEFORE invoking the
      worker, then **immediately call `persist_state(task_dir, state)` BEFORE
      `get_worker_adapter(state).invoke(...)`** so the baseline survives a
      crash that kills the worker between file-create and `_commit_worker_diff`.
      A grep for `untracked_files(` inside `implement.py` finds it only via
      the shared helper (no surviving per-round live capture site).
- [ ] No legacy prior-run signal detector is introduced. The implement
      runners do NOT branch on `implement_round_count`, on `phases_completed`,
      on `verification.last_run_at`, on `task_dir/impl_diff.patch` existence,
      or on "commit beyond pinned base" as a way to refuse fresh-snapshotting
      a key-absent state. Key-absent legacy state simply takes the
      first-entry snapshot via the helper above; the two-layer integrity gate
      below (in particular the baseline-INDEPENDENT source/test floor) is the
      safety net that makes a pre-fix legacy file under `source_dirs` /
      `test_dir` dead-end VISIBLY rather than be silently re-masked.
- [ ] The local `set` returned by `get_or_set_untracked_baseline` is the
      value passed to BOTH `_commit_worker_diff` AND the new outside-scope
      gate layer (see below). After the worker returns, NEITHER the commit
      step NOR the gate re-reads `state["implement_untracked_baseline"]` and
      NEITHER re-calls `untracked_files(cwd)` for the baseline (same-round
      TOCTOU safety / IR-006). `_commit_worker_diff`'s computation is
      unchanged: it still computes
      `new_untracked = current_untracked - before_untracked`.
- [ ] `state.template.json` is NOT updated to seed
      `implement_untracked_baseline`. The key stays absent on a fresh task so
      the helper's "missing → first-entry snapshot" branch is the only
      initializer (no `[]` or `null` default that would either sweep
      pre-existing scratch into the commit or force a fragile `None`-vs-`list`
      special case at every caller).
- [ ] **Integrity gate is a UNION of two layers** invoked post-`_commit_worker_diff`
      in BOTH `_run_agent_pair` and the TDD `run`. A stray-list aggregated from
      both layers (sorted, deduplicated) drives the existing `status="error"`
      / "stale committed range" / "commit these or remove them" feedback path.
      The existing fail-closed-on-failed-probe semantics (status="error",
      "could not verify commit integrity", no stderr leakage) are preserved
      verbatim across both layers.
    - **Layer 1 — source/test floor (BASELINE-INDEPENDENT, UNCHANGED).**
      `implement._uncommitted_scope_files(cwd, proj)` is preserved
      verbatim — signature, body, docstring, behavior. It still checks
      staged-but-uncommitted + tracked unstaged + non-ignored untracked,
      restricted to `proj.source_dirs` / `proj.test_dir`, baseline-INDEPENDENT.
      The existing test
      `test_uncommitted_scope_files_ignores_artifacts_outside_source_dirs`
      stays GREEN UNMODIFIED. This layer guarantees a pre-fix legacy (or
      adversarially mis-baselined) source/test file is still flagged today
      (dead-ends VISIBLY), so the brief's "fail closed or degrade safely"
      constraint is honored for the realistic #112 surface (the masked file
      in this issue is a NEW TEST under `test_dir`).
    - **Layer 2 — outside-source/test widening (BASELINE-RELATIVE, NEW).**
      A new private helper in `.redteam/workflows/phase_runners/implement.py`
      (e.g. `_uncommitted_outside_scope_files(cwd, task_dir, proj, baseline)
      -> list[str]`) returns the sorted list of paths that are
      `current_untracked - baseline` BUT NOT under `proj.source_dirs` or
      `proj.test_dir` AND NOT under `_in_task_dir(task_dir, cwd)` AND not
      gitignored (re-use the existing `--exclude-standard` semantics
      `untracked_files` already applies). It is fail-closed on a failed git
      probe (raise `RuntimeError` on non-zero exit, omit stderr — IR-002),
      stdlib-only, and uses the SAME exclusion symbols as
      `_commit_worker_diff` so its view of "outside scope" matches the
      commit surface symbol-for-symbol. The runners pass the SAME
      `before_untracked` set + the SAME `task_dir` Path here that they
      passed to `_commit_worker_diff`.
- [ ] BOTH `_run_agent_pair` and the TDD `run` invoke the two layers as a
      union, sort+dedup the combined stray list, and on a non-empty result
      return a `PhaseResult(status="error", ...)` whose feedback (1) names
      the stray files, (2) carries the existing "stale committed range" /
      "commit these or remove them" intent, and (3) does NOT leak git
      stderr. The feedback wording is updated so it remains correct under
      the union: where today's wording says "implement left source/test
      changes uncommitted ...", it now says (substring) "implement left
      changes uncommitted after the [WIP] commit, so the reviewed range
      `git diff <base>...HEAD` would be STALE relative to the tree
      verification just passed on. Uncommitted: <files>. Commit these
      (they belong in the implementation diff), or remove them — refusing
      to hand a stale committed range to review." A test asserts the
      feedback names a stray outside-scope file flagged by Layer 2 (so the
      wording is verified to NOT be source/test-only when a Layer 2-only
      violation fires).
- [ ] Engine remains stdlib-only: no new pip dependency is introduced.

## Out of scope
- The complementary fix from issue #112 of auto-staging stray untracked files
  that are listed in `outcome.md`'s Affected files — not this task.
- #109 (worker timeout / interrupt-robustness elsewhere) — separate bug.
- The documented by-name-diff limitation in `_commit_worker_diff`'s docstring
  (a pre-existing untracked file modified IN PLACE is not detected) — unchanged.
- The `verify_test` / `write_test` phase's own untracked handling — not touched.
- Moving `_write_progress` into `_base.py` — explicitly OUT; progress.md
  rendering stays orchestrator-only and `persist_state` is pure atomic write.
- Migration / rewrite of any prior `state.json` files; behavior only needs to
  be deterministic when the key is absent.
- Updating `state.template.json` to declare the key (intentional — see
  Done-when above and PR-003 in Risks).
- **Any "legacy prior-run signal" detection branch** — `implement_round_count`,
  `phases_completed`-membership, `verification.last_run_at`,
  `task_dir/impl_diff.patch` existence, "commit beyond pinned base", or any
  variant thereof. The operator's settled design (R3) is to use NONE of these
  as gating signals. Key-absent state is fresh-snapshotted; the source/test
  floor is the safety net for the realistic regression surface, and the
  outside-scope residual below is accepted in the SAME class as the
  adversarial poisoning out-of-scope item.
- **Adversarial baseline poisoning via a worker that out-of-band rewrites
  `state.json` between rounds.** A worker with Write/Bash CAN today rewrite
  the on-disk `state["implement_untracked_baseline"]` between rounds to
  inject its own new file into the baseline, which would mask that file from
  `_commit_worker_diff` AND the Layer 2 (baseline-relative) gate; Layer 1
  (baseline-INDEPENDENT, source/test) still catches it within source/test.
  Fully closing the outside-scope attack surface requires a baseline trust
  root the worker cannot move (blocking worker writes to `state.json`, or
  integrity-stamping the baseline with out-of-tree key material) — that is a
  change to the implement worker TRUST MODEL, broader than this
  crash-robustness fix, and is reserved for a separate follow-up issue the
  operator will file. Same-round TOCTOU within a single process is defeated
  by passing the in-memory `set` to both `_commit_worker_diff` and the gate
  (see Done-when above).
- Hash- or HMAC-based tamper detection on the persisted baseline — see the
  adversarial-out-of-scope item above; not added here.
- **Accepted residual: a one-time, pre-fix-state-only window in which a
  task-created file OUTSIDE `proj.source_dirs` / `proj.test_dir` was already
  untracked at the next post-fix `implement` entry.** The next entry takes a
  fresh first-entry snapshot, that file lands IN the new baseline, and is
  therefore excluded from both `_commit_worker_diff` and Layer 2. Layer 1
  does not see it (it is outside source/test by construction). This residual
  is in the SAME class as the adversarial poisoning above (an outside-scope
  file admitted into the baseline), is bounded to tasks already mid-implement
  at deployment time, closes after the first post-fix `implement` entry per
  task (because `persist_state` writes the baseline BEFORE `worker.invoke`),
  and does NOT affect the source/test surface this issue is about — that
  surface is protected by Layer 1's baseline-INDEPENDENT floor.

## Affected files
- `.redteam/workflows/phase_runners/_base.py` — add
  `persist_state(task_dir, state) -> None` (pure atomic write of
  `state.json`, no `progress.md`) and
  `get_or_set_untracked_baseline(state, cwd) -> set[str]` (set-once
  read-or-snapshot of `state["implement_untracked_baseline"]`, returns a
  `set`, stores a sorted `list[str]`, does NOT persist). Both live next to
  the existing `untracked_files` / `commit_paths` helpers so the implement
  paths and the orchestrator import them from the same place. `utc_now()`
  is imported from (or moved/re-exported alongside) `orchestrator` so a
  single indirection is monkeypatchable in the parity test; if a re-export
  is impractical without circularity, replicate the one-line
  `datetime.now(timezone.utc).isoformat()` body verbatim in `_base.py` so
  the bytes match.
- `.redteam/workflows/orchestrator.py` — `save_state` (currently L177) is
  refactored to delegate the atomic write to
  `phase_runners._base.persist_state`, then call `_write_progress` exactly
  as today (same best-effort `except Exception` swallow). No behavior
  change visible to callers; the imports section gains the `persist_state`
  import.
- `.redteam/workflows/phase_runners/implement.py` —
    1. Replace the two live `before_untracked = untracked_files(rr)`
       capture sites in `_run_agent_pair` (currently ~L275) and the TDD
       `run` (currently ~L412) with a `get_or_set_untracked_baseline(state,
       rr)` call followed immediately by `persist_state(task_dir, state)`
       BEFORE the `get_worker_adapter(state).invoke(...)` line. The OSError
       / RuntimeError fail-closed handler currently wrapping the snapshot
       is preserved (it now wraps both the get-or-set and the
       `persist_state` call, since either can raise OSError on disk
       failure).
    2. Do NOT add any legacy prior-run signal branch. The "key absent →
       fresh snapshot" path runs unconditionally for fresh state and for
       any pre-fix legacy state.
    3. Add a small private helper
       `_uncommitted_outside_scope_files(cwd, task_dir, proj, baseline)
       -> list[str]` (Layer 2): probe `git ls-files --others
       --exclude-standard -z` once, subtract `baseline`, exclude paths
       under `task_dir` (use the existing `_in_task_dir` symbol or
       replicate its computation), exclude paths under any
       `proj.source_dirs` root or `proj.test_dir`, fail-closed on a
       non-zero git exit (raise `RuntimeError`, omit stderr — IR-002).
       Stdlib-only.
    4. After `_commit_worker_diff` runs in BOTH paths, compute
       `layer1 = _uncommitted_scope_files(rr, proj)` (UNCHANGED) and
       `layer2 = _uncommitted_outside_scope_files(rr, task_dir, proj,
       before_untracked)`. Union, sort, dedup. If non-empty, return
       `PhaseResult(status="error", ...)` with the updated feedback (see
       Done-when) and no stderr leakage. The existing
       fail-closed-on-failed-probe `except (OSError, RuntimeError)` block
       is widened to also catch a Layer 2 probe failure (same "could not
       verify commit integrity" wording, no stderr leakage). Update the
       in-runner comments at L333–337 (agent-pair) and L447–449 (tdd) to
       describe the union scope rather than "source/test only".
    Re-locate by symbol if line numbers have drifted.
- `.redteam/tests/test_implementer_commit.py` — UNCHANGED. The existing
  `test_uncommitted_scope_files_ignores_artifacts_outside_source_dirs`,
  `test_pre_existing_untracked_is_not_swept_into_the_commit`,
  `test_real_git_commits_new_untracked_and_excludes_task_artifacts`, and
  `test_real_git_tdd_implement_commits_out_of_root_file_into_committed_range`
  must stay green WITHOUT MODIFICATION. New regressions go in the new file
  below; this file is not edited.
- `(new) .redteam/tests/test_implement_untracked_baseline_pin.py` —
  regression tests covering the helper directly, the durable pre-worker
  flush + crash/restart on BOTH implement paths, the legacy fresh-snapshot
  behavior (no prior-run signal detector), the source/test floor catching a
  pre-fix legacy source/test file, the Layer 2 outside-scope widening
  (flagged-when-uncommitted, passes-when-committed, user-scratch-outside-
  scope NOT flagged), set-once idempotency, the updated feedback wording,
  and the `save_state` ↔ `persist_state` parity. Sits next to
  `test_implementer_commit.py` under `.redteam/tests/` and matches the
  `test_*.py` pattern.

## Verification

```yaml
commands:
  - "bash .redteam/scripts/verify.sh"
```

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full suite (ruff check + ruff format
  --check + pytest) must pass.
- `pytest .redteam/tests/test_implementer_commit.py -q` — every test stays
  green WITHOUT MODIFICATION; in particular
  `test_uncommitted_scope_files_ignores_artifacts_outside_source_dirs`,
  `test_pre_existing_untracked_is_not_swept_into_the_commit`,
  `test_real_git_commits_new_untracked_and_excludes_task_artifacts`, and
  `test_real_git_tdd_implement_commits_out_of_root_file_into_committed_range`
  pass unchanged.
- `pytest .redteam/tests/test_progress_surface.py -q` — the `save_state` /
  `progress.md` surface tests stay green (in particular
  `test_save_state_persists_even_if_progress_render_fails`).
- `pytest .redteam/tests/test_agents_generic_prompts.py -q` — guards agent
  bodies stay project-agnostic; the fix must not push project fingerprints
  into them.
- `pytest .redteam/tests/test_base_branch_pin.py -q` — the #91
  base-branch-pin tests stay green; this fix does not touch
  `_writable_phase_started` or its call site.

### To be created (the implementer phase will define exact test names)
Tests live under `.redteam/tests/` and match `test_*.py`. Behavioral scope:
- **Helper unit — key absent.** `get_or_set_untracked_baseline(state, cwd)`
  with no `implement_untracked_baseline` key calls `untracked_files(cwd)`
  exactly once, stores a sorted `list[str]` at
  `state["implement_untracked_baseline"]`, and returns the matching set.
  Assert the helper does NOT itself write `state.json` (no `persist_state`
  call inside the helper).
- **Helper unit — key present (set-once).** With
  `state["implement_untracked_baseline"]` already a list and a DIFFERENT
  live untracked set on disk, the helper returns the stored list as a set,
  does NOT call `untracked_files(cwd)`, and does NOT mutate
  `state["implement_untracked_baseline"]`. Assert this idempotency is
  independent of `implement_round_count`, of `phases_completed`, and of
  `verification.last_run_at` (varying those does not change the helper's
  decision — there is no legacy signal detector).
- **Durable pre-worker flush — REAL git, BOTH implement paths.** Stub the
  worker so its `invoke()` creates a new untracked file under
  `source_dirs` (or `test_dir`) AND then raises (or is killed) AFTER
  file-create, BEFORE `_commit_worker_diff`. After the call returns, read
  `state.json` from disk and assert `implement_untracked_baseline` is
  present, is a sorted `list[str]`, and DOES NOT contain the just-created
  file. Then re-enter `implement` (worker now a no-op, the file still
  untracked from the prior round): assert the file lands in
  `git diff <base>...HEAD` and the (two-layer) integrity gate returns
  clean. Cover both `_run_agent_pair` and the TDD `run`.
- **Legacy/no-key + a source/test file already untracked → Layer 1 floor
  flags it (dead-ends).** With `implement_untracked_baseline` absent AND a
  source/test file already untracked on the tree (simulating the pre-fix
  legacy crash window's residual), the runner takes the fresh-entry
  snapshot, the snapshot includes the file (so `_commit_worker_diff` skips
  it), and the source/test floor `_uncommitted_scope_files` STILL flags it
  → `PhaseResult(status="error", ...)` with the "stale committed range"
  feedback. This proves "no silent masking" and matches today's behavior
  on the realistic #112 surface. Cover both `_run_agent_pair` and the TDD
  `run`.
- **Layer 2 outside-scope widening.** Drive the runner end-to-end against
  REAL git so a NEW file the implementer created OUTSIDE
  `source_dirs`/`test_dir` (e.g. `migrations/0001.sql`) and left
  uncommitted in the KEY-PRESENT flow (i.e. the file is NOT in the
  persisted baseline) is flagged by Layer 2. Three sub-cases:
    1. Same migration file properly committed by `_commit_worker_diff`
       passes the gate.
    2. A user's pre-existing scratch OUTSIDE source/test (e.g.
       `notes.txt`) that is IN the baseline is NOT flagged by Layer 2.
    3. A task-dir artifact (e.g. `<task_dir>/impl_diff.patch`) is NOT
       flagged by Layer 2.
- **Set-once idempotency under round changes.** With
  `implement_untracked_baseline` present, increment `implement_round_count`
  by hand between two helper invocations and assert the baseline is not
  re-snapshotted in either call.
- **Pre-existing user scratch under no special dir stays excluded from
  the commit across ≥2 rounds.** Extends the existing
  `test_pre_existing_untracked_is_not_swept_into_the_commit` scenario
  across at least two rounds: a user file present at the FIRST entry is
  captured into the persisted baseline and stays out of the staged /
  committed set across at least two subsequent rounds, even after the
  worker creates new files in those rounds. The existing single-round
  test continues to pass unchanged.
- **Updated feedback wording (no source/test-only claim under Layer 2).**
  When Layer 2 alone fires (a file ONLY outside source/test is
  uncommitted), the resulting `PhaseResult.feedback` names the offending
  file, carries the "stale committed range" intent, and is consistent with
  a union gate (it does NOT claim "source/test changes uncommitted" when
  no source/test file is involved). When Layer 1 alone fires, the feedback
  remains consistent with today's wording. When both layers fire, both
  sets of files are named. No variant leaks git stderr. Cover both
  `_run_agent_pair` and the TDD `run`.
- **`save_state` ↔ `persist_state` parity (deterministic clock).**
  Monkeypatch the shared `utc_now` indirection used by both writers to a
  fixed value so the `updated_at` stamp is deterministic. Assert:
    (a) `orchestrator.save_state(td, st)` and a separate
        `_base.persist_state(td, st)` call (on a clean dict copy with the
        same fixed clock) write byte-identical `state.json` content;
    (b) `orchestrator.save_state(td, st)` still produces a `progress.md`
        file;
    (c) `test_save_state_persists_even_if_progress_render_fails` continues
        to pass — monkeypatching `_write_progress` to raise still leaves
        `state.json` written and `save_state` non-raising.
- **Shared-helper coverage.** Both implement paths route through the SAME
  symbol — assert by importing `get_or_set_untracked_baseline` and
  `persist_state` directly AND by exercising `_run_agent_pair` and the
  TDD `run` through the existing `_load_implement_module` +
  monkeypatched `subprocess.run` style used in `test_implementer_commit.py`,
  confirming the same persisted behavior on both sides.

## Risks
- **Decision (operator-settled, R3) — two-layer integrity gate.** Layer 1
  (the source/test floor) is `_uncommitted_scope_files` UNCHANGED, BASELINE-
  INDEPENDENT. Layer 2 is a new BASELINE-RELATIVE check restricted to paths
  OUTSIDE `source_dirs`/`test_dir`, with the SAME `_in_task_dir` +
  gitignored exclusions as `_commit_worker_diff`. The two are unioned at
  the runner level. This preserves today's "fail-closed for source/test
  even on legacy snapshots" guarantee (the #112-realistic surface) while
  also closing the non-adversarial outside-scope coverage gap (the PR-002
  intent). The settled trade-off is: the gate is STRICTER than
  `_commit_worker_diff` within source/test (baseline-INDEPENDENT) and
  EQUAL to `_commit_worker_diff` outside source/test (baseline-RELATIVE).
- **Decision (operator-settled, R3) — drop ALL legacy signal detection.**
  The plan does NOT consult `implement_round_count`, `phases_completed`,
  `verification.last_run_at`, `task_dir/impl_diff.patch`, or "commit beyond
  pinned base" to decide whether a key-absent state is fresh or legacy.
  Reviewer R1/R3 ruled out each of these in turn (PR-004 / PR-005), and
  the source/test floor renders such detection unnecessary for the surface
  this issue is about. Key-absent → fresh snapshot; the floor catches
  legacy source/test residuals; the outside-scope residual is accepted
  (next item).
- **Accepted limitation — one-time pre-fix residual OUTSIDE source/test.**
  A task-created file OUTSIDE `source_dirs`/`test_dir` that was already
  untracked at the next post-fix `implement` entry will be included in the
  fresh first-entry snapshot, so it is excluded from
  `_commit_worker_diff` AND Layer 2 (and Layer 1 does not see it). This is
  in the SAME class as the adversarial poisoning out-of-scope item (an
  outside-scope file admitted into the baseline). It is bounded to tasks
  already mid-implement at deployment time and closes after the first
  post-fix `implement` entry per task (because `persist_state` writes the
  baseline BEFORE `worker.invoke`). The source/test surface this issue is
  about is NOT affected by this residual.
- **Decision (PR-002 adversarial half) — explicitly OUT OF SCOPE.** A
  worker with Write/Bash can rewrite the on-disk
  `state["implement_untracked_baseline"]` between rounds. Layer 2 cannot
  defeat this (it is baseline-relative); Layer 1 still catches such an
  attack within source/test. Closing the outside-scope attack surface is
  a TRUST-MODEL change (block worker writes to `state.json`, or
  integrity-stamp the baseline with out-of-tree key material) reserved for
  a separate follow-up issue. Same-round TOCTOU within a single process
  is defeated by passing the in-memory `set` to both `_commit_worker_diff`
  and the gate.
- **Decision (PR-003, retained) — `state.template.json` does NOT seed the
  key.** A default of `[]` would make a fresh task's baseline
  "present-and-empty" on the very first round, so
  `new_untracked = current - {}` would sweep the user's genuine
  pre-existing scratch into the commit (regression of
  `test_pre_existing_untracked_is_not_swept_into_the_commit`). A default
  of `null` would force every consumer to special-case `None` vs `list`.
  The key is absent on a fresh task; the helper's
  "missing → first-entry snapshot" branch is the only initializer.
- **Decision — shared `persist_state` in `_base.py`, NOT a runner-local
  atomic write, and NOT `progress.md` inside `_base.py`.** The implement
  runner imports `persist_state` from `_base.py`; the runner does NOT
  duplicate the atomic-write logic. `progress.md` rendering stays in
  `orchestrator.py` (operator surface + best-effort swallow are
  orchestrator concerns, not phase-runner concerns).
- **`utc_now` indirection for the parity test.** The parity test needs a
  SINGLE monkeypatchable clock symbol both writers consult. If
  `_base.persist_state` cannot import `orchestrator.utc_now` without a
  circular import, the implementer should either move `utc_now` to a
  neutral location (e.g. `_base.py`) and have `orchestrator` import it
  from there, or replicate the one-line body in `_base.py` and patch BOTH
  symbols in the parity test. Pick one and stay within it; the human can
  weigh in at the gate if the choice is not obvious in code.
- **Test-file placement preference.** The Affected files list nominates a
  new `test_implement_untracked_baseline_pin.py`. The test-writing phase
  MAY instead place the `save_state` ↔ `persist_state` parity assertions
  in the existing `test_progress_surface.py` (the canonical home of
  `save_state` tests) while keeping the implement-path regressions in the
  new file. The budget covers either layout so long as every new test
  file matches `test_*.py` under `.redteam/tests/`. The human can pick at
  the gate; otherwise the implementer chooses one layout and stays within
  it.
- **Dogfooding hazard (operational, not scope).** This very fix is
  dogfooded through `implement`, which today carries #112. If the
  implementer creates the new test file and the orchestrator is
  interrupted before `_commit_worker_diff` runs, the run itself may hit
  the dead-end the fix targets (worked around in prior slices via a
  manual commit + hand-advance). The fix's scope is unchanged.
- **Line numbers in the brief.** Cited line numbers (~L177 for
  `save_state`, ~L186 for `_uncommitted_scope_files`,
  ~L275/~L281/~L321/~L339/~L412/~L436/~L452 for the implement paths,
  L307–313 for the `verify_allowlist`-missing branch,
  L333–337/L447–449 for the in-runner gate comments, L347–353/L456–462
  for the integrity-gate feedback strings) are from current `main` and
  may drift; the implementer should re-locate by symbol (`save_state`,
  `_run_agent_pair`, `_commit_worker_diff`, `_uncommitted_scope_files`,
  `run`, `before_untracked = …`, the integrity-gate feedback strings)
  not by absolute line, as the brief itself notes.
