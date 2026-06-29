# Harden the implement untracked-baseline against an adversarial worker + the pre-fix in-flight migration window (#117)

## Goal
Close the two residual trust-boundary surfaces that #112 / PR #116 deliberately
carved out of scope. Both are confined to files **outside `source_dirs`/`test_dir`**
— the source/test surface is already fully fail-closed by the Layer-1
baseline-INDEPENDENT floor (`_uncommitted_scope_files`), which flags a masked
source/test file regardless of baseline contents. This task hardens the
**outside-source/test** residual only.

After the fix, **both** acceptance criteria hold:

1. **Adversarial worker cannot silently shrink the reviewed range via the
   baseline.** A worker that mutates `state["implement_untracked_baseline"]`
   (or `implement_tracked_baseline`) out-of-band — to inject a file it created
   into the baseline so that file is excluded from BOTH `_commit_worker_diff`
   staging (`current - baseline`) AND the Layer-2 baseline-RELATIVE gate
   (`_uncommitted_outside_scope_files`) — EITHER cannot move the baseline the
   orchestrator actually consumes, OR the orchestrator **detects the tamper and
   fails closed** (round result `status="error"`, no PR). A worker-created file
   in any location must remain attributable to the reviewed `base...HEAD` range
   (committed and reviewed) or trip a visible dead-end — never silently smuggled
   out.

2. **The pre-fix in-flight migration window fails closed or degrades safely.** A
   task that started under **pre-#112 code** (no `implement_untracked_baseline`
   key) and crashed after the worker created an untracked file outside
   source/test but before the WIP commit must NOT, on restart under the new code,
   re-snapshot that already-created file into a fresh first-entry baseline and
   re-mask it — WITHOUT false-positiving a legitimate **fresh TDD task** (where
   `write_test` commits before `implement` runs).

## What to build
The mechanism is a **security-boundary design decision**; propose it in
`plan_outcome` and let `plan_review` adversarially vet/refute it, exactly as
#112's signal candidates were refuted one by one. Do NOT pre-commit to a weak
mechanism. The deliverable is whatever survives plan_review and meets BOTH
acceptance criteria under the Constraints below. Candidate directions, each with
its KNOWN weakness to be argued, not assumed away:

