## What
On a CROSS-RUN load (a new orchestrator process consuming a `state.json` written
by an earlier process), re-derive trust from the LIVE worktree **and** from the
STORED baseline contents — not from the stored baseline alone. At implement
runner entry, when the `task_dir` is NOT yet in a module-level in-memory marker
set, the runner applies a **cross-run trust-root floor** with TWO checks:

1. **Live floor** — the current outside-`source_dirs`/`test_dir` untracked
   surface (with the same `task_dir` POSIX-prefix exemption used by
   `_floor_outside_scope`, `_commit_worker_diff`, and
   `_uncommitted_outside_scope_files`) must be empty.
2. **Stored-baseline contents floor** — `state["implement_untracked_baseline"]`
   AND `state["implement_tracked_baseline"]`, if key-present, must contain **no
   path outside `source_dirs`/`test_dir` and outside `task_dir`**. (A
   legitimately-stored baseline never contains such a path: the existing
   pre-worker tracked floor + the new untracked floor both require a clean
   outside-scope surface BEFORE the set-once snapshot is taken, so any
   outside-scope path appearing in a stored baseline is by construction a
   worker-injected entry.)

Either check failing → return
`PhaseResult(status="error", feedback=…, log=…, diff="")` whose feedback names
every offending path, BEFORE `get_worker_adapter(state).invoke(...)` is called
and WITHOUT calling `persist_state` with a new baseline. Both checks passing →
proceed through `get_or_set_tracked_baseline` / `get_or_set_untracked_baseline`
exactly as today AND stamp `task_dir` into the in-memory marker.

This closes BOTH the "poisons baseline AND leaves an untracked file" vector
(caught by the live floor) AND the "poisons baseline with an outside-scope path
while the file is absent at entry, then creates it during the round" vector
(caught by the stored-baseline contents floor — explicit PR-001 round-3 fix).
The pre-#112 in-flight migration window collapses into the same live floor: a
worker-created outside-scope untracked file already in the worktree at
restart, with no baseline key on disk, trips the live floor on the first
implement entry of the new process.

## Why
Closes #117. PR #116 (#112) and PR #121 (#91 Part A) deliberately carved out
two residual trust-boundary surfaces outside `source_dirs`/`test_dir`: an
adversarial worker that mutates `state.json` between runs can silently shrink
the reviewed range via the baseline, and a pre-#112 in-flight migration
window can re-snapshot a worker-created untracked file into a fresh first-entry
baseline on restart. The human asked for a security-boundary design that
fails closed on CROSS-RUN consumption (same-user, stdlib-only,
worker-with-Bash → tamper PREVENTION is impossible, so the target is
tamper-DETECTION). The chosen mechanism layers a live + stored-baseline-contents
floor at implement-runner entry, keyed by a process-local marker so healthy
in-process retries pay zero cost.

## Done-when
- [ ] `bash .redteam/scripts/verify.sh` exits 0 (ruff + `ruff format --check` +
      full pytest over `.redteam/`).
- [ ] A new `test_*.py` file under `.redteam/tests/` (matching the project's
      `test_*.py` pattern) covers the behavioral scope listed under "Verification
      → To be created" and is picked up by the pytest run.
- [ ] BOTH `phase_runners.implement._run_agent_pair` AND
      `phase_runners.implement.run` (the TDD path) apply the cross-run
      trust-root floor when the runner-resolved `task_dir` is NOT yet in a
      module-level in-memory marker set. The floor performs BOTH the live-floor
      check AND the stored-baseline-contents check described in "Goal" and
      uses the SAME shape as the existing `_floor_outside_scope`:
      POSIX-prefix `source_dirs` + `test_dir` scope roots, plus the `task_dir`
      POSIX-prefix exemption (same `_in_task_dir` logic used by
      `_commit_worker_diff` and `_uncommitted_outside_scope_files`).
