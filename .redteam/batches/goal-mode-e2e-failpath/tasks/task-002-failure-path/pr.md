# test(goal-mode): add failure-path e2e composition assertions (#94)

Extends the goal-mode E2E composition test (`.redteam/tests/test_goal_mode_e2e.py`,
added in #123) with the **failure-path** assertions, reusing its existing
scaffolding (temp-git-repo builder, stub-worker harness, multi-task `goal.json`
helper). Test-only — no engine change.

## What this adds

1. **Deferred / failed parent blocks descendants** — a two-task chain where the
   stub reports the parent as deferred/failed: the dependent is never dispatched
   and ends `blocked_on_dependency`.
2. **`ceilings.max_tasks` mismatch aborts before any seeding** — a manifest whose
   declared `ceilings.max_tasks` disagrees with the task count aborts the batch
   before any task is seeded or any stub worker dispatched.
3. **Moved parent-tip / wrong-base reused branch fails closed** — the
   stacking-pin freeze invariant holds in the composed pipeline: a dependent's
   reused branch pointing at a base that no longer matches the parent tip fails
   closed rather than silently producing a wrong-stacked dependent.

Together with the happy-path coverage from #123, this closes the #94 gap of
having no single test that drives the goal-mode slices (manifest → schedule →
stacking-pin → ceilings → done-criterion) **composed together** through the real
`process_batch`.

## Verification

`bash .redteam/scripts/verify.sh` — ruff + pytest over `.redteam/`, 565 passed.

## Provenance

Driven through the redteam pipeline (plan_outcome → plan_review → implement →
review_code APPROVED). The `create_pr` step was completed by hand because the
pr-author worker hit a session token limit; the branch, the reviewed diff, and
the APPROVED `code_review.md` are the harness's own output.
