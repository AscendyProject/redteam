# Slice C: hard ceilings enforcement + goal-level done-criterion

Implements **Slice C** of the goal-mode design (#94). The accepted umbrella design is
`docs/decisions/2026-06-27-goal-mode-design.md` (Piece 3); it ties into #92 Proposal 5
(hard ceilings). This slice **stacks on Slice A** (PR #111, branch
`redteam/task-001-slice-a-dag-branching`) — Slice A added the `goal.json` manifest +
layered scheduler and **parses-but-ignores** the `ceilings` block; Slice C makes that
block load-bearing and adds the goal-level done-criterion. Scheduler/safety-boundary
change (a ceiling is what stops an autonomous loop expanding unboundedly), so this task
goes through `plan_review` before any code.

## Goal
The DAG scheduler **enforces a hard `max_tasks` ceiling** on a `goal.json` manifest
(fail closed, before any task is seeded or run), and after a batch run it computes and
surfaces a **goal-level done-criterion**: the goal is complete only when **every**
manifest task reached `done` (the full draft-PR stack exists) — completion is the stack
being built, **never** auto-merge. Absent `goal.json`, behavior is byte-for-byte
unchanged. A ceiling can never be silently exceeded.

## What to build
1. **`max_tasks` ceiling — validated + enforced at manifest load (fail closed).**
   In `_load_goal_manifest` (or the scheduler entry, wherever the whole manifest is
   already validated before seeding), enforce `ceilings.max_tasks`:
   - validate shape: if `ceilings.max_tasks` is present it must be an **integer ≥ 1**
     (reject `0`, negatives, non-int, bools) — `ValueError` aborts the whole batch,
     consistent with Slice A's fail-closed manifest validation (no state seeded, no
     task run);
   - enforce: if the manifest declares **more tasks than `max_tasks`**, abort the whole
     batch fail-closed with a clear message naming the count vs the ceiling;
   - `max_tasks` absent → no task-count bound (documented; ceilings stays optional).
   This is the forward-compatible enforcement point: today the manifest is hand-fed, but
   once the Slice B decomposer generates it, this same check bounds the generated forest.

2. **Goal-level done-criterion — computed + surfaced after the run.** After the layered
   scheduler finishes a manifest batch, determine **goal-complete = all manifest tasks
   ended `done`**. Surface it distinctly in the pipeline output (`_run_pipeline`): a
   clear `GOAL COMPLETE — draft-PR stack ready for human review (N/N tasks done; not
   merged)` vs `GOAL INCOMPLETE — M/N done; blocked/deferred: <task ids>`. Make the
   determination available from `process_batch` (e.g. alongside the per-task results, or
   a small computed summary) so it is unit-testable, not only printed. **No merge, no
   gate change** — this only reports stack completeness.

3. **Ceilings shape discipline (minimal).** Keep `ceilings` an optional JSON object
   (Slice A already rejects a non-object). Beyond `max_tasks`, token/wall-clock ceilings
   are **out of scope for enforcement** in this slice — if you accept the keys at all,
   parse-and-ignore them (do not enforce); prefer the smallest surface. Let `plan_review`
   settle strict-reject-unknown-keys vs tolerate-and-ignore.

## Constraints
- **Engine stays project-agnostic; stdlib-only; zero runtime deps.** No project/stack
  fingerprints in `.redteam/workflows/`.
- **Backward compatible:** absent `goal.json`, `process_batch`/`_run_pipeline` behave
  exactly as today (flat mode unchanged; existing tests stay green). A manifest with no
  `ceilings` block, or `ceilings` without `max_tasks`, runs as it does under Slice A.
- **Preserve every Slice A invariant** (single-parent forest, pin-before-branch, freeze
  guard, `blocked_on_dependency` cascade). Ceiling enforcement is an additional
  fail-closed gate at load time — it must not alter the layered-run semantics.
- **No auto-merge** — goal-complete means the draft-PR stack is built, not merged.
- Match existing orchestrator style; minimum code (this is a small slice).

## Out of scope
- The goal→task **decomposer** + decomposition review (Slice B).
- **Token / wall-clock ceilings enforcement** (parse-tolerate only; not enforced here).
- The per-round reviewer cost work in #92 Proposals 1–4 (deterministic pre-gate, etc.).
- Any change to `_ensure_task_branch`, the `base_branch` pin, or the freeze guard
  (those are Slice A's, already shipped on this branch — do not touch them).
- Multi-parent DAGs; auto-retarget of stacked PRs.

## Affected files
- `.redteam/workflows/orchestrator.py` — `_load_goal_manifest` (validate + enforce
  `ceilings.max_tasks`), `process_batch` (compute goal-complete from the per-task
  results), `_run_pipeline` (surface the goal-level status line).
- Tests under `.redteam/tests/` — new ceiling + goal-done invariant tests.

## Verification
- `bash .redteam/scripts/verify.sh` stays green (ruff check + ruff format --check +
  pytest), including `test_agents_generic_prompts.py`, `test_install.py`, and Slice A's
  `test_goal_manifest_validation.py` / `test_goal_dag_scheduler.py`.
- New tests assert the real invariants:
  - a manifest with task-count > `max_tasks` aborts the WHOLE batch fail-closed,
    seeding NO state and running NO task;
  - `max_tasks` of `0`, negative, non-int, or boolean is rejected at load (fail closed);
  - task-count == `max_tasks` runs normally (boundary);
  - `ceilings` absent, or present without `max_tasks`, runs exactly as under Slice A
    (no new bound) — backward-compat;
  - goal-complete is True only when every manifest task is `done`; a single
    `blocked_on_dependency`/`deferred`/`error` task makes it False and the incomplete
    surface names the offending task(s);
  - absent `goal.json` → flat mode, no goal-level line emitted (byte-for-byte unchanged).

## Risks
- The goal-complete determination must read the SAME per-task result map the scheduler
  already produces — don't re-derive task status from disk (avoid drift). 
- The `max_tasks` check must run inside the existing fail-closed validation path so an
  abort seeds no state (reuse Slice A's abort semantics; don't add a second, weaker
  path).
- Boolean-is-an-int trap in Python (`isinstance(True, int)` is True) — validate
  `max_tasks` rejects `bool` explicitly.
