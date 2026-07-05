## What
Add opt-in per-task hard ceilings — a maximum number of `review_code` rounds
and a maximum cumulative wall-clock time spent in `review_code` — on top of
the existing retry / rescue-entry ladder, so a rare reviewer↔worker ping-pong
has a bounded tail cost. When a ceiling is hit the task must terminate
deterministically at the same fail-closed / deferred outcome the existing
rescue-entry ceiling uses — never as a silent approval. Default behavior
(no `[models.review_ceilings]` subtable in config) reproduces today's pipeline
with no new state fields written, no new counters mutated, and no new
runtime work.

## Why
Issue #92 (P5) asks for hard ceilings on the review loop so a rare
reviewer↔worker ping-pong has a bounded tail cost — a max review-round count
and a max cumulative wall-clock time per task, both opt-in, and both routed
through the same fail-closed / deferred exit the rescue-entry ceiling
already uses (no silent approvals under time pressure). It also asked us to
investigate CLI-adapter prompt caching and either implement it or record why
it isn't implementable at the CLI seam. This PR is the second half of #92
(stacked on task-001-p3-staged-reviewer / P3) and lands both ceiling knobs
plus the "not implementable at the CLI adapter layer" decision doc.

## Done-when
- [ ] `.redteam/workflows/config.py` gains a frozen dataclass
      `ReviewCeilingsConfig(max_review_rounds: int | None,
      max_wall_clock_sec: int | None)` (both fields default `None`), and
      `ModelsConfig` gains an optional field `review_ceilings:
      ReviewCeilingsConfig | None = None`.
- [ ] `config.load_config` fails loud with `ValueError` on every one of
      the following inputs under `[models.review_ceilings]`: (a) unknown
      key inside the subtable; (b) subtable present but both keys absent;
      (c) `max_review_rounds` that is a `bool`, non-`int`, `0`, or
      negative; (d) `max_wall_clock_sec` that is a `bool`, non-`int`,
      `0`, or negative; (e) either key of the wrong TOML type. The error
      message shape matches the P3 `_parse_review_stages` style
      (`Unknown models.review_ceilings config key(s): [...]. Known keys:
      [...]`).
- [ ] `_parse_tiers` is UNCHANGED. `TierProfile.models` stays
      `dict[str, str]`. A `[tiers.N].models.review_ceilings` sub-table
      continues to be rejected by today's fail-loud path (unknown-role OR
      non-str value). `_KNOWN_ROLES` additionally excludes
      `"review_ceilings"` (P3 already excludes `"review_stages"`).
      Regression test locks both rejection paths.
- [ ] With `[models.review_ceilings]` absent from `.redteam/config.toml`,
      `load_config(repo_root)` returns a `RedteamConfig` whose
      `models.review_ceilings is None`, and every other `ModelsConfig`
      field is byte-identical to today's shipped defaults.
      Regression-locked.
- [ ] `.redteam/config.toml` in THIS repo is UNCHANGED — no
      `[models.review_ceilings]` subtable is added. Regression-locked by
      a test that loads this repo's own config and asserts
      `cfg.models.review_ceilings is None`.
- [ ] `.redteam/templates/state.template.json` is UNCHANGED — no new
      counter fields are added to the template. The two new counters
      (`review_code_round_count`, `review_code_wall_clock_sec`) appear in
      state.json lazily on the first opted-in `review_code.run` and are
      treated as `0` / `0.0` when absent. Regression-locked by a test
      that reads the template file bytes and asserts equality to the
      pre-change bytes (or by a field-set assertion that excludes the
      two new keys).
- [ ] `.redteam/workflows/phase_runners/_base.py::PhaseResult` gains an
      optional field `ceiling_hit: NotRequired[str]` — set ONLY by the
      runner (mirrors `staging_audit` / `fallback_audit`), never parsed
      from reviewer text.
- [ ] `phase_runners/review_code.py::run` (agent-pair branch ONLY) reads
      `review_ceilings = load_config(repo_root()).models.review_ceilings`
      at the top of the agent-pair branch. When `review_ceilings is
      None`, the runner is byte-identical to today: no counter increment,
      no `time.monotonic()` reads, no ceiling checks, no `ceiling_hit`
      field ever set, no growth of state.json. Regression-locked.
