## What
Let an operator declare a cheaper first-pass reviewer that handles the early
`review_code` rounds and escalates to the configured frontier reviewer on later
rounds, while (a) keeping the default behavior byte-identical when the new
config is absent, (b) preserving the cross-provider adversarial-pairing guard
against BOTH the first-pass reviewer AND the frontier reviewer, and (c)
guaranteeing that no round dispatched to the first-pass reviewer can finalize
as `PhaseResult.status == "approved"`.

## Why
This is the first of two tasks closing the remaining proposals of issue #92
("Reduce reviewer token cost: gate model review behind deterministic checks").
Proposal 1 (deterministic pre-gate) and Proposal 2 (narrowed reviewer context,
PR #119) already shipped; this task implements Proposal 3 — stage the reviewer
model by round so early rounds can run on a cheaper reviewer and only persistent
findings escalate to the frontier reviewer. A follow-up task (P5, task-002) will
stack hard ceilings on top of this seam without another restructure.

## Done-when
- [ ] `.redteam/workflows/config.py` gains a frozen dataclass
      `ReviewStagesConfig(first_pass_reviewer: str, escalate_after: int)`
      and `ModelsConfig` gains an optional field
      `review_stages: ReviewStagesConfig | None = None`. Unknown keys inside
      `[models.review_stages]` raise `ValueError` with the same
      `Unknown ... config key(s)` shape as `_build`.
- [ ] `config.load_config` fails loud with `ValueError` on: (a)
      `first_pass_reviewer` not a key of `adapters._REVIEWER_ADAPTERS`;
      (b) `first_pass_reviewer` equal to `"manual"` / `"human"`;
      (c) `escalate_after` that is not an `int >= 1` (Python `bool` values
      rejected); (d) either required key missing when the subtable is
      present; (e) either key of the wrong TOML type.
- [ ] `_parse_tiers` is UNCHANGED. `TierProfile.models` stays
      `dict[str, str]`. A `[tiers.N].models.review_stages` sub-table continues
      to be rejected by today's fail-loud paths (unknown-role OR non-str
      value). A regression test asserts both rejection paths.
- [ ] With `[models.review_stages]` absent from `.redteam/config.toml`,
      `load_config(repo_root)` returns a `RedteamConfig` whose
      `models.review_stages is None`, and every other `ModelsConfig` field
      is byte-identical to today's shipped defaults. Regression-locked.
- [ ] `.redteam/workflows/adapters/__init__.py` gains exactly two new
      public names, re-exported via `__all__`:
      (i) `get_reviewer_adapter_by_provider(name: str) -> ReviewerAdapter | None`;
      (ii) `review_with_fallback_for_provider(state, *, role, prompt, cwd,
      target, primary_provider: str) -> ReviewResult`.
      Both are implemented by delegating to a single private
      `_review_with_fallback_impl` shared with `review_with_fallback`.
- [ ] `review_with_fallback`'s public signature and observable semantics are
      UNCHANGED. Every existing caller (`plan_review.run`, and Case B / Case C
      of `review_code.run`) continues to invoke it exactly as today.
- [ ] `phase_runners/review_code.py::run` (agent-pair branch ONLY)
      implements the D5 dispatch contract with a single private helper
      `_resolve_round_stage(state) -> Literal["manual", "unstaged", "frontier", "first_pass"]`
      (or an equivalent tagged enum). The dispatch tree matches Cases A / B /
      C / D exactly. The runner contains NO hardcoded provider literal
      (`"codex"` / `"claude"`); every provider identifier flows in from
      `state["models"]` / `load_config()` output.
- [ ] With `load_config(repo_root()).models.review_stages is None`,
      `review_code.run` is byte-identical to today: same prompt build
      (full or narrowed), one `review_with_fallback` call, same
      `PhaseResult` shape, no `code_review.first_pass.md` file. Regression-locked.
- [ ] With staging ON and the round dispatched to the first-pass reviewer
      AND the parsed verdict is `APPROVED` with `parse_status == "ok"`,
      `review_code.run` MUST invoke `review_with_fallback` a second time
      within the same `run()` call and return THAT result. No code path
      exists by which a first-pass `ReviewResult(decision="APPROVED")` maps
      to `PhaseResult(status="approved")`.
- [ ] `code_review.md` after a promoted round contains the FRONTIER
      reviewer's raw. The first-pass reviewer's raw is written to
      `task_dir / "code_review.first_pass.md"` BEFORE the frontier
      invocation, and rotates through `code_review.first_pass.round1.md`,
      `code_review.first_pass.round2.md`, … on subsequent rounds via
      `_archive_review_round`.
- [ ] `orchestrator._clear_manual_phase_artifacts` (`.redteam/workflows/orchestrator.py:524-533`)
      is extended so that when `phase == "review_code"` it makes TWO
      `_archive_review_round` calls: the existing one on `code_review.md`
      AND an explicit second call on `code_review.first_pass.md`. The
      `plan_review` and `rescue` mappings are UNCHANGED (single file each).
      Resolves the prior "Uncertain" comment.
- [ ] `.redteam/workflows/phase_runners/_base.py::PhaseResult` gains an
      optional field `staging_audit: NotRequired[str]` — structured
      provenance set by the runner (never parsed from reviewer text),
      mirroring `fallback_audit`. On a promoted round the runner populates
      `staging_audit` with a short string naming the round number and the
      promoted-from provider.
