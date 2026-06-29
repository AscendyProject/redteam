# Outcome — Harden the implement untracked/tracked baselines against an adversarial worker + the pre-#112 in-flight migration window (#117)

## Goal
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

## Converged framing (must appear verbatim in `plan_review.md` and in a code comment at the floor site)
> Same-user, stdlib-only, worker-needs-Bash ⇒ tamper PREVENTION of the on-disk
> baseline is impossible. The target is fail-closed tamper-DETECTION on
> cross-run consumption. Trust is re-derived from (a) the live outside-scope
> untracked surface AND (b) the stored baseline contents — outside-scope paths
> never legitimately appear in either, so the union of those two probes
> catches both the "future-create" and "leave-on-disk" variants of an
> adversarial baseline rewrite.

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

## Out of scope
- HMAC / orchestrator-held secret / integrity stamp of the baseline — replaced
  by the stored-baseline-contents floor (PR-001 round-3 resolution).
- Worker-side `--disallowedTools` / `--permission-mode plan` / `env=` scrub on
  the worker invocation in `run_claude` or `ClaudeWorkerAdapter.invoke` —
  defense-in-depth only, bypassable via Bash; not the trust root.
- Any check inside `orchestrator.py:load_state` — runs before
  `_ensure_task_branch` / config load, so it cannot trust the checkout.
- A structural "is this path currently untracked?" per-baseline-entry probe
  to distinguish worker-poisoned entries from legitimately pre-existing
  outside-scope untracked entries — superseded by the
  stored-baseline-contents floor (no outside-scope path is ever legitimately
  in a stored baseline, so the discrimination question dissolves).
- OS-level worker sandbox (containers, seccomp, read-only mounts) or any
  `sys.platform` branch — violates project-agnostic + stdlib-only.
- The reviewer/Codex adapter (`ClaudeReviewerAdapter`, `CodexReviewerAdapter`)
  — already hardened; not touched.
- #91 base-branch pinning, `pinned_base_branch`, the freeze guard, and the
  `_tracked_changed_paths` / `_commit_worker_diff` attribution mechanics —
  only ADD the cross-run trust-root floor; do not re-litigate them.
- The Layer-1 (`_uncommitted_scope_files`) and Layer-2
  (`_uncommitted_outside_scope_files`) gate shapes — preserved exactly.
- Reintroducing any #112-refuted legacy signal (`implement_round_count`,
  commit-beyond-base, `verification.last_run_at`) — the new floor is purely
  worktree-shape + stored-baseline-contents-shape and consults none of these.
- Changing the default worker permission mode or any broader refactor of the
  worker adapter.
- New fields in `.redteam/templates/state.template.json` or the task-state
  schema — none needed.
- The `_base.py` helpers `get_or_set_untracked_baseline`,
  `get_or_set_tracked_baseline`, and `persist_state` — their signatures and
  set-once-by-KEY-PRESENCE semantics stay byte-for-byte stable. The floor is
  layered ABOVE them inside the two implement runners, not inside the
  helpers.
- A worker that ALSO commits a smuggled outside-scope file onto the task
  branch — the file is then inside `base...HEAD` and reviewed by definition;
  not this task's concern.

## Affected files
- `.redteam/workflows/phase_runners/implement.py` — add (a) a module-level
  in-memory marker `set[Path]` of resolved `task_dir`s whose baselines this
  process has validated; (b) a new helper that runs the cross-run trust-root
  floor (live outside-scope untracked + stored-baseline-contents checks,
  re-using `_scope_root` and the `_in_task_dir` POSIX-prefix logic — one
  definition, no duplication); (c) in BOTH `_run_agent_pair` AND `run` (TDD),
  after the existing tracked `_floor_outside_scope` call and BEFORE the
  `get_or_set_tracked_baseline` / `get_or_set_untracked_baseline` /
  `persist_state` block, apply the new floor IFF `task_dir` is not yet in the
  marker; on the success path add `task_dir` to the marker. The existing
  tracked `_floor_outside_scope` call stays unchanged. Pass the
  already-computed `untracked_files(rr)` set into
  `get_or_set_untracked_baseline` via the existing `_untracked_fn` seam
  (`_untracked_fn=lambda _cwd: current_untracked_set`) so git is not
  re-probed on the key-absent path. Add a short comment block at the floor
  site quoting the "Converged framing" verbatim.