- [ ] On the failure path the runner returns
      `PhaseResult(status="error", feedback=…, log=…, diff="")` whose
      `feedback` string names EVERY offending path from BOTH the live floor
      AND the stored-baseline-contents check (de-duplicated, sorted). The
      runner returns BEFORE `get_worker_adapter(state).invoke(...)` is called.
      NO baseline is stamped on the error path (`persist_state` is NOT called
      with a new baseline; if neither baseline key was previously present on
      disk, the keys remain absent on disk after the call so a clean re-run
      after the operator commits/stashes is never self-locked — same shape as
      #91 Part A's PR-001 fix).
- [ ] On the success path the runner stamps the runner-resolved `task_dir`
      into the module-level marker set BEFORE returning from the implement
      phase, so a same-process retry round bypasses the floor entirely.
- [ ] The marker is IN-MEMORY ONLY. A grep over `.redteam/workflows/` shows
      the marker symbol is never written to `state.json`, never serialized via
      `json.dumps`, never added to `state.template.json`, and never read from
      disk. It is a module-level `set[Path]` (or equivalent), reset only by
      process exit. The marker is keyed by the resolved `task_dir` `Path` so
      same-process sibling tasks in goal-mode do not cross-pollinate.
- [ ] `orchestrator.py:load_state` is NOT modified by this task (round-2
      PR-002 fix). No trust probe, no git probe, no baseline check inside
      `load_state` — the check is at baseline consumption in the implement
      runner, AFTER `process_task` has called `_ensure_task_branch` and the
      pinned base is in state. A diff of
      `.redteam/workflows/orchestrator.py` against the parent branch shows
      zero hunks inside `load_state`.
- [ ] `_commit_worker_diff` still accepts explicit `before_untracked` and
      `before_tracked` arguments and performs NO in-function re-read of
      `state.json` or any baseline key (#91 contract). `pinned_base_branch`
      is still read via the state-dict the caller passes.
- [ ] `_uncommitted_scope_files` (Layer-1) keeps its current `(cwd, proj)`
      signature with NO baseline / state argument added — it stays
      baseline-INDEPENDENT (#112 contract). `_uncommitted_outside_scope_files`
      (Layer-2) keeps its current signature, stays baseline-relative, stays
      `task_dir`-exempt.
- [ ] No HMAC / orchestrator-held secret is introduced. A grep over
      `.redteam/workflows/` for `hmac` and any `baseline_hmac` / `_baseline_hmac`
      symbol finds nothing; the only `hashlib` use stays the existing
      `sha256` on `impl_diff.patch` in `implement.py`. Justification: with the
      stored-baseline-contents floor, any outside-scope path in a stored
      baseline is detected on cross-run consumption regardless of whether a
      MAC was forged; HMAC dissolves under a same-user worker with Bash and
      brings no security gain that the contents floor does not already deliver.
- [ ] No worker-side `--disallowedTools` / `--permission-mode plan` / `env=`
      scrub is added to `run_claude` in `.redteam/workflows/phase_runners/_base.py`
      OR to `ClaudeWorkerAdapter.invoke` in
      `.redteam/workflows/adapters/claude.py`. Defense-in-depth tool-deny is
      bypassable via Bash and is out of scope for this task; the worker
      adapter is not touched. A grep over `adapters/claude.py` and
      `_base.py:run_claude` shows no new `--disallowedTools` flag, no new
      `--permission-mode` flag on the WORKER subprocess, and no new `env=`
      kwarg on the worker `subprocess.Popen`.
- [ ] The engine adds no non-stdlib import and no `sys.platform` /
      Linux-only / container / seccomp / OS-fingerprint branch under
      `.redteam/workflows/`.
- [ ] `pytest .redteam/tests/test_agents_generic_prompts.py -q` stays green —
      no project / stack fingerprint leaks into agent skeletons or engine code.
- [ ] The existing #112 / #91 / #82 baseline + commit-discipline regression
      files (listed under "Verification → Existing notes") stay green WITHOUT
      having their substantive assertions rewritten (only fixture additions
      strictly required by the new floor — at most a one-line helper to stamp
      the process-local marker — are acceptable).

## Verification
- Tests: test_adversarial_untracked_baseline_file_on_disk_agent_pair, test_adversarial_untracked_baseline_absent_at_entry_agent_pair, test_adversarial_tracked_baseline_absent_at_entry_agent_pair, test_adversarial_untracked_baseline_absent_at_entry_tdd, test_pre112_migration_window_outside_scope_file_present, test_healthy_cross_run_resume_no_false_positive, test_healthy_in_process_multi_round_floor_skipped_on_round2, test_fresh_tdd_task_no_false_positive, test_task_dir_scratch_exempt_live_floor, test_task_dir_entry_in_baseline_exempt_stored_contents_floor, test_in_scope_untracked_not_flagged_by_floor, test_feedback_names_all_offending_paths_deduped_sorted, test_commit_worker_diff_receives_in_memory_baselines, test_uncommitted_scope_files_no_baseline_arg, test_uncommitted_outside_scope_files_task_dir_exempt, test_no_hmac_in_implement_module, test_no_hmac_in_workflows, test_load_state_no_git_probe, test_worker_adapter_no_new_env_kwarg, test_sibling_task_isolation_goal_mode
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff scope: `phase_runners/implement.py` (+120 lines for the floor helpers
  and call sites in both `_run_agent_pair` and the TDD `run`), a new
  `.redteam/tests/test_baseline_trust_root_cross_run.py` (+761 lines, 20 tests),
  and a 2-line fixture addition in `test_implement_untracked_baseline_pin.py`.
- Floor placement confirmed in `implement.py` only — `orchestrator.load_state`
  has zero hunks (PR-002 round-2 fix), the floor runs before baseline
  consumption in both `_run_agent_pair` and the TDD `run`.
- Both required checks present: live outside-scope untracked + stored
  `implement_untracked_baseline` / `implement_tracked_baseline` contents, with
  `source_dirs` / `test_dir` scope roots and `task_dir` exemption.
- Failure path returns BEFORE worker invocation and BEFORE `persist_state`,
  with sorted/de-duplicated offending paths in feedback (PR-001 round-3 fix).
- Marker is process-local only: module-level `set[Path]`, no workflow
  serialization, keyed by resolved `task_dir` so sibling tasks in goal-mode do
  not cross-pollinate.
- Contracts preserved: `_commit_worker_diff` still takes explicit
  `before_untracked` / `before_tracked`; Layer 1 and Layer 2 signatures
  unchanged; no HMAC, no worker permission-mode/env scrub, no non-stdlib
  import. Recorded verification: `536 passed`, `last_exit_code: 0`.
  `REVIEW_DECISION: APPROVED`.

## Generated by
redteam / batch fix-117-baseline-trust-root / task task-001-baseline-trust-root