- [ ] When `review_ceilings is not None` AND
      `review_ceilings.max_review_rounds is not None`,
      `state["review_code_round_count"]` is incremented at the TOP of the
      agent-pair branch, BEFORE the pre-dispatch check. When
      `review_ceilings is None` OR `max_review_rounds is None`, the
      counter is not written and not read.
- [ ] When `review_ceilings is not None` AND
      `review_ceilings.max_wall_clock_sec is not None`, the runner wraps
      the existing dispatch (Cases B / C / D from task-001) with a
      `time.monotonic()` timer per D3 and runs the post-dispatch
      wall-clock check. When `review_ceilings is None` OR
      `max_wall_clock_sec is None`, no `time.monotonic()` reads happen
      and `state["review_code_wall_clock_sec"]` is neither written nor
      read.
- [ ] On any ceiling crossing the runner returns
      `PhaseResult(status="error", ceiling_hit=<"max_review_rounds" |
      "max_wall_clock_sec">, ...)` per D5. Legacy state without the two
      new counters is treated as `0` / `0.0`.
- [ ] The round-ceiling check is triggered on invocation
      `max_review_rounds + 1`, NOT on `max_review_rounds`. Invocations
      `1..max_review_rounds` run the reviewer normally.
- [ ] The wall-clock ceiling triggers whenever accrued time crosses the
      configured value, EITHER before dispatch (accrued >= max before
      the call) OR after dispatch (accrual pushes total >= max). On the
      after-dispatch crossing the reviewer's raw is still persisted to
      `code_review.md` (and `code_review.first_pass.md` on a promoted
      round) for the audit trail, but the `PhaseResult` is upgraded to
      `ceiling_hit="max_wall_clock_sec"` — no approval is finalized.
- [ ] There is NO code path by which either ceiling crossing produces
      `PhaseResult.status == "approved"`. Regression test locks this in
      as a hard invariant against future refactors.
- [ ] `orchestrator.process_task` gains a ceiling-handling pre-check
      that runs BEFORE `manual_required`, BEFORE the `fallback_audit` /
      `staging_audit` audit-append site, and BEFORE the retries / rescue
      routing. On `result.get("ceiling_hit")` it appends a
      `deferred_requirements` entry (with `"reason"`, `"round_count"`,
      `"wall_clock_sec"`, `"feedback"` per D6), records failure, sets
      `state["next_phase"] = "deferred"`, saves, and returns
      `"deferred"`.
- [ ] The `review_audit` list receives an entry
      `{"phase": "review_code", "reason": <ceiling_hit value>}` on a
      ceiling-terminated round (mirrors the P3 `staging_audit` wiring
      at `.redteam/workflows/orchestrator.py:1474-1479`).
- [ ] `state["review_code_round_count"]` increments on EVERY
      `review_code.run` invocation in agent-pair mode (Cases A / B / C /
      D — even Case A, so a manual-heavy task can still hit the round
      ceiling) **when the round-ceiling is configured**.
      `state["review_code_wall_clock_sec"]` is accrued ONLY on Cases
      B / C / D **when the wall-clock ceiling is configured**. Neither
      counter is reset on convergence — the budget is per-task,
      per-lifetime.
- [ ] Ceilings and P3 staging (`[models.review_stages]`) are independent:
      the loader accepts `[models.review_ceilings]` alone,
      `[models.review_stages]` alone, both, or neither. A ceiling
      triggered on a first-pass (cheap) round terminates the same way as
      one triggered on a frontier round — the "no approval under ceiling"
      invariant holds on both staging paths (regression-locked).
- [ ] `adapters/claude.py` and `adapters/codex.py` are UNCHANGED by this
      task (the D7 investigation says CLI seam has no caching control).
      Verified by a `git diff` scope assertion in the affected-files
      list.
- [ ] `(new) docs/decisions/2026-07-05-reviewer-prompt-caching.md` exists
      and records the D7 determination: CLI-seam caching investigation,
      the "not implementable at the CLI adapter layer" conclusion, and a
      guard rail that any future revisit go through `plan_review` on the
      adapter transport. No code lands from this bullet.
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff check + ruff
      format --check + full pytest under `.redteam/tests/`) — including
      the new test file AND every existing test.

