# Outcome — Plan-declared Affected files exemption for the pre-worker floor (#137)

## Goal
Stop `_floor_outside_scope` from self-locking on tracked paths that the current
task's review-approved `outcome.md` explicitly lists under **Affected files**,
by snapshotting that set **set-once** into `state.json` at the first pre-worker
floor evaluation and consuming ONLY the stored snapshot on every subsequent
floor call — so a worker cannot widen its own floor exemption by editing the
live `outcome.md` after plan approval.

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
- [ ] **Reviewed-range integrity for exempted paths (IR-001, stack review).**
      Exempting an outside-scope tracked path at the pre-worker floor must NOT
      let the worker's change to that path escape the committed reviewed range
      (`git diff <base>...HEAD`). Today `_commit_worker_diff` stages
      `_tracked_changed_paths - before_tracked`; a plan-affected file that was
      already dirty-vs-base before the set-once tracked baseline is in
      `before_tracked`, so the worker's edits to it are dropped from the commit
      while `verify.sh` runs against the worktree that has them — a stale
      review range on the very integrity boundary this family guards. Fix
      fail-closed: guarantee that any plan-affected path with changes vs base
      is EITHER committed into the reviewed range OR fails the post-commit
      integrity gate (no silent omission). Preserve the existing operator-WIP
      and adversarial-baseline guarantees — the exemption covers ONLY the
      snapshotted plan-affected set, nothing else.
- [ ] Regression proving the integrity fix: an outside-scope path listed in the
      approved `outcome.md` Affected files, dirty vs base BEFORE the first
      implement round, has the worker's changes to it reflected in the
      committed `base...HEAD` range (or the integrity gate refuses) — verify
      is never green against a worktree whose committed range omits the change.
- [ ] Engine remains stdlib-only (no new imports beyond what `implement.py`
      already uses — `pathlib`, `re` if needed for the heading match are
      stdlib).

## Out of scope
- Any change to `_cross_run_trust_root_floor`, `_is_harness_artifact`,
  `_uncommitted_scope_files`, `_uncommitted_outside_scope_files`, or
  `_commit_worker_diff`. The plan-affected exemption is limited to
  `_floor_outside_scope` per the input brief's Scope section.
- Any change to the tracked / untracked set-once baseline mechanism itself
  (key names, persistence path, reset semantics) — only a new sibling
  `implement_plan_affected_files` key is added, following the same pattern.
- Reading a sibling task's `outcome.md` for this exemption — the current task
  only; the #124 sibling allowlist (extended by task 1) already covers the
  sibling harness-artifact case.
- Prefix, glob, or trailing-slash directory expansion of listed paths — exact
  POSIX equality only. A planner-written `docs/` (trailing slash) is treated
  as a literal path and falls through to the fail-closed default.
- Any change to reviewer/worker adapters, agent prompts, agent skeletons, or
  the orchestrator / state machine.
- Any fix for #138 (planner emitting `## Verification hooks` instead of the
  parseable `## Verification` + yaml block) — that is a separate issue.
- Any doc, script, workflow, or top-level file change — task 2 must not touch
  files outside `.redteam/workflows/phase_runners/implement.py` and the new
  test file, or it self-locks at its own pre-worker floor.
- Backfill for legacy in-flight state (a task that already crossed the
  pre-worker floor before this change ships): such a state has no
  `implement_plan_affected_files` key, so the key-absent branch parses on
  next entry and stores — behavior is the correct new default without any
  migration code.

## Affected files
- `.redteam/workflows/phase_runners/implement.py`
- `(new) .redteam/tests/test_floor_plan_affected_files_exemption.py`

## Verification

```yaml
hooks:
  - bash .redteam/scripts/verify.sh
```

### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full suite (ruff check + ruff format
  check + `pytest .redteam/tests -x --tb=short`).
- `pytest .redteam/tests/test_floor_decompose_and_sibling_exemptions.py -q`
  — task 1's regression suite (batch-root + `input.md` + shared predicate).
- `pytest .redteam/tests/test_sibling_task_floor_exemption.py -q` — #124
  sibling exemption regression.
- `pytest .redteam/tests/test_baseline_trust_root_cross_run.py -q` — #117
  cross-run baseline trust-root floor regression.

### To be created (the agent-pair implementer will define exact test names)
Tests under `.redteam/tests/test_floor_plan_affected_files_exemption.py`
covering:

- **Exemption on:** a tracked path outside `source_dirs` / `test_dir` /
  task-dir (e.g. `docs/reviewer/round-staging.md`) is NOT in
  `_floor_outside_scope`'s offending set when the current task's `outcome.md`
  lists it under `Affected files` and the `plan_affected` frozenset therefore
  contains it.
