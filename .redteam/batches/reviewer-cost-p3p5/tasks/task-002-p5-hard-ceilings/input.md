# Task 002 — P5: Hard ceilings on the review loop

## Parent context

This is the second of two tasks that close the remaining proposals of issue
#92. It **stacks on task-001's branch** (round-staged reviewer / P3). Both
tasks touch the review-invocation path in the orchestrator and the review
runner; stacking prevents conflicting edits. Assume the P3 config keys and
round-routing seam from task-001 are already in place — extend them, do not
duplicate them.

## What to build

Add configurable **hard ceilings** on top of the existing retry/rescue ladder
so a rare reviewer↔worker ping-pong has a bounded tail cost. Concretely:

- **Max review rounds per task.** An opt-in config key (e.g. under `[models]`
  or a new sibling section — decide in planning, consistent with the shape
  chosen for P3) that caps the total number of review rounds for a single
  task, independent of the existing retry ladder. On hitting the ceiling, the
  loop must terminate deterministically (fail-closed, no silent approval) —
  typically by deferring to the human gate / draft-PR checkpoint, mirroring
  how the engine already handles the rescue-entry ceiling in
  `orchestrator.py`.
- **Max wall-clock per task.** A second opt-in config key that caps
  cumulative wall-clock time spent in review for a task. Use `time.monotonic`
  (stdlib) — no new deps. Persist accrued time across resumes so the ceiling
  survives a restart; the batch state seam under `.redteam/batches/<batch>/`
  is the right place, following whatever pattern the orchestrator already
  uses for per-task counters (retries, round numbers, etc.).
- **Prompt caching of the fixed reviewer prompt portion.** Investigate whether
  the CLI adapters (`.redteam/workflows/adapters/claude.py`,
  `.redteam/workflows/adapters/codex.py`) actually expose a caching control at
  the CLI seam. If yes, implement it minimally. **If no** (which is the likely
  outcome for CLI-driven adapters), document why in a short `docs/decisions/`
  note and leave it out — do not fake it, do not ship a stub. The plan must
  state this determination explicitly before any code is written.

## Approval-authority invariant (still HARD)

The P3 rule from task-001 remains in force: cheap first-pass reviewers may
reject early but must NEVER approve. Additionally:

- Hitting the max-rounds ceiling must NEVER be interpreted as approval. It
  must route to a deferred / human-gate outcome (or fail-closed) — never to a
  silent pass.
- Hitting the wall-clock ceiling has the same rule: no silent approvals under
  time pressure.

Regression tests must lock this in: a scenario where the ceiling triggers on a
cheap-reviewer round must end in a non-approved terminal state, and a
frontier-reviewer round at the ceiling must also end non-approved.

## Files this task most likely affects

- `.redteam/workflows/config.py` — new opt-in ceiling keys, fail-loud
  validation, defaults that keep today's behavior (no ceiling → unbounded, as
  today).
- `.redteam/workflows/phase_runners/review_code.py` — enforce the round /
  wall-clock ceilings at the review dispatch seam.
- `.redteam/workflows/orchestrator.py` — persist per-task accrued review time
  and round counter across resumes (mirror the existing retry-counter
  pattern); route the ceiling termination through the same fail-closed /
  deferred outcome the retry-ceiling already uses.
- `.redteam/workflows/adapters/claude.py`, `.../codex.py` — ONLY if the
  prompt-caching investigation says the CLI seam actually exposes a caching
  control; otherwise leave untouched and record the decision.
- `.redteam/tests/` — new tests covering: ceiling reached → non-approved
  terminal state; wall-clock accrual survives resume; defaults reproduce
  today's behavior; cross-provider guard still intact; interaction with P3
  staging (cheap reviewer + ceiling both configured).
- Possibly `docs/decisions/<date>-reviewer-prompt-caching.md` for the
  documented no-op if caching is not implementable.

## Constraints inherited from the parent goal

- **Default behavior unchanged.** No new config keys set → pipeline behaves
  exactly as today, incl. no artificial cap on rounds or wall-clock.
  Regression-test the default path.
- **Engine stays project-agnostic and zero-runtime-deps** (stdlib only —
  `time.monotonic`, `json`, etc.).
- Adapter trust model, verification allowlist, snapshot / fail-closed logic,
  and cross-provider pairing guard remain **security boundaries** — do not
  loosen; `plan_review` will scrutinize any change near them.
- Config loader stays **fail-loud** on unknown keys / bad types.
- **No changes to the worker (implementer) side of the loop.**

## Non-goals for this task

- Do not re-open P3's routing decisions; extend the seams task-001
  established.
- Do not touch P4 (static-analysis offloading) or the native-diff adapter /
  #120 context-narrowing coupling.
- Do not implement speculative caching if the CLI adapters have no caching
  control — a documented "not implementable at the adapter layer" note is the
  correct outcome, per the parent goal.
- Do not change the worker adapters or the implement/rescue phases beyond the
  minimum needed to persist a review-round counter / accrued time.
