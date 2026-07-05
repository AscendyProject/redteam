# Goal: close #92's remaining proposals — round-staged reviewer (P3) + hard ceilings (P5)

## Intent

Issue #92 ("Reduce reviewer token cost: gate model review behind deterministic
checks") shipped Proposal 1 (deterministic pre-gate) and Proposal 2 (narrowed
reviewer context, PR #119). Close the two remaining proposals:

- **P3 — stage the reviewer model by round.** Extend the review loop so the
  first-pass scan can run on a cheaper reviewer model, promoting to the
  configured frontier reviewer only when findings persist across rounds
  (escalation), instead of paying frontier price on every round.
- **P5 — hard ceilings on the review loop.** Add configurable hard ceilings —
  max review rounds and max wall-clock per task — on top of the existing
  retry/rescue ladder, so a rare reviewer↔worker ping-pong has a bounded tail
  cost. Prompt caching of the fixed reviewer prompt portion: implement only if
  the CLI adapters actually expose a caching control; otherwise document why it
  is not implementable at the adapter layer and leave it out.

## Hard constraints

- **Approval authority never downgrades.** A cheap first-pass reviewer may
  reject early (emit CHANGES_REQUESTED) but must NEVER be able to APPROVE a
  round on its own — APPROVED must only ever come from the configured frontier
  reviewer. This mirrors #92's pitfall rule: cost optimizations may only
  withhold or defer expensive review, never replace its judgment.
- **Default behavior unchanged.** Staging and ceilings must be opt-in via
  config; with no new config keys set, the pipeline behaves exactly as today
  (backward compatible, regression-tested).
- Engine stays project-agnostic and zero-runtime-deps (stdlib only).
- The adapter trust model, verification allowlist, snapshot/fail-closed logic,
  and the cross-provider pairing guard are security boundaries — do not loosen
  them; changes near them go through plan_review scrutiny.
- Config loader stays fail-loud on unknown keys/bad types (extend it properly
  for any new keys).

## Non-goals

- P4 (offloading checklist items to static-analysis tools) — separate work.
- The native-diff adapter and #120's context-narrowing coupling — do not
  entangle; leave #120 as is.
- No changes to the worker (implementer) side of the loop.

## Notes for decomposition

P3 and P5 both touch the review-invocation path in the orchestrator, so if they
are split into two tasks the later one should stack on the earlier one to avoid
conflicting edits.