## Verification
- Tests: test_ceilings_max_rounds_only, test_ceilings_max_wall_clock_only, test_ceilings_both_set, test_ceilings_unknown_key_rejected, test_ceilings_empty_subtable_rejected, test_ceilings_max_rounds_bool_rejected, test_ceilings_max_rounds_zero_rejected, test_ceilings_max_rounds_negative_rejected, test_ceilings_max_rounds_string_rejected, test_ceilings_max_wall_clock_bool_rejected, test_ceilings_max_wall_clock_zero_rejected, test_ceilings_max_wall_clock_negative_rejected, test_ceilings_max_wall_clock_string_rejected, test_tier_level_ceilings_dict_rejected, test_tier_level_ceilings_string_rejected, test_default_config_review_ceilings_is_none, test_no_ceilings_no_state_growth, test_no_ceilings_no_ceiling_hit_field, test_no_ceilings_time_monotonic_not_called, test_state_template_unchanged, test_round_counter_increments_each_invocation, test_round_ceiling_not_triggered_within_budget, test_round_ceiling_triggers_on_max_plus_one, test_round_ceiling_no_wall_clock_written, test_round_counter_not_written_when_only_wall_clock_configured, test_wall_clock_pre_dispatch_skip, test_wall_clock_pre_dispatch_skip_equal_exactly, test_wall_clock_post_dispatch_upgrade_approved, test_wall_clock_post_dispatch_upgrade_raw_persisted, test_wall_clock_post_dispatch_upgrade_no_approval, test_wall_clock_post_dispatch_upgrade_manual_required, test_wall_clock_post_dispatch_manual_required_below_ceiling_unchanged, test_no_path_sets_ceiling_hit_and_approved, test_ceiling_hit_never_produces_approved_status, test_orchestrator_ceiling_routes_to_deferred, test_orchestrator_ceiling_deferred_requirements_entry, test_orchestrator_ceiling_wall_clock_entry_shape, test_orchestrator_ceiling_review_audit_entry, test_orchestrator_ceiling_not_routed_to_rescue, test_wall_clock_accrual_cumulative, test_wall_clock_legacy_state_starts_at_zero, test_case_a_no_wall_clock_accrual, test_case_a_round_counter_increments, test_case_a_hits_round_ceiling, test_ceilings_and_staging_coexist, test_ceiling_on_first_pass_round_counts, test_ceiling_on_promoted_round_terminates_non_approved, test_persistence_round_counter_survives_resume, test_persistence_wall_clock_survives_resume, test_convergence_does_not_reset_round_counter, test_convergence_does_not_reset_wall_clock, test_adapter_claude_no_cache_control, test_adapter_codex_no_cache_control, test_decision_doc_exists, test_dogfood_config_review_ceilings_is_none
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff summary: fail-loud `_parse_review_ceilings` in `config.py`, new `ReviewCeilingsConfig` dataclass and `ModelsConfig.review_ceilings` field, `_KNOWN_ROLES` excludes `"review_ceilings"`, `PhaseResult.ceiling_hit` added, `review_code.run` agent-pair branch gains the D4 enforcement (round increment, pre-dispatch checks, wall-clock timer wrap for Cases B/C/D, post-dispatch upgrade), orchestrator ceiling pre-check defers via `deferred_requirements` + `review_audit`, plus 55 new tests in `test_review_code_hard_ceilings.py` and the D7 decision doc.
- Key Done-when met: opt-in fail-loud config; default parity (no counters, no `time.monotonic()`, no state growth when subtable absent); round-ceiling triggers on max+1; wall-clock ceiling triggers pre- OR post-dispatch and discards a just-crossed APPROVED; `ceiling_hit` + `status="approved"` cannot coexist; orchestrator pre-check routes to `deferred` before manual/rescue/retries; `review_audit` mirror entry; adapters and state template untouched.
- No open HITs: code_review IR-001 (manual-required wall-clock bypass) and IR-002 (unrelated sibling state.json in delta) are both `status:resolved`; final decision `REVIEW_DECISION: APPROVED`.
- Plan review context: PR-001 (missing parseable `## Verification` block) noted in the round-2 plan review as CHANGES_REQUESTED, but the implementer/orchestrator carried the same review-item ID as `status:resolved` in the final review-items list; the outcome ships with an inline verify command and code review approved on that basis. Flagging for the human gate.
- Verification evidence: `state.json` records `verification.last_exit_code: 0` and `verification.log` reports `682 passed` with `verify.sh OK`.

## Generated by
redteam / batch reviewer-cost-p3p5 / task task-002-p5-hard-ceilings
