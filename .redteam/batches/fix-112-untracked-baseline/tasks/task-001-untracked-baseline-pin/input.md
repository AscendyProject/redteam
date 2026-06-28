# Pin the implement untracked baseline once per task (#112)

## Goal
A NEW file the implementer creates in an `implement` round must never be permanently
masked out of the committed review range when the orchestrator process is interrupted
(kill / detach / monitor relaunch / crash) between the worker creating the file and the
WIP commit. After the fix, a restart re-enters `implement`, re-attributes the
task's own previously-created-but-uncommitted file, commits it, and the integrity gate
(#50) passes — while a user's genuine pre-task untracked scratch is still excluded.

## What to build
Today `implement._run_agent_pair` (and the TDD `_run_implement` path) capture
`before_untracked = untracked_files(rr)` **fresh at the start of every round**, right
before invoking the worker. `_commit_worker_diff` then stages
`new_untracked = current_untracked - before_untracked`. This per-round re-capture
conflates two different things:
- the user's pre-existing untracked scratch (must STAY excluded), and
- a file *this task's own implementer* created in a PRIOR round that never got committed
  because the process died before `_commit_worker_diff` ran.

After a restart the latter is already in the tree, lands in `before_untracked`, and is
masked out of `new_untracked` forever. The integrity gate then correctly flags it as a
stray uncommitted scope file and fails the phase — but the harness owns commits, not the
implementer, so every retry re-captures the same masking baseline and re-defers. The
task dead-ends at `deferred` with "commit these" feedback the implementer is
structurally unable to satisfy.

Fix (per the issue): capture the untracked baseline **once per task, at first
`implement` entry, and persist it in `state.json`** (e.g.
`state["implement_untracked_baseline"]`, stored as a sorted list since JSON has no set),
instead of re-capturing it live each round. On every subsequent round / after a restart,
LOAD the persisted baseline instead of re-snapshotting. Then
`new_untracked = current_untracked - persisted_baseline` correctly attributes everything
the task created across all rounds and across a restart, while still excluding the user's
genuine pre-task scratch. Apply to BOTH implement paths (agent-pair and TDD) via a single
shared helper so the two cannot drift.

## Constraints
- **Security boundary — touches the reviewed-range integrity contract (cf. #50/#91).
  plan_review FIRST before any code.**
- Engine stays project-agnostic and stdlib-only (zero runtime deps). No project/stack
  fingerprints in `.redteam/workflows/`.
- Preserve the existing exclusions that already have tests: the task-dir scratch filter
  (`_in_task_dir`), GITIGNORED files (`--exclude-standard`), and a user's pre-existing
  untracked scratch must all keep behaving exactly as before
  (`test_pre_existing_untracked_is_not_swept_into_the_commit` must stay green).
- The baseline must be captured at the FIRST implement entry only — once set in
  `state.json`, later rounds must not overwrite it (idempotent set-once).
- Consider the trust model: the worker has Write/Bash and runs every round. The baseline
  is consumed each round from `state.json`. Spell out in the plan whether a worker that
  mutates `state.json` between rounds could poison the baseline (mask its own new file
  out of the reviewed range), and if so, how the fix stays fail-closed against that —
  mirror the #91/IR-006 "pin a pre-worker snapshot the worker can't move" discipline.
- Legacy in-flight tasks (state.json predating this key) must fail closed or degrade
  safely, not crash — match how the existing `verify_allowlist`-missing legacy branch is
  handled.

## Out of scope
- The complementary idea in the issue (have the harness auto-stage stray untracked files
  that fall within outcome.md's Affected files) — NOT this task; keep the fix to the
  baseline-pin.
- #109 (worker timeout) — separate interrupt-robustness bug, do not touch.
- The known by-name-diff limitation for a pre-existing untracked file that is later
  modified in place (documented in `_commit_worker_diff`'s docstring) — unchanged.

## Affected files
- `.redteam/workflows/phase_runners/implement.py` — the two `before_untracked =
  untracked_files(rr)` capture sites (agent-pair ~L275, TDD ~L412) and the
  `_commit_worker_diff` signature/usage at ~L321 / ~L436. Likely a new small helper to
  get-or-set the persisted baseline.
- Possibly `.redteam/workflows/phase_runners/_base.py` if the get-or-set helper belongs
  with the other shared git/state helpers (`untracked_files` lives there).
- New/extended test file under `.redteam/tests/` for the regression.

## Verification
- `bash .redteam/scripts/verify.sh` (ruff check + ruff format --check + full pytest)
  stays green; the current suite must not regress.
- A new regression test reproduces the issue deterministically with REAL git (only the
  worker + verify stubbed, in the style of the existing implement tests): a file left
  untracked at round start (simulating a prior interrupted round that this task created)
  is committed into the range on the next round and the integrity gate passes — i.e. it
  appears in the committed `base...HEAD` range, NOT deferred.
- A test asserting the inverse still holds: a user's genuine pre-existing untracked
  scratch (NOT created by this task — i.e. present before the persisted baseline was set)
  stays excluded from the commit.
- A test asserting set-once idempotency: the baseline persisted on the first round is not
  overwritten on a later round even though the live untracked set has changed.

## Risks
- Baseline poisoning via worker-mutated `state.json` (see Constraints) — the plan must
  resolve whether this is a real hole and pick a fail-closed design; the human will
  weigh the chosen approach at the gate.
- This very fix is dogfooded through `implement`, which today carries the #112 bug, so
  the dogfood run itself may hit the dead-end on a new test file across an interrupted
  round (worked around in prior slices via a manual commit + hand-advance). That is an
  operational note for the run, not a change to the fix's scope.
- Exact line numbers above are from the current `main` and may shift; the planner/
  implementer should re-locate by symbol, not line.