- `(new) .redteam/tests/test_baseline_trust_root_cross_run.py` — adversarial
  cross-run + migration regression tests for the new floor. Written by the
  agent-pair implementer (this repo runs in agent-pair mode per `CLAUDE.md`).
  Canonical project test location (`.redteam/tests/`), matching the project's
  `test_*.py` pattern. Sits next to `test_implement_untracked_baseline_pin.py`.

(Explicitly NOT to be touched by this task:
`.redteam/workflows/orchestrator.py`, `.redteam/workflows/phase_runners/_base.py`
— the `get_or_set_*_baseline` helpers, `persist_state`, and `run_claude` stay
byte-for-byte stable for THIS task — the floor lives in `implement.py`;
`.redteam/workflows/adapters/claude.py`, `.redteam/templates/state.template.json`,
`.redteam/config.toml`, `.redteam/docs/*`.)

## Verification

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### Notes
- `bash .redteam/scripts/verify.sh` is the project verify command
  (ruff check + `ruff format --check` + full pytest over `.redteam/`).
  It is the single command the orchestrator snapshots and runs at the
  verification step; every existing regression file below is part of that
  pytest run, so the one verify command covers them all.

### Existing (must continue to pass under the verify run)
The following existing files must stay green and un-rewritten (only fixture
additions strictly required by the new floor are acceptable; assertion shapes
stay intact):
- `.redteam/tests/test_implement_untracked_baseline_pin.py` — #112 untracked
  baseline + two-layer gate.
- `.redteam/tests/test_tracked_baseline_attribution.py` — #91 Part A tracked
  baseline + tracked floor.
- `.redteam/tests/test_implementer_commit.py` — `_commit_worker_diff` + #82
  commit discipline.
- `.redteam/tests/test_preimplement_snapshot_invariant.py` — IR-006 / #39
  pre-implement snapshot.
- `.redteam/tests/test_base_branch_pin.py` — #91 Part B pin.
- `.redteam/tests/test_pinned_base_freeze_guard.py` — #91 freeze guard.
- `.redteam/tests/test_tdd_commit_discipline.py` — #82 TDD commit discipline
  (mode-neutral — the new floor runs in BOTH paths).
- `.redteam/tests/test_progress_surface.py` — `save_state` / `progress.md`
  parity (this task does not touch `persist_state`).
- `.redteam/tests/test_agents_generic_prompts.py` — engine + agent bodies stay
  project-agnostic.

### To be created (the test-writing phase — here, the agent-pair implementer — defines exact test names)
The new file under `.redteam/tests/` (`test_*.py`) must cover:
- **Adversarial cross-run, file ALREADY on disk (agent-pair):** a worker
  poisons `state["implement_untracked_baseline"]` on disk to include an
  outside-`source_dirs`/`test_dir`, outside-`task_dir` path AND that path is
  ALREADY present in the worktree as an untracked file. Simulate a fresh
  orchestrator process by clearing the module-level marker. On the next
  implement entry, the runner returns `PhaseResult(status="error", …)` whose
  feedback names the offending path(s); `get_worker_adapter(...).invoke` is
  NOT called; no baseline is re-stamped (state on disk is unchanged for the
  baseline keys); the poisoned file is NOT silently smuggled out of the
  reviewed `base...HEAD` range.
- **Adversarial cross-run, file ABSENT at entry / created later — PR-001
  round-3 variant:** a worker poisons `state["implement_untracked_baseline"]`
  on disk to include `scratch/secret.txt` while that path is ABSENT from the
  worktree at restart. Simulate a fresh process (marker cleared). On the
  next implement entry, the live floor would pass but the
  stored-baseline-contents floor fires: the runner returns
  `status="error"` naming `scratch/secret.txt`; the worker is NOT invoked;
  the runner returns BEFORE any opportunity for the worker to create the
  file; baseline keys on disk are unchanged. This is the explicit
  PR-001 round-3 regression — assert it.