- [ ] `orchestrator._adversarial_pairing_error` (`.redteam/workflows/orchestrator.py:292-330`)
      gains the D8 first-pass check. The check is additive, only fires when
      staging IS configured, and only when `review_code` is in the resolved
      order in agent-pair mode. Error message names which side collapsed.
- [ ] `orchestrator.py`'s existing `review_audit` wiring
      (`.redteam/workflows/orchestrator.py:1448-1450`) additionally appends a
      `{"phase": "review_code", "reason": <staging_audit>}` entry when
      `PhaseResult.staging_audit` is present, mirroring the existing
      `fallback_audit` append. Trusted only when set by the runner
      (structured field), never from in-band reviewer text.
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff check + ruff format
      --check + full pytest under `.redteam/tests/`) — including the new
      test file AND every existing test.
- [ ] `.redteam/config.toml` in THIS repo is UNCHANGED — no
      `[models.review_stages]` subtable is added. Regression-locked by a
      test that loads this repo's own config and asserts
      `cfg.models.review_stages is None`.

## Verification
- Tests: `test_review_stages_happy_path`, `test_review_stages_with_claude_first_pass`, `test_review_stages_unknown_key_rejected`, `test_review_stages_unknown_provider_rejected`, `test_review_stages_manual_first_pass_rejected`, `test_review_stages_human_first_pass_rejected`, `test_review_stages_escalate_after_zero_rejected`, `test_review_stages_escalate_after_negative_rejected`, `test_review_stages_escalate_after_bool_rejected`, `test_review_stages_missing_first_pass_reviewer_rejected`, `test_review_stages_missing_escalate_after_rejected`, `test_tier_level_review_stages_dict_rejected`, `test_tier_level_review_stages_string_rejected`, `test_default_config_review_stages_is_none`, `test_no_staging_uses_review_with_fallback_once`, `test_no_staging_no_first_pass_artifact`, `test_no_staging_prompt_byte_identical`, `test_case_a_human_reviewer_bypasses_staging`, `test_case_a_prior_manual_required_bypasses_staging`, `test_case_c_frontier_round_uses_review_with_fallback`, `test_case_c_frontier_round_no_first_pass_artifact`, `test_case_d_routing_progression`, `test_case_d_first_pass_provider_key_passed`, `test_case_d_cheap_approved_triggers_frontier_call`, `test_case_d_cheap_approved_staging_audit_set`, `test_case_d_cheap_approved_staging_audit_names_round`, `test_case_d_frontier_rejects_after_promotion`, `test_case_d_changes_requested_not_promoted`, `test_case_d_rescue_required_not_promoted`, `test_case_d_ask_user_not_promoted`, `test_case_d_unparseable_not_promoted`, `test_case_d_manual_required_not_promoted`, `test_approval_authority_invariant`, `test_for_provider_valid_primary_returned_unchanged`, `test_for_provider_infra_failure_falls_back_to_manual`, `test_for_provider_same_provider_fallback_blocked`, `test_for_provider_non_read_only_fallback_blocked`, `test_for_provider_unknown_fallback_blocked`, `test_unknown_first_pass_provider_returns_manual_required`, `test_get_reviewer_adapter_by_provider_returns_none_for_unknown`, `test_get_reviewer_adapter_by_provider_returns_adapter_for_codex`, `test_d8_first_pass_same_provider_fails_closed`, `test_d8_codex_worker_codex_first_pass_fails_closed`, `test_d8_cross_provider_staging_passes_guard`, `test_d8_guard_does_not_fire_when_staging_off`, `test_review_audit_receives_staging_audit`, `test_first_pass_artifact_rotation_two_promoted_rounds`, `test_first_pass_rotation_noop_when_absent`, `test_plan_review_not_staged`, `test_dogfood_config_review_stages_is_none` (new file `.redteam/tests/test_review_code_staged_reviewer.py`, plus the full existing suite — 627 passed per `verification.log`).
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Adversarial reviewer (`codex`) reached **`REVIEW_DECISION: APPROVED`** after plan_review rounds 1–3 resolved all seven blockers (PR-001…PR-007) — see `code_review.md`.
- Config parsing adds global-only `[models.review_stages]` with fail-loud validation and keeps tier-level staging rejected via `_KNOWN_ROLES` exclusion; `TierProfile.models` still `dict[str, str]`.
- Runner implements the four dispatch cases (A/B/C/D) and preserves the approval-authority invariant: a first-pass `APPROVED` writes `code_review.first_pass.md` and immediately promotes to `review_with_fallback`; only that frontier result can map to `PhaseResult(status="approved")`.
- Adapter layer keeps a single shared fallback ladder (`_review_with_fallback_impl`) so `review_with_fallback` and the new `review_with_fallback_for_provider` cannot drift; only two new public names added.
- Cross-provider adversarial-pairing guard extended (D8) to cover the first-pass reviewer as well as the frontier reviewer, additive-only when staging is configured.
- New tests are discriminating against pre-change code (import new types, assert first-pass routing/promotion, artifact rotation, D8 guard); no HITs, no MEDs.

## Generated by
redteam / batch reviewer-cost-p3p5 / task task-001-p3-staged-reviewer
