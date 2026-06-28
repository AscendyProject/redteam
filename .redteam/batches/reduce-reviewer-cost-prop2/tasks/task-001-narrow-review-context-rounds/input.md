# Narrow round-over-round reviewer context for carried-over findings (#92 Proposal 2)

## Goal
Cut the **per-round input-token cost of `review_code`** in agent-pair mode on
*subsequent* rounds (round 2+), WITHOUT weakening the adversarial guarantee.
Today every `review_code` round rebuilds the SAME full context — the entire
accumulated `git diff <base>...HEAD` plus the full security checklist — even when
the implementer changed only a few lines to address the prior round's findings
(`review_code.py:24-38`, `_code_review_prompt`). The reviewer is also a *fresh*
reviewer each round, so it re-derives everything from scratch every time. As
`CHANGES_REQUESTED → implement → review_code` loops, this is the dominant
reviewer-token driver (#92 background).

After the fix, on a re-review round the reviewer is handed (a) what **changed
since the last review** for a full adversarial pass, plus (b) the carried-over
open findings to confirm resolution — instead of re-embedding the full
unchanged accumulated diff. The first review round and any uncertain case keep
today's full-diff behavior exactly.

## What to build
In `review_code.py` (agent-pair headless path, `run` when `mode == "agent-pair"`):
- Record the reviewed revision: at each `review_code` invocation, capture the
  current `HEAD` SHA into state (e.g. `state["last_reviewed_rev"]`) AFTER a
  review completes, so the next round can diff against it. (Confirm the exact
  field + where it is written in plan_review; it must survive resume and not
  collide with the `#91` pinned `base_branch` or the `verification.last_diff_sha256`
  content hash, which is a different thing.)
- On a round where a prior reviewed revision exists AND open `review_items`
  exist, build a **narrowed prompt**: the incremental diff
  `git diff <last_reviewed_rev>...HEAD` (full adversarial pass on the delta) +
  the carried-over open findings (rendered from `review_items`) + the checklist.
  Otherwise build today's full-diff prompt.
- Keep the reviewed range derived from the **pinned** `base_branch`
  (`pinned_base_branch`, #91) for the full-review path and for the PR; the
  incremental ref is an additional narrowing, never a replacement for the pinned
  base.

The precise shape of the narrowed prompt and exactly which state field carries
the prior revision are **design decisions for plan_review** — do not assume; the
brief fixes the goal and the four invariants below, not the wording.

## The hard invariant (the crux — confirm the mechanism in plan_review)
This touches the **adversarial-review fidelity boundary**. A naive "just ask the
reviewer whether the prior findings are resolved" is WRONG: it would blind the
reviewer to *new* problems the implementer's fix introduces. The narrowing is
allowed to drop only **re-derivation of context already reviewed**, never
visibility into new changes.

Non-negotiable properties any design must hold:
1. **New changes are always fully reviewed.** The reviewer must receive a full
   adversarial view of everything that changed since the previously-reviewed
   revision (the round-over-round incremental diff). A bug introduced by the fix,
   anywhere in that delta, must still be catchable → `CHANGES_REQUESTED`.
2. **Carried-over findings are still adjudicated.** The open `review_items`
   from prior rounds (the harness already tracks these — `id`, `severity`,
   `status`, `carry_over_count`; see `orchestrator._sync_review_items` /
   `_has_open_blocker_at_or_above`) are passed to the reviewer to confirm each is
   resolved or still open. The existing carry-over / escalation accounting
   (blocker-carried-over-3×, etc.) must keep working unchanged.
3. **Fail-safe to full review on any uncertainty.** First review round (no prior
   reviewed revision recorded), a missing/unparseable prior ref, an empty or
   failed incremental-diff computation, or any git probe error → fall back to
   today's FULL `<base>...HEAD` diff review. Never silently narrow when the
   incremental view can't be computed reliably (fail toward MORE review, not
   less).
4. **Output contract unchanged.** The reviewer still emits `IR-NNN` findings and
   a final `REVIEW_DECISION:` line exactly as today; `review_with_fallback`,
   `_sync_review_items`, and the decision parsing are untouched.

## Constraints
- **Adversarial-review fidelity boundary — plan_review FIRST before any code.**
- Engine stays project-agnostic and **stdlib-only (zero runtime deps)**.
- Preserve the `review_with_fallback` reviewer-adapter contract, the manual /
  fallback ladder (#37), the `parse_status` fail-closed branch, and the
  `REVIEW_DECISION` parsing — all unchanged.
- Preserve `pinned_base_branch` (#91) usage; the PR base and the full-review
  range stay pinned-base-derived.
- No new state that breaks resume; any new field must be set-and-read safely on a
  legacy state that lacks it (treat absence as "fall back to full review").

## Out of scope
- **#92 Proposal 1** (deterministic verify pre-gate before `review_code`) —
  already satisfied by today's architecture (`implement` re-runs verify and only
  hands an `approved`/verify-passing diff to `review_code`; failing rounds loop
  back to `implement` without invoking the reviewer). Verified on main; see the
  #92 issue comment. Do NOT re-implement it.
- **#92 Proposals 3 / 4 / 5** (round-staged model tiering; SAST/semgrep offload —
  which conflicts with zero-deps; prompt caching + hard ceilings) — separate
  follow-ups, not this task.
- The non-agent-pair / TDD sub-agent reviewer path (`review_code.py` tail, the
  `impl_diff.patch`-based fresh reviewer) — only mirror the narrowing there if it
  is trivial and risk-free; otherwise leave it on the full-diff path and note it.
- `plan_review` context narrowing — this task is `review_code` only.

## Affected files
- `.redteam/workflows/phase_runners/review_code.py` — `_code_review_prompt` /
  `run` (agent-pair branch): prompt construction + recording the reviewed
  revision. Re-locate by symbol, not line number.
- Possibly `.redteam/workflows/phase_runners/_base.py` — only if a shared
  incremental-diff helper is the clean home (mirror `compute_repo_diff` /
  `pinned_base_branch` style). Confirm in plan_review.
- New/extended test under `.redteam/tests/` (e.g.
  `test_review_code_narrow_context.py`).

## Verification
- `bash .redteam/scripts/verify.sh` (ruff check + ruff format --check + full
  pytest) stays green; no existing test regresses.
- New deterministic tests (monkeypatch the reviewer adapter + git probes; do NOT
  call a real model or do real git network I/O):
  1. **First round → full diff.** No prior reviewed revision in state → the
     prompt/target is the full `<base>...HEAD` review (today's behavior); a prior
     reviewed-revision field is recorded after the round.
  2. **Subsequent round → narrowed.** With a recorded prior revision and an open
     `review_item`, the reviewer is invoked with the incremental
     `<prior>...HEAD` delta + the carried-over finding, NOT the full accumulated
     diff. Assert the carried-over finding is present in the reviewer input and
     the full unchanged range is not re-embedded.
  3. **New issue in the delta is still caught.** A finding the reviewer raises on
     the incremental delta still yields `status="changes_requested"` and a new
     `IR-NNN` flows into `review_items` (invariant 1).
  4. **Fail-safe.** A failed/empty incremental-diff computation (or missing prior
     ref / not-an-ancestor) falls back to the full-diff review rather than
     narrowing — assert the full range is used (invariant 3).
  5. **Contract intact.** `REVIEW_DECISION` parsing, `parse_status` fail-closed,
     and `_sync_review_items` carry-over accounting behave identically on the
     narrowed path.

## Risks
- **Adversarial blind spot (highest):** if the narrowing drops any part of the
  round's actual changes, a fix-introduced bug escapes review. Invariant 1 + test
  3 are the guard; plan_review must confirm the incremental diff truly covers the
  full delta since the last reviewed revision (including new files anywhere, not
  just under source/test roots — mirror the #82 "complete faithful view" point).
- **Stale prior ref after rebase/amend:** if `last_reviewed_rev` is not an
  ancestor of `HEAD` (history rewritten mid-task), `git diff <prior>...HEAD`
  could be misleading → fail-safe to full review (invariant 3). Cover the
  not-an-ancestor case.
- **Resume / legacy state:** a task mid-flight from before this change has no
  prior-ref field — must transparently take the full-review path, not error.
- Exact line numbers in this brief are from current `main` and may shift; locate
  by symbol.
- Dogfooded through the same review path it changes; a mid-flight interrupt on the
  new test file could touch the (now-fixed) #112 hazard — operational note only.
