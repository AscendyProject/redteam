# Task 001 — P3: Round-staged reviewer model

## Parent context

This is the first of two tasks that close the remaining proposals of issue #92
("Reduce reviewer token cost: gate model review behind deterministic checks").
Proposal 1 (deterministic pre-gate) and Proposal 2 (narrowed reviewer context,
PR #119) already shipped. This task implements **Proposal 3 (P3) — stage the
reviewer model by round**. A follow-up task (P5, task-002) will add hard
ceilings on top of what you build here; keep your edits localized so P5 can
stack cleanly on this branch without conflicting edits to the review-invocation
path.

## What to build

Extend the review loop so that the *first-pass* scan can run on a cheaper
reviewer model, promoting to the configured frontier reviewer only when
findings persist across rounds ("escalation"). Concretely:

- Add opt-in config keys under `[models]` (or a nested `[models.review_stages]`
  table — decide during planning) that let the operator declare a cheap
  first-pass reviewer and an escalation threshold (e.g. "first N rounds run on
  the cheap model, then escalate"). The exact key shape is a design decision to
  argue in `plan_review` — but the loader must fail loud on unknown keys/bad
  types, matching the existing config discipline in
  `.redteam/workflows/config.py`.
- Route the reviewer role at review-round dispatch time based on the current
  round number and the staging config, so the review runner
  (`.redteam/workflows/phase_runners/review_code.py`) still asks the adapter
  registry for whichever reviewer the staging policy resolves to. Do not
  hardcode adapters in the runner.
- Preserve the **cross-provider pairing guard**: whichever reviewer is selected
  for a given round must resolve to a different provider than the worker.
  Same-provider review remains fail-closed self-review.

## Approval-authority invariant (HARD)

A cheap first-pass reviewer may reject early (emit `CHANGES_REQUESTED`) but
must **never** be able to `APPROVE` a round on its own. `APPROVED` must only
ever come from the configured frontier reviewer.

Enforce this at the seam that consumes the reviewer's verdict: if the current
round is running the cheap first-pass reviewer and the verdict parses as
`APPROVED`, the engine must either (a) coerce it into a deferred/escalated
outcome that forces the next round onto the frontier reviewer, or (b)
fail-closed. Argue the concrete strategy in `plan_review`, but the ONLY
acceptable end state is: no cheap-reviewer round ever finalizes as approved.

Write regression tests that would fail if a future refactor let a cheap
reviewer approve a round.

## Files this task most likely affects

- `.redteam/workflows/config.py` — new opt-in config keys, fail-loud
  validation, defaults that reproduce today's behavior.
- `.redteam/workflows/phase_runners/review_code.py` — round → reviewer routing
  seam and the approval-authority guard.
- `.redteam/workflows/adapters/__init__.py` / `_protocol.py` — only if a new
  resolver seam is needed; prefer to route via existing resolvers.
- `.redteam/tests/` — new tests covering: staging enabled routes cheap → frontier
  across rounds; cheap reviewer's `APPROVED` never finalizes; cross-provider
  guard still refuses self-review even under staging; default config unchanged.
- `.redteam/config.toml` — do NOT enable staging in this repo's own config; the
  default must remain today's behavior.

## Constraints inherited from the parent goal

- **Default behavior unchanged.** With no new config keys set, the pipeline
  behaves exactly as today. Add explicit regression tests that assert the
  default path is untouched.
- **Engine stays project-agnostic and zero-runtime-deps** (stdlib only).
- Adapter trust model, verification allowlist, snapshot / fail-closed logic,
  and cross-provider pairing guard are **security boundaries** — do not
  loosen; `plan_review` will scrutinize any change near them.
- Config loader stays **fail-loud** on unknown keys / bad types — extend it
  properly for any new keys; do not silently accept typos.
- **No changes to the worker (implementer) side of the loop.**

## Non-goals for this task

- Do not add hard ceilings on rounds / wall-clock — that is task-002 (P5).
- Do not touch P4 (static-analysis offloading) or the native-diff adapter /
  #120 context-narrowing coupling.
- Do not change the worker adapters or the implement/rescue phases.
- **Tier-level staging is out of scope for v1** (operator decision at the
  ask_user gate): staging is configured globally only; do not change the
  `TierProfile.models` type contract (`dict[str, str]`) or the tier parser.
