# task-001 — Exempt the batch's sibling task decision-trail from the out-of-scope tracked floor (#124)

## Problem (found by the #94 goal-mode e2e dogfood)

In a **stacked goal-mode run** (a dependent task pinned to its parent task's
branch), the dependent task's `implement` phase trips the #91 out-of-scope tracked
floor on the **parent task's `state.json`** and defers. Concretely, in batch
`goal-mode-e2e`, `task-002-failure-path` (pinned to `redteam/task-001-happy-path`)
deferred at `implement` with:

```
refusing to sweep operator tracked WIP into the task commit ...
Out-of-scope tracked paths:
  .redteam/batches/goal-mode-e2e/tasks/task-001-happy-path/state.json
```

### Root cause

1. In a repo that **tracks its batch dir**, each task's `state.json` is committed
   onto its task branch. When `task-002` branches from `task-001`'s branch, the
   **parent's `state.json` is tracked** on the child's base.
2. The orchestrator **legitimately mutates** the parent's `state.json` as the
   parent finalizes (`create_pr` flips `phase -> done`, writes `pr_url`). That
   on-disk update is an **uncommitted tracked change** vs the child's base.
3. `_floor_outside_scope` (`.redteam/workflows/phase_runners/implement.py`) exempts
   only the **current** `task_dir` via `_in_task_dir` (POSIX-prefix). The parent's
   `tasks/task-001-.../state.json` is outside source/test scope AND outside the
   current task_dir → the floor flags it → defer.

The floor is otherwise correct — it did NOT mask anything, it fail-closed. The gap
is purely that the exemption rationale already in the `_floor_outside_scope`
docstring — *"its files (outcome.md, state.json, the *_review.md trail) are the
harness's own decision trail ... a consumer who commits their batch dir onto the
task branch must not be falsely refused over the harness's own artifacts"* —
**applies equally to sibling task dirs in the SAME batch under a stacked run**, but
the current exemption is scoped to a single task_dir.

## Goal

Make a stacked dependent task NOT defer over the parent/sibling task's
harness-owned decision-trail artifacts, **without weakening** the floor against
genuine operator tracked WIP outside scope.

## Done-when

- A stacked dependent task whose only out-of-scope tracked change is a **sibling
  task's harness artifact** (`state.json` / `outcome.md` / `*_review.md` / `pr.md`
  under the same batch's `tasks/<other-id>/`) proceeds (does NOT defer).
- Genuine operator tracked WIP outside scope (a real source-adjacent file, or an
  arbitrary non-artifact file even under a sibling task dir) **still trips** the
  floor — no weakening. This is the security-critical assertion.
- The existing single-task_dir exemption behavior is preserved byte-for-byte for
  flat (non-stacked) runs.
- Verification: `bash .redteam/scripts/verify.sh` (ruff + pytest over `.redteam/`).

## Candidate approach (confirm/adjust in plan_review)

**Approach A — widen the exemption to the batch's task decision-trail surface.**
The current task_dir is `<batch>/tasks/<this-id>/`; its parent is the batch's
`tasks/` root. Exempt harness-owned artifacts under **any** `<batch>/tasks/*/` of
the same batch, not just the current task.

**Security tension to resolve in plan_review (Codex):** the current exemption
trusts the *whole* current task_dir (any path under it). Widening that wholesale to
all sibling task dirs would let a task hide an arbitrary tracked file under a
sibling dir and evade the floor. Prefer the **tighter** form: exempt only
**harness-owned artifact basenames** (`state.json`, `outcome.md`, `pr.md`,
`*_review.md`, and the known decision-trail files) under sibling task dirs —
arbitrary paths under a sibling dir still trip the floor. Decide the exact
allowlist vs. prefix boundary in plan_review.

**Symmetry check (raise in plan_review):** `_commit_worker_diff` and the Layer-2
untracked gate (`_uncommitted_outside_scope_files`) share the same task_dir
exemption rationale. Determine whether they need the same sibling-aware treatment
for consistency, or whether only the tracked floor (`_floor_outside_scope`) is
load-bearing for this failure. The concrete #124 failure is the **tracked floor**;
do not widen the others unless plan_review shows a real gap (surgical-changes rule).

## Constraints

- **Security-boundary adjacent (#91/#112/#117 floor) → `plan_review` first.** Never
  loosen the floor inline. The fix must keep the floor's fail-closed guarantee.
- **Engine stays project-agnostic** — no project/stack fingerprints in
  `.redteam/workflows/`. The batch/tasks layout is a generic harness concept.
- **Stdlib only.** No new dependency.
- Surgical: change only what #124 requires. Mirror the existing `_in_task_dir` /
  `_scope_root` POSIX-prefix idiom; do not refactor the floor.

## Affected files (expected)

- `.redteam/workflows/phase_runners/implement.py` — `_floor_outside_scope` (and,
  only if plan_review confirms, the symmetric exemptions).
- A new/extended test under `.redteam/tests/` asserting both the
  proceeds-on-sibling-artifact case and the still-trips-on-genuine-WIP case.

## Out of scope

- Whether batch `state.json` should be tracked at all (candidate B in #124) — not
  this task.
- The #94 e2e `task-002` re-run — handled separately after this fix merges.