- **Exemption off (fail-closed default):** the same path IS in the offending
  set when `outcome.md` does not list it (empty `plan_affected`).
- **Parser — `(new) ` prefix stripping:** `- (new) docs/x.md` and
  `- (New) docs/x.md` both contribute `docs/x.md`; `- foo (new).md` stays the
  literal `foo (new).md` (positional strip, not substring).
- **Parser — absent / empty:** absent `outcome.md`, unreadable `outcome.md`,
  or `outcome.md` with no `Affected files` heading each yield an empty
  frozenset; the outside-scope offending set is byte-identical to task 1's
  baseline.
- **Parser — malformed entries skipped:** absolute path (`- /etc/passwd`),
  `..` segment (`- ../evil`) — each is skipped, but well-formed entries in
  the SAME list are still honored (per-entry skip, not whole-file reject).
- **Parser — heading boundary:** an `Affected files` section followed by a
  same-or-higher-level heading with additional bullets does not bleed —
  those later bullets are not in the exemption set.
- **Parser — exact equality, no prefix expansion:** a listed
  `docs/reviewer/` (trailing slash) treated literally does NOT exempt
  `docs/reviewer/round-staging.md`.
- **Set-once — same-process second round:** on first pre-worker entry with
  `outcome.md` listing only path A, path A is exempt (via the stored
  snapshot). Widening the live `outcome.md` on disk to add an outside-scope
  path B and re-invoking the pre-worker floor path in the same process (the
  second round of a review backtrack) — the state key is present, the parser
  is NOT re-called, `plan_affected` remains `{A}`, and B is still offending.
- **Set-once — fresh-process re-entry:** persist `state.json` after the
  first snapshot, discard the in-memory state, reload from `state.json`,
  widen the live `outcome.md` to add B, re-enter the pre-worker path — the
  reloaded state has the key present as the original stored list, the parser
  is NOT re-called, and B is still offending.
- **Cross-run trust-root floor is NOT exempted:** even when `outcome.md`
  lists an outside-scope path, `_cross_run_trust_root_floor` Check-2 still
  returns that path as offending when it appears in
  `state["implement_untracked_baseline"]` or
  `state["implement_tracked_baseline"]` and is not otherwise allowlisted
  (adversarial baseline-rewrite security-boundary regression).
- **Default byte-identical:** for a non-goal-mode, non-stacked task with
  only in-scope tracked changes (paths under `source_dirs` / `test_dir`),
  `_floor_outside_scope` returns empty regardless of `outcome.md`'s presence
  or content — no behavioral drift on the default path.

## Risks
- Adds one new set-once key `implement_plan_affected_files` to `state.json`.
  It is additive, mirrors the existing tracked/untracked baseline convention,
  and legacy in-flight state (key-absent) parses on next entry and stores —
  no migration code, but a state-schema change nonetheless. Reviewers should
  confirm the key name does not collide with any existing state field and
  that a state.json snapshot the reviewer inspects still round-trips through
  `orchestrator status`.
- The fresh-process re-entry test needs to exercise the state.json round-trip
  without invoking the full orchestrator; the design assumes the implementer
  simulates it by writing `state.json`, re-reading it as JSON, and re-invoking
  the getter with the reloaded dict — stdlib-only, matches how existing
  baseline regressions do their round-trip.
- Task 1 (#136 fixes: batch-root allowlist, `input.md` on sibling allowlist,
  shared `_is_harness_artifact` predicate) MUST land before this task starts.
  The plan references those symbols (shared predicate, sibling `input.md`)
  and the two changes stack on the same file; if task 1 has not merged, the
  wiring described here will not apply cleanly.
- The parser tolerates the exact bullet forms + heading levels the current
  planner emits. If a future planner change materially changes the format
  (e.g. code-fenced Affected files, `####` heading), the exemption set will
  silently shrink — paths that would have been exempt become offending. This
  is a safe direction (fail-closed) but could re-trigger #137-style
  self-lock; a follow-up would need to widen the parser deliberately, not
  drift.
- The `plan_affected` argument is added as an optional keyword-only parameter
  to `_floor_outside_scope` specifically so existing test files
  (`test_floor_decompose_and_sibling_exemptions.py`,
  `test_sibling_task_floor_exemption.py`) that call `_floor_outside_scope`
  with the current 4-arg signature continue to pass without modification —
  those files are out of Affected files and MUST NOT be touched by this
  task. Reviewers should confirm the default remains `frozenset()` (not
  `None`) so the truthiness check stays consistent.
