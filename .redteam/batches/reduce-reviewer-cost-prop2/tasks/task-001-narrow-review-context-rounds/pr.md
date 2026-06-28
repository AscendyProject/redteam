## What
On `review_code` rounds 2+ in agent-pair mode, hand the reviewer the incremental
delta since the previously-reviewed revision plus the carried-over open
`review_items` — instead of re-embedding the full accumulated
`git diff <base>...HEAD` every round — while keeping the adversarial-fidelity
guarantees (new changes in the delta are always fully reviewed; carried-over
findings are still adjudicated; any uncertainty fails back to today's full-diff
prompt; reviewer output contract and orchestrator-owned bookkeeping unchanged).

## Why
`review_code` in agent-pair mode currently re-embeds the entire accumulated
`git diff <base>...HEAD` plus the full security checklist on every round, even
when the implementer only changed a few lines to address the prior round's
findings. As `CHANGES_REQUESTED → implement → review_code` loops, this is the
dominant reviewer-token driver (#92 background). This task implements
**#92 Proposal 2** — narrow round-over-round reviewer context for carried-over
findings — without weakening the adversarial guarantee.

## Done-when
- [ ] In `.redteam/workflows/phase_runners/review_code.py`, the agent-pair branch
      of `run()` reads `state.get("last_reviewed_rev")` (top-level state key,
      `str`; absence treated as "first round"). The new key is read-only on
      legacy state — its absence MUST take the full-review path and MUST NOT
      raise `KeyError` or any other exception.
- [ ] After `review_with_fallback` returns inside the agent-pair branch, the
      runner sets `state["last_reviewed_rev"] = git_rev_parse("HEAD",
      repo_root())` ONLY when BOTH `result["parse_status"] == "ok"` AND
      `result["decision"] in {"APPROVED", "CHANGES_REQUESTED",
      "RESCUE_REQUIRED", "ASK_USER"}`. The runner MUST NOT write
      `last_reviewed_rev` on the `MANUAL_REQUIRED` branch nor on the
      `parse_status != "ok"` fail-closed branch (so a manual-pending or
      malformed-review state never poisons the next round's narrowing).
- [ ] If the `git rev-parse HEAD` capture raises `RuntimeError`, the runner
      catches it and leaves `state["last_reviewed_rev"]` untouched (the next
      round simply uses the full-diff path) — the captured exception MUST NOT
      propagate out of `run()`.
- [ ] Narrowing precondition (ALL must be true to take the narrowed path on a
      given round): (a) `state.get("last_reviewed_rev")` is a non-empty `str`;
      (b) `state.get("review_items")` is a list containing at least one `dict`
      with `status == "open"`; (c) `git merge-base --is-ancestor <prior> HEAD`
      returns exit code 0; (d) `git diff <prior>...HEAD` exits 0 with non-empty
      stdout. If ANY precondition fails, the runner takes the full-diff path
      (no exception, no log mutation beyond what today does).
- [ ] On the narrowed path, the prompt passed to `review_with_fallback` is
      built by a new private helper
      `_narrowed_code_review_prompt(task_dir, base_branch, prior_rev,
      open_items)` in `review_code.py`. Its output is byte-deterministic for a
      given input and contains, in this order:
        1. `Act as an adversarial code-security reviewer for the implementation
           of the task at {task_dir}/. Review `git diff
           {prior_rev}...HEAD`.` (the incremental delta — full adversarial pass
           on everything that changed since the previously-reviewed revision).
        2. A line `Pinned base for the PR remains {base_branch}; the narrowed
           diff above is the round-over-round delta, not a replacement for the
           base.`
        3. A `Carried-over open findings (adjudicate each as resolved or still
           open):` section, followed by one line per open item formatted
           `- {id} severity:{severity} status:{status} — {summary}` (fields
           drawn directly from `state["review_items"]`).
        4. The same task inputs the full prompt names today: `{task_dir}/
           outcome.md`, `{task_dir}/plan_review.md`,
           `{task_dir}/impl_diff.patch`.
        5. The same project references the full prompt names today:
           `.redteam/prompts/codex/code_review.md`, `proj.security_checklist`,
           `proj.context_file`.
        6. The same stdout-only / no-file-writes / no-sentinel-touch language
           the full prompt uses today.
        7. The same closing line: `End with a final line `REVIEW_DECISION:
           APPROVED` (or CHANGES_REQUESTED / RESCUE_REQUIRED / ASK_USER), with
           IR-NNN findings above it.`
- [ ] On the full-diff path (first round OR any failed precondition), the
      runner calls the existing `_code_review_prompt(task_dir, base_branch)`
      byte-identically to today; no edits to `_code_review_prompt` are required
      beyond what is necessary to keep `test_headless_prompts_forbid_writes`
      green.
- [ ] `review_with_fallback` is invoked exactly once per round with
      `role="review_code"`, `cwd=repo_root()`,
      `target={"kind": "branch_diff", "base": pinned_base_branch(state, rr)}`
      (the pinned base, #91) — on BOTH the narrowed and the full path. The
      `target.base` MUST remain the pinned base; narrowing lives in the
      prompt only.
- [ ] The two new git probe helpers live as private module-level functions in
      `review_code.py` (NOT in `_base.py`): `_is_ancestor(prior: str, repo:
      Path) -> bool` (wraps `subprocess.run(["git", "merge-base",
      "--is-ancestor", prior, "HEAD"], ...)`, returncode 0 → True, anything
      else → False, never raises) and `_incremental_diff_nonempty(prior: str,
      repo: Path) -> bool` (wraps `subprocess.run(["git", "diff",
      f"{prior}...HEAD"], ...)`, returncode 0 with non-empty stdout → True,
      anything else → False, never raises). Both use shell-free arg lists and
      `encoding="utf-8"`, matching the discipline in `_base.py`.
- [ ] The fail-closed branches in the runner (`result["parse_status"] !=
      "ok"` → `PhaseResult(status="error", ...)`, `MANUAL_REQUIRED` →
      `PhaseResult(status="manual_required", ...)`, the
      `decision in {APPROVED, CHANGES_REQUESTED, RESCUE_REQUIRED, ASK_USER}`
      mapping to `PhaseResult.status`, and the `code_review.md`
      `write_text(..., encoding="utf-8")` artifact write) remain functionally
      identical on the narrowed path.
- [ ] The non-agent-pair / TDD sub-agent reviewer path (`run()` tail,
      `state.get("mode") != "agent-pair"`, the `impl_diff.patch`-driven fresh
      reviewer) is NOT modified — it keeps today's full-diff behavior
      unchanged.
- [ ] No new state key is added beyond `last_reviewed_rev`. The orchestrator's
      `_sync_review_items` call at `orchestrator.py` ~line 1457 and the
      `_close_phase_review_items` call at ~1493 are unchanged in signature,
      site, or behavior; the new prompt is built only from data the runner
      already has access to (`state["review_items"]`).
- [ ] `bash .redteam/scripts/verify.sh` passes (ruff check + ruff format
      --check + full pytest under `.redteam/tests/`), with no existing test
      regressing.
- [ ] A new test file at `.redteam/tests/test_review_code_narrow_context.py`
      (matching `test_*.py`) covers the first-round-full-diff,
      subsequent-round-narrowed, new-issue-still-caught, fail-safe-fallback,
      and contract-intact behaviors below using monkeypatched
      `get_reviewer_adapter`, `review_with_fallback`, `compute_repo_diff`,
      `repo_root`, `git_rev_parse`, and the two new private probe helpers — no
      `codex` / `claude` subprocess invocations and no network or remote git
      I/O.

## Verification
- Tests: `test_first_round_uses_full_diff_prompt`, `test_first_round_writes_last_reviewed_rev`, `test_subsequent_round_uses_narrowed_prompt`, `test_subsequent_round_excludes_full_range`, `test_subsequent_round_target_uses_pinned_base`, `test_new_issue_in_delta_caught_on_narrowed_path`, `test_new_issue_syncs_into_review_items`, `test_fallback_no_last_reviewed_rev`, `test_fallback_not_ancestor`, `test_fallback_empty_incremental_diff`, `test_fallback_no_open_items_empty_list`, `test_fallback_no_open_items_all_closed`, `test_manual_required_no_rev_mutation`, `test_unparseable_result_no_rev_mutation`, `test_code_review_md_written_on_success`, `test_rev_written_on_all_valid_decisions`, `test_rev_not_written_when_git_fails`
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Implementation matches the approved outcome: agent-pair runner reads `state.get("last_reviewed_rev")`, falls back to the full prompt unless all narrowing preconditions pass, preserves `target={"kind": "branch_diff", "base": base_branch}`, and only records `last_reviewed_rev` after parsed valid decisions.
- Git probes (`_is_ancestor`, `_incremental_diff_nonempty`) are shell-free, private to `review_code.py`, use arg lists with `encoding="utf-8"`, and fail closed to `False` on errors.
- Fail-closed branches do not poison narrowing: `MANUAL_REQUIRED` returns before writing `code_review.md` or mutating `last_reviewed_rev`; non-ok `parse_status` writes the artifact and returns `error` before the rev capture.
- New tests discriminate against the pre-change code (narrowed-prompt assertions and `last_reviewed_rev` capture would have failed on `main`); branch decision surface is exercised on both sides.
- Reviewer verified `verification.log` reports 493 tests passed and `state.json` records `verification.last_exit_code == 0`.
- `REVIEW_DECISION: APPROVED` — no HITs, no Disagree, no Uncertain entries.

## Generated by
redteam / batch reduce-reviewer-cost-prop2 / task task-001-narrow-review-context-rounds