- **Adversarial cross-run via TRACKED baseline, file ABSENT at entry:** same
  shape, but the poison is in `state["implement_tracked_baseline"]` and the
  outside-scope path is absent from `_tracked_changed_paths(rr, base_branch)`
  at restart. The stored-baseline-contents floor must catch it for the
  tracked baseline as well — the runner returns `status="error"` naming the
  path; the worker is NOT invoked.
- **Adversarial cross-run (TDD path):** repeat the absent-at-entry untracked
  variant through the TDD `run` path; same outcome.
- **Pre-#112 in-flight migration window:** a state with NO
  `implement_untracked_baseline` key AND NO `implement_tracked_baseline` key,
  `phases_completed` populated by a prior process, AND an outside-scope
  untracked file already in the worktree (worker-created before the pre-#112
  crash). On a fresh process's first implement entry (marker empty), the
  runner returns `status="error"` naming the path; the first-entry
  set-once snapshot is NOT taken (assert
  `state["implement_untracked_baseline"]` remains absent on disk after the
  call); a clean re-run after the operator commits/stashes the file
  succeeds.
- **Healthy cross-run resume (no false positive):** a key-PRESENT untracked
  AND tracked baseline on disk whose stored contents are clean of
  outside-scope paths, AND a CLEAN outside-scope untracked surface in the
  worktree (only `task_dir`-scoped and/or in-scope untracked files exist).
  A fresh process's first implement entry passes the floor, reuses the
  stored baselines UNCHANGED (the lists on disk before and after
  `get_or_set_*_baseline` are byte-identical), and the implement round
  proceeds normally.
- **Healthy in-process multi-round (no false positive):** within ONE
  process, implement round 1 stamps the marker; round 2 skips the floor
  entirely (no re-probe of the worktree or the stored baselines for the
  trust-root check). Verify the in-memory baseline `set` passed to
  `_commit_worker_diff` is identical across rounds; verify no in-process
  re-read of `state.json` happens between baseline-set and baseline-consume
  (e.g. by intercepting `load_state` / `Path.read_text` for the state path
  during the rounds).
- **Fresh TDD task (no false positive):** `write_test` commits the test
  inside `test_dir` first (tracked, in-scope), then implement runs in the
  same process. The trust-root floor passes (no outside-scope untracked from
  the test, no outside-scope path in baselines), the marker is stamped, and
  the round returns `status="approved"` (or `"changes_requested"` for an
  unrelated verify outcome) — never `status="error"` for a baseline / floor
  reason.
- **`task_dir` scratch is exempt:** an untracked file under
  `.redteam/batches/<batch>/tasks/<task>/` (e.g. an operator note,
  `verification.log`, the in-flight `impl_diff.patch`) does NOT trip the
  live floor; an entry under `task_dir` POSIX-prefix in a stored baseline
  does NOT trip the stored-baseline-contents floor. The exemption mirrors
  the one used by `_floor_outside_scope`, `_commit_worker_diff`, and
  `_uncommitted_outside_scope_files`.
- **In-scope untracked NOT flagged by the floor:** an untracked file inside
  `source_dirs` or `test_dir` does NOT trip the new floor — it flows through
  the existing #112 Layer-1 / `_commit_worker_diff` paths unchanged.
- **Visible dead-end on the failure path:** the offending paths from BOTH
  the live floor AND the stored-baseline-contents floor appear verbatim
  (de-duplicated, sorted) in the returned `feedback` (operator-readable);
  the runner returns BEFORE the worker is invoked; `persist_state` is NOT
  called with a new baseline on the failure path (baseline keys that were
  absent on disk before the call remain absent after).
- **Contract preservation under the new floor:** `_commit_worker_diff` is
  still called with the caller's in-memory `before_untracked` /
  `before_tracked` sets (the test inspects the call args via a stub /
  monkeypatch); `_uncommitted_scope_files` is still called with no baseline
  argument (Layer-1 stays baseline-INDEPENDENT); `_uncommitted_outside_scope_files`
  stays `task_dir`-exempt + baseline-relative.
