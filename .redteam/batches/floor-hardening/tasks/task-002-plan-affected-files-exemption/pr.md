## What
Stop `_floor_outside_scope` from self-locking on tracked paths that the current
task's review-approved `outcome.md` explicitly lists under **Affected files**,
by snapshotting that set **set-once** into `state.json` at the first pre-worker
floor evaluation and consuming ONLY the stored snapshot on every subsequent
floor call — so a worker cannot widen its own floor exemption by editing the
live `outcome.md` after plan approval.

## Why
Follow-up to issue #137, filed during the first autonomous goal run: on a
review backtrack the pre-worker out-of-scope tracked floor (`_floor_outside_scope`
in `implement.py`, from #91 Part A) was refusing paths the worker had legally
created in round 1 when those paths were declared by the review-approved
`outcome.md` (e.g. a doc under `docs/`). The floor was refusing the task's own
approved scope. This task teaches the floor to honor the plan's explicit
Affected files while snapshotting the exemption set-once so a live edit of
`outcome.md` after approval cannot widen it — and deliberately does NOT extend
the exemption to `_cross_run_trust_root_floor`, which stays strict.

## Done-when
- [ ] `bash .redteam/scripts/verify.sh` passes (ruff check + ruff format check +
      `pytest .redteam/tests -x --tb=short`).
- [ ] `.redteam/workflows/phase_runners/implement.py` defines a pure parser
      `_plan_affected_files(task_dir: Path) -> frozenset[str]` (stdlib-only,
      colocated with `_scope_root` / `_is_harness_artifact`) that:
      - reads `task_dir / "outcome.md"` (utf-8);
      - locates the FIRST `#`/`##`/`###` heading whose stripped title matches
        `Affected files` case-insensitively; stops at the next heading of the
        same or higher level, so the parse never bleeds into a later section;
      - collects bullet items (`- ` and `* `) under that heading only;
      - case-insensitively strips a **single leading positional** `(new) `
        prefix (with any trailing whitespace) from each item — so
        `- (new) foo/bar.py` → `foo/bar.py`, `- (New) docs/x.md` → `docs/x.md`,
        and `- foo (new).md` stays the literal path `foo (new).md`;
      - trims surrounding whitespace and backticks and normalizes with
        `.replace("\\", "/")`;
      - drops empty entries; drops entries that are absolute, contain a `..`
        segment, or escape the repo root (per-entry skip — the rest of the
        same list stays honored);
      - returns an empty frozenset when `outcome.md` is absent, unreadable, or
        contains no `Affected files` heading (fail-closed default).
- [ ] `implement.py` defines a **set-once** getter
      `_get_or_set_plan_affected_files_baseline(state, task_dir) -> frozenset[str]`
      keyed on `state["implement_plan_affected_files"]`, mirroring the
      `get_or_set_tracked_baseline` / `get_or_set_untracked_baseline` pattern in
      `_base.py`:
      - key-present (stored as a `list`) → return `frozenset(list)` WITHOUT
        touching `_plan_affected_files` and WITHOUT mutating the stored value;
      - key-absent → call `_plan_affected_files(task_dir)` exactly once, store
        the result as a **sorted list of strings** back into
        `state["implement_plan_affected_files"]`, return the matching frozenset
        (including the empty-list case — a fail-closed empty parse is stored
        empty and never re-parsed);
      - does NOT persist by itself — the caller runs `persist_state(task_dir,
        state)` on the same call that already flushes the tracked/untracked
        baselines.
- [ ] `_floor_outside_scope` gains a keyword-only parameter
      `plan_affected: frozenset[str] = frozenset()` and treats "path is in
      `plan_affected` (exact POSIX equality)" as an additional allowed-predicate
      branch alongside the shared `_is_harness_artifact` predicate. The default
      empty frozenset preserves existing behavior for any caller that omits
      the argument (e.g. existing tests in
      `test_floor_decompose_and_sibling_exemptions.py` and
      `test_sibling_task_floor_exemption.py`).
- [ ] `_run_agent_pair` and the tdd `run` path both, on their pre-worker
      snapshot line, call `_get_or_set_plan_affected_files_baseline(state,
      task_dir)` BEFORE `_floor_outside_scope`, pass the returned frozenset
      into `_floor_outside_scope(..., plan_affected=...)`, and rely on the
      existing `persist_state(task_dir, state)` call to durably flush the
      snapshot alongside the tracked/untracked baselines.
- [ ] The state snapshot is NEVER re-read from live `outcome.md` after the
      key is present: on any subsequent `_floor_outside_scope` call within the
      same process (later round) or after a fresh process reload of
      `state.json`, the exemption set is the stored list unchanged. Widening
      the live `outcome.md` to add an outside-scope path after the snapshot
      DOES NOT cause that path to be exempted.
- [ ] `_cross_run_trust_root_floor` and `_is_harness_artifact` are unchanged
      in behavior: neither Check-1 nor Check-2 consults
      `state["implement_plan_affected_files"]`; a stored-baseline outside-scope
      entry not otherwise allowlisted still trips Check-2 even when
      `outcome.md` names it.
- [ ] `pytest .redteam/tests/test_floor_plan_affected_files_exemption.py -q`
      passes, with tests covering each behavior enumerated below.
- [ ] `pytest .redteam/tests/test_floor_decompose_and_sibling_exemptions.py
      .redteam/tests/test_sibling_task_floor_exemption.py
      .redteam/tests/test_baseline_trust_root_cross_run.py -q` still passes
      byte-identically (this task does NOT modify those files).
- [ ] Engine remains stdlib-only (no new imports beyond what `implement.py`
      already uses — `pathlib`, `re` if needed for the heading match are
      stdlib).

## Verification
- Tests: test_plan_affected_exemption_on, test_plan_affected_exemption_off_default, test_parser_new_prefix_lowercase_stripped, test_parser_new_prefix_mixed_case_stripped, test_parser_new_suffix_not_stripped, test_parser_new_prefix_inside_backticks, test_parser_absent_outcome_md, test_parser_no_affected_files_heading, test_parser_empty_affected_files_section, test_parser_unreadable_outcome_md, test_parser_absent_outcome_md_floor_unchanged, test_parser_absolute_path_skipped_well_formed_honored, test_parser_dotdot_segment_skipped_well_formed_honored, test_parser_same_level_heading_stops_section, test_parser_higher_level_heading_stops_section, test_parser_subheading_does_not_stop_section, test_parser_trailing_slash_not_directory_prefix, test_set_once_same_process_widened_outcome_ignored, test_set_once_fresh_process_reentry, test_set_once_empty_parse_stored_and_not_reparsed, test_cross_run_floor_not_exempted_by_plan_affected, test_cross_run_floor_check1_not_exempted_by_plan_affected, test_default_in_scope_path_always_exempt, test_default_path_empty_current_tracked
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff scope confirmed limited to the two approved files (`implement.py` + the new test file); no drift into adapters, prompts, or docs.
- Set-once snapshot is correctly wired BEFORE `_floor_outside_scope` in both the agent-pair path (`_run_agent_pair`) and the tdd `run` path, matching the tracked/untracked baseline convention.
- Security boundary held: `_cross_run_trust_root_floor` continues to consult only `_is_harness_artifact`, never `plan_affected` — a plan cannot weaken the cross-run baseline trust floor.
- Fail-closed parser confirmed: absent, unreadable, and undecodable `outcome.md` all yield `frozenset()`; malformed per-entry (absolute paths, `..` segments) are skipped without rejecting the whole list.
- IR-001 (major) resolved round-over-round by adding `UnicodeDecodeError` to the parser's `try/except`, plus IR-002 test coverage for the undecodable case. Final decision: `REVIEW_DECISION: APPROVED`.
- Stdlib-only preserved (`re` is stdlib); no new runtime dependency.

## Generated by
redteam / batch floor-hardening / task task-002-plan-affected-files-exemption
