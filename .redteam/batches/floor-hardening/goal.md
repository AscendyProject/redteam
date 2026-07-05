# Goal: make the fail-closed floors goal-mode-aware and plan-aware (#136 + #137)

## Intent

The first autonomous goal run (batch `reviewer-cost-p3p5`) proved the floors'
threat model right but their scope model wrong: they fail-closed on the
harness's **own** artifacts. Close both filed gaps without weakening what the
floors actually guard (genuine operator WIP must still be refused):

- **#136 — decompose artifacts and the #117↔#124 inconsistency.**
  1. The same batch's top-level decompose artifacts — `goal.md`, `goal.json`,
     `decompose_review.md`, `decompose_blocked.md` at the batch root — are
     harness decision trail and must be exempt in `_floor_outside_scope`
     (#91 Part A) and `_cross_run_trust_root_floor` (#117), same rationale as
     the existing task-dir exemption.
  2. Add `input.md` to the #124 sibling top-level basename allowlist (a
     stacked child's scheduler run always sees its sibling's brief).
  3. Fix the #117 Check-2 inconsistency: `_cross_run_trust_root_floor`'s
     `_is_allowed` must honor the same #124 sibling allowlist as
     `_floor_outside_scope` — today a path the #91 floor correctly tolerates
     gets stored in the set-once baseline and then #117 fail-closes on that
     stored entry in the next process (self-lock), and pruning the entry
     breaks the sweep's operator-WIP exclusion instead (catch-22, recorded in
     the #136 comment thread).
- **#137 — plan-declared Affected files.** The pre-worker out-of-scope floor
  must exempt paths explicitly declared in the current task's
  **review-approved** `outcome.md` Affected files (tolerating the
  `(new) ` prefix convention). They are the task's scope by definition; a
  review backtrack must not self-lock on the worker's own round-1 output.
  Everything the plan did not declare stays guarded exactly as today.

## Hard constraints

- **The floors stay fail-closed.** Every exemption is narrow and enumerated
  (exact batch-root basenames; sibling top-level allowlisted basenames;
  Affected-files paths from the approved outcome.md only). Genuine operator
  WIP outside those sets must still be refused — keep regression tests
  proving each existing refusal still fires.
- These functions are a **security boundary**: no broad prefix exemptions, no
  "trust everything under .redteam/batches/", no weakening of the sweep's
  operator-WIP exclusion. The catch-22 fix must keep tamper-DETECTION intact
  (an adversarial baseline rewrite must still be caught).
- Engine stays project-agnostic, stdlib-only, zero runtime deps.
- Default behavior for non-goal-mode, non-stacked, in-scope-only tasks is
  byte-identical (regression-tested).

## Operator delegation (autonomy clause)

Plan-level scope questions in this run are delegated to the operator agent:
prefer the **narrowest** exemption that unblocks autonomous goal runs, record
every such decision in `ask_user_response.md` (or the final report) instead of
waiting for a human. Security-boundary WEAKENING beyond this goal's enumerated
exemptions is not delegated — that still stops the run.

## Non-goals

- #138 (planner Verification-block bug), #120, #132, #133.
- No redesign of the set-once baseline mechanism or the commit sweep beyond
  the enumerated consistency fix.
- No changes to reviewer/worker adapters or prompts.

## Notes for decomposition

- Two tasks; the second **stacks on the first** (both edit
  `phase_runners/implement.py` and its tests): task 1 = #136 (items 1–3),
  task 2 = #137.
- Each brief MUST tell the planner: `outcome.md` needs a parseable
  `## Verification` section with a fenced ```yaml block containing
  `bash .redteam/scripts/verify.sh` (known #138 pitfall — prose
  "Verification hooks" does not parse).
- Each brief MUST pin Affected files strictly inside `.redteam/workflows/`
  and `.redteam/tests/` — do NOT add files under `docs/` or anywhere else
  outside scope in this run (that is the very self-lock #137 fixes; it is not
  fixed yet while task 1 runs).