- **No HMAC, no env-scrub, no load_state mutation regression markers:** the
  test suite asserts (or the test file imports + spot-checks) that no new
  HMAC key field is read from or written to `state.json`, that the worker
  `subprocess.Popen` (via `run_claude`) is invoked with no new `env=` kwarg
  and no new tool-deny flag, and that `orchestrator.load_state` does not
  perform any git probe or baseline trust check.
- **Same-process sibling-task isolation (goal-mode):** in ONE process, two
  sibling tasks each hit implement; each stamps its OWN `task_dir`; the
  trust-root floor decision for task B does not bleed across to task A (and
  vice versa). The marker is keyed by resolved `task_dir`.

## Risks
- **Behavior change for operator scratch outside `task_dir`.** The new floor
  fires on every implement entry whose `task_dir` was NOT stamped by the
  current process — cross-run resume, pre-#112 migration, AND the first
  implement entry of a brand-new task in a fresh process. An operator who
  keeps untracked scratch OUTSIDE `task_dir` AND OUTSIDE
  `source_dirs`/`test_dir` (e.g. a top-level `TODO.txt` or an unrelated
  scratch dir at repo root) will see `status="error"` until they commit or
  stash. This is symmetric with the existing tracked `_floor_outside_scope`
  (which already refuses outside-scope tracked WIP) and is per the
  operator's steering ("demanding a clean outside-scope untracked surface,
  exactly like the #91/#112 'locked behavior'"). The human resolves at the
  draft-PR gate.
- **Stored-baseline-contents floor's invariant.** The floor's correctness
  rests on the invariant "a legitimately-stored baseline never contains an
  outside-scope, outside-`task_dir` path", which holds because (a) the
  pre-worker tracked `_floor_outside_scope` already refuses outside-scope
  tracked changes BEFORE `get_or_set_tracked_baseline` snapshots, and (b)
  the new untracked floor refuses outside-scope untracked files BEFORE
  `get_or_set_untracked_baseline` snapshots. If a future change ever lets a
  baseline be persisted before the corresponding floor runs (out of order),
  the invariant breaks. This task must NOT change the snapshot ordering;
  any future change to the implement runner that re-orders these steps must
  re-validate this invariant.
- **In-process re-read assumption.** The HMAC drop relies on the fact that
  `state.json` is loaded exactly once per process (verified:
  `load_state` is called only at `orchestrator.py:1026` inside
  `process_task`, and `_commit_worker_diff` + Layer-2 take the baseline as
  in-memory arguments). If a future change adds an in-process re-read of
  `state.json` between baseline-set and baseline-consume, the in-process
  vector reopens and HMAC must be reconsidered. This task must NOT
  introduce such a re-read.
- **Decision-record placement.** The "Converged framing" block must land
  somewhere durable. The committed plan is to embed it verbatim as a code
  comment at the floor site in `implement.py` AND verbatim in
  `plan_review.md`. Whether to additionally add a
  `docs/decisions/2026-…-baseline-trust-root.md` file is a human decision at
  the draft-PR gate; if yes, the file must be added to "Affected files"
  before the implementer may touch it.
- **A worker that ALSO commits the smuggled file is out of this task's
  scope.** A worker that creates a file outside `source_dirs`/`test_dir`,
  commits it onto the task branch, AND poisons the baseline so
  `_commit_worker_diff`'s `current − baseline` set excludes the file is
  irrelevant to this floor (the file is committed → already inside
  `base...HEAD` → already in the reviewed range). This is the honest limit
  of the present design and is acceptable because the reviewer sees the
  commit.
- **Single-symbol floor location.** The new trust-root floor helper lives
  alongside `_floor_outside_scope` in `implement.py`. If a future task wants
  to share it with `_base.py`, that relocation is a separate decision —
  this task keeps the helper in `implement.py` to stay surgical.
- **Security boundary — `plan_review` FIRST.** This change touches the
  worker trust model and the reviewed-range integrity guarantee. No direct
  edits; the outcome must clear cross-provider adversarial review before
  any code lands.