- **(A) Deny the worker write access to the task state.** Add a worker-adapter
  deny for `state.json` / the task dir / `.redteam/batches/` (mirroring the
  reviewer adapter's `--disallowedTools` discipline). **Known weakness:** the
  worker keeps Bash (it needs Write/Bash to implement code), so a tool-level
  `Write`/`Edit` deny is bypassable via `Bash` (`echo > state.json`,
  `python3 -c "open(...,'w')"`). Tool-deny RAISES the bar but is not, alone, a
  cryptographic trust root. A true OS sandbox (read-only mount / seccomp /
  container) is **platform-specific and violates the project-agnostic +
  stdlib-only engine rule** — out of scope for the engine.
- **(B) Integrity-stamp the baseline (HMAC/hash the orchestrator verifies before
  consuming it each round, including after `load_state`).** **Known weakness:**
  HMAC needs a secret the worker cannot read. The worker runs as the SAME OS user
  with Bash, so any secret in a repo file OR in an env var the worker process
  inherits is readable/forgeable. A viable variant must put the secret only where
  the worker cannot reach it across runs — e.g. an orchestrator-held key combined
  with a **scrubbed worker subprocess environment** (`subprocess` `env=` that
  withholds the secret from the child) — and must solve cross-run verification
  (resume = a new process must verify a stamp written by an earlier process)
  without writing the secret somewhere the worker's Bash can read. State honestly
  in plan_review whether a same-user stdlib-only deployment can achieve a true
  cryptographic root, or whether the achievable guarantee is **tamper-DETECTION
  that fails closed** (acceptance criterion #1's second branch) rather than
  tamper-PREVENTION.
- **(C) Tamper-detection without a persistent secret.** The orchestrator
  remembers (in-memory, and/or via an integrity field it writes) the baseline it
  last wrote, and on the next read detects an out-of-band change it did not make
  → fail closed. Argue the cross-run/crash cases where the only record is the
  on-disk state the worker could also have rewritten.

For **part 2 (migration)**, the issue notes it is "likely solved together with
the baseline trust root in part 1." A clean signal that distinguishes the
ambiguous pre-fix window from a legitimate fresh TDD task (write_test commits
first) is the open sub-question. It is acceptable for plan_outcome to either
(i) solve it with the same trust-root mechanism, or (ii) propose the minimal
fail-closed/degrade-safe handling and justify it — but it must NOT reintroduce a
signal #112 already refuted without explaining why that refutation no longer
applies.

### Background — exact current behavior (verified against the tree)
- **Worker reach.** The implement worker is dispatched at `cwd = repo_root()`
  (`implement.py`: `get_worker_adapter(state).invoke(... cwd=rr)`, both agent-pair
  and tdd paths). The default worker permission mode is `"bypassPermissions"`
  (`_base.py _worker_permission_mode()`), i.e. unrestricted Write/Bash. The
  reviewer adapter is hardened (`--permission-mode plan` +
  `--disallowedTools Edit,Write,NotebookEdit`); the **worker adapter has no
  equivalent deny**. The task `state.json` lives at
  `.redteam/batches/<batch>/tasks/<task>/state.json`, under the repo root and
  therefore inside the worker's Write/Bash reach.
- **Baseline lifecycle.** `get_or_set_untracked_baseline` /
  `get_or_set_tracked_baseline` (`_base.py`) are **set-once by KEY PRESENCE**:
  key absent → snapshot now + store sorted list; key present → return the stored
  list verbatim (no re-probe). They do NOT persist; the caller calls
  `persist_state(task_dir, state)` immediately (agent-pair + tdd both persist the
  baselines BEFORE invoking the worker).
- **In-process vs on-disk.** Within ONE orchestrator run, `process_task` loads
  state once (`load_state`) and reuses the in-memory dict across retry rounds;
  `_commit_worker_diff` and the Layer-2 gate receive the baseline as an **in-memory
  argument** (`before_untracked` / `before_tracked`), NOT re-read from
  state.json. So a worker that rewrites state.json mid-round does not move THAT
  round's in-memory baseline. The durable hole is **across orchestrator runs**:
  on `resume`/restart, `load_state` reads state.json from disk as authoritative,
  and a poisoned (key-present) baseline is then returned verbatim by the set-once
  path and consumed by the next round. Note also that an ordinary post-round
  `save_state` overwrites the worker's on-disk poison with clean in-memory state,
  so the realistic durable vectors are: (a) the worker spawns a **detached
  background process** (e.g. `nohup ... &`) that rewrites state.json after
  `invoke` returns / after the process's last `save_state`, racing the next run;
  or (b) the orchestrator crashes after a worker poison but before the cleaning
  `save_state`, leaving the poison on disk for `resume`. The design must reason
  about these explicitly.
- **Migration window.** #112 deliberately **dropped all signal-based legacy
  detection** — `implement_round_count`, commit-beyond-base, and
  `verification.last_run_at` were each refuted in plan_review (round_count only
  increments inside `_commit_worker_diff`; commit-beyond-base breaks fresh TDD
  where `write_test` commits first; `last_run_at` is written after the crash
  window) — and relies on the Layer-1 floor for safety. That floor covers
  source/test; the outside-source/test file is the residual.
- **Crypto already present.** `implement.py` imports `hashlib` and already
  computes `hashlib.sha256(diff...).hexdigest()` for `impl_diff.patch`
  (`state["verification"]["last_diff_sha256"]`). `hmac` + `hashlib` are stdlib.

## Constraints
- **Security boundary — `plan_review` FIRST before any code.** This touches the
  worker trust model and the reviewed-range integrity guarantee.
- **Engine stays project-agnostic and stdlib-only (zero runtime deps).** `hmac`,
  `hashlib`, `subprocess`, `os` are stdlib. No pip dependency, no OS/platform
  fingerprint baked into `.redteam/workflows/` (no Linux-only sandbox, etc.).
- **Preserve the #112 two-layer gate exactly.** Layer 1 (`_uncommitted_scope_files`)
  MUST remain baseline-INDEPENDENT (it is the safety floor); Layer 2
  (`_uncommitted_outside_scope_files`) stays baseline-relative + task_dir-exempt.
  Do not weaken either.
- **Preserve the #91 contracts.** `_commit_worker_diff` keeps its explicit
  `before_untracked` / `before_tracked` arguments (no in-function re-read of the
  baseline from state). `base_branch` stays pinned end-to-end.
- **Preserve set-once-by-KEY-PRESENCE semantics** for the non-adversarial path:
  a legitimate resume of a healthy task must reuse its baseline unchanged (no
  re-snapshot, no false tamper trip). The hardening must not regress the #112
  durable-pin behavior or the fresh-TDD path.
- **Fail closed, never silently mask.** Any unresolved ambiguity or detected
  tamper yields `status="error"` with a feedback string naming the offending
  file(s) — the same visible dead-end the Layer-1 floor produces — not a silent
  exclusion.
- **No new false positives.** Healthy resumes, fresh TDD tasks, and an operator's
  own out-of-scope scratch files must not be flagged (respect the existing
  task_dir exemption and scope filtering).

## Out of scope
- The **source/test** surface — already fully covered by the baseline-independent
  Layer-1 floor. This task is the outside-source/test residual only.
- A full OS-level worker sandbox (containers, seccomp, read-only mounts) — these
  are platform-specific and violate the project-agnostic + stdlib-only rule.
  Tool-level deny (adapter flags) is in scope to consider; an OS sandbox is not.
- The reviewer/Codex adapter — already hardened (`--permission-mode plan`,
  `--disallowedTools`). Do not touch it.
- #91 base_branch pinning and the tracked/untracked attribution mechanics — merged
  and stable; only ADD integrity on top, do not re-litigate them.
- Changing the default worker permission mode for unrelated reasons, or any
  broad refactor of the worker adapter beyond the minimal deny/env-scrub the
  chosen mechanism needs.

## Affected files (re-locate by symbol, not line number)
- `.redteam/workflows/phase_runners/_base.py` — `get_or_set_untracked_baseline`,
  `get_or_set_tracked_baseline`, `persist_state`/`save_state`, and (if mechanism A)
  `_worker_permission_mode` / `_worker_allowed_tools` / `run_claude`'s tool flags.
- `.redteam/workflows/phase_runners/implement.py` — the pre-worker baseline
  capture + `persist_state` sites (both agent-pair and tdd paths), and wherever
  the consumed baseline must be verified before `_commit_worker_diff` /
  `_uncommitted_outside_scope_files`.
- `.redteam/workflows/orchestrator.py` — `load_state` (the cross-run authoritative
  read where a tamper check on a restored baseline would live).
- `.redteam/workflows/adapters/claude.py` — only if mechanism A adds a worker-side
  deny / scrubbed env.
- Tests under `.redteam/tests/` — add adversarial-poison and migration-window
  cases; keep `test_agents_generic_prompts.py` green (no project fingerprints).

## Verification
- `bash .redteam/scripts/verify.sh` (ruff + ruff format --check + full pytest)
  stays green; the existing #112/#91 baseline tests
  (`test_implement_untracked_baseline_pin.py`,
  `test_tracked_baseline_attribution.py`, the integrity-gate tests) must NOT
  regress.
- New tests cover: (a) a worker that poisons `implement_untracked_baseline` on
  disk between rounds cannot exclude a worker-created outside-scope file from the
  reviewed range — it is either still committed/reviewed OR the round returns
  `status="error"` naming the file; (b) a key-absent pre-fix in-flight state with
  an already-created outside-scope untracked file fails closed / degrades safely
  on restart; (c) regression: a healthy resume of a non-adversarial task reuses
  its baseline with NO tamper trip, and a fresh TDD task (write_test committed
  first) is not false-flagged.
- `test_agents_generic_prompts.py` stays green (no project/stack fingerprints in
  the engine or agent bodies).

## Risks
- **The trust-root may not be fully achievable for a same-user, stdlib-only,
  project-agnostic engine.** If plan_review concludes a true cryptographic
  prevention is impossible without an OS sandbox (out of scope), the honest
  fallback is acceptance criterion #1's second branch — tamper-DETECTION that
  fails closed — and the outcome.md must say so explicitly rather than claim
  prevention it cannot deliver. The human resolves this framing at the draft-PR
  checkpoint.
- **False-positive risk on the migration signal.** Any part-2 signal must be
  proven not to fire on a fresh TDD task; #112 already refuted three such
  signals, so a new one carries the burden of explaining why it is different.
- **Scope creep into a worker sandbox.** Keep the change surgical; resist
  redesigning the worker adapter beyond the minimal deny/env-scrub the chosen
  mechanism requires.
