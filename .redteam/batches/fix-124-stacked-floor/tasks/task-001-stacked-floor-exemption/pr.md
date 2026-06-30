## What
In a stacked goal-mode run, the dependent task's `implement` phase must NOT
defer on the **out-of-scope tracked floor** (`_floor_outside_scope` in
`.redteam/workflows/phase_runners/implement.py`) when the only out-of-scope
tracked change vs the pinned base is a **sibling task's top-level
harness-owned decision-trail artifact** (`state.json`, `outcome.md`, `pr.md`,
or a single filename matching `*_review.md`) directly under the SAME batch's
`tasks/<sibling-id>/`. The floor's fail-closed behavior against any other
out-of-scope tracked change — including a harness-named basename buried in a
sibling task's subdirectory, an arbitrary non-allowlisted file under a
sibling task dir, or any cross-batch path — must be preserved.

## Why
The #94 goal-mode e2e dogfood (batch `goal-mode-e2e`,
`task-002-failure-path` pinned to `redteam/task-001-happy-path`) deferred at
`implement` because the parent task's tracked `state.json` was a legitimate
on-disk mutation (orchestrator finalizing the parent) that the current
`_floor_outside_scope` saw as out-of-scope tracked WIP. The existing
exemption rationale — harness decision-trail artifacts are the harness's own
files, not operator WIP — already applied; it was just scoped to the current
task dir only. This PR extends the exemption surface to sibling top-level
artifacts within the same batch, without weakening the floor against genuine
operator WIP.

## Done-when
- [ ] `_floor_outside_scope` in
      `.redteam/workflows/phase_runners/implement.py` no longer flags a path
      `p` (POSIX-normalized, repo-relative) that satisfies ALL of:
      (a) `p` starts with `<this-task-dir>.parent.as_posix() + "/"`, i.e. it
          is under the **same batch's** `tasks/` root;
      (b) the next path segment is some `<sibling-id>` that is NOT the
          current task's directory name (`task_dir.name`); AND
      (c) the path relative to that sibling task dir is **exactly** one of
          `state.json`, `outcome.md`, `pr.md`, OR a single filename matching
          the glob `*_review.md` with **no `/` in it** (i.e. a top-level
          sibling artifact, never a path under a sibling subdirectory).
- [ ] An allowlisted basename **buried in a sibling task subdirectory**
      (e.g. `<batch>/tasks/<sibling-id>/sub/state.json`,
      `<batch>/tasks/<sibling-id>/archive/foo_review.md`) **still trips**
      the floor and the worker adapter is NOT invoked. Asserted by a test
      under `.redteam/tests/`.
- [ ] A non-allowlisted top-level path under a sibling task dir
      (e.g. `<batch>/tasks/<sibling-id>/scratch.py`,
      `<batch>/tasks/<sibling-id>/input.md`,
      `<batch>/tasks/<sibling-id>/verification.log`,
      `<batch>/tasks/<sibling-id>/impl_diff.patch`) **still trips** the
      floor and the worker adapter is NOT invoked. Asserted by a test
      under `.redteam/tests/`. (Per plan_review preference: the allowlist
      stays narrow — `input.md`, `verification.log`, `impl_diff.patch`,
      `progress.md`, `pr_url.txt`, `ask_user_response.md`,
      `*.round<N>.md`, and `state.json.bak-*` are NOT exempted by this
      change.)
- [ ] A tracked path outside source/test scope AND outside any sibling task
      dir (e.g. a root-level `README.md`, `NOTES.md`) **still trips** the
      floor. Existing assertions in
      `.redteam/tests/test_tracked_baseline_attribution.py` (notably
      `test_out_of_scope_tracked_fails_closed_no_baseline_agent_pair` at
      line 221 and the tdd twin at line 257) continue to pass unchanged.
- [ ] A path under a task dir in a **different batch**
      (e.g. `<other-batch>/tasks/<id>/state.json`) **still trips** the
      floor — the exemption is scoped to the same batch's `tasks/` root
      only (i.e. the current task dir's PARENT). Asserted by a test under
      `.redteam/tests/`.
- [ ] A stacked dependent task whose only out-of-scope tracked change is a
      sibling task's top-level allowlisted artifact (e.g.
      `<batch>/tasks/<sibling-id>/state.json`) proceeds:
      `_floor_outside_scope` returns an empty set, the early-return at
      `.redteam/workflows/phase_runners/implement.py:442-448`
      (agent-pair) and `.redteam/workflows/phase_runners/implement.py:623-629`
      (tdd) does NOT fire, and the worker adapter IS invoked. Asserted by
      a test under `.redteam/tests/` for each of the four basenames
      (`state.json`, `outcome.md`, `pr.md`, a `*_review.md` example).
- [ ] The flat (non-stacked) single-task_dir exemption path is preserved:
      every existing test in
      `.redteam/tests/test_tracked_baseline_attribution.py`,
      `.redteam/tests/test_implementer_commit.py`,
      `.redteam/tests/test_implement_untracked_baseline_pin.py`,
      `.redteam/tests/test_baseline_trust_root_cross_run.py`, and
      `.redteam/tests/test_preimplement_snapshot_invariant.py` passes
      unchanged.
- [ ] Both implement paths are covered mode-neutrally: every new behavioral
      case is asserted via BOTH `impl._run_agent_pair` (agent-pair) AND
      `impl.run` with `state["mode"]` set so the tdd branch at
      `.redteam/workflows/phase_runners/implement.py:586` is taken,
      mirroring the dual-helper `_wire_agent_pair` / `_wire_tdd` pattern at
      `.redteam/tests/test_tracked_baseline_attribution.py:84-122`.
- [ ] `bash .redteam/scripts/verify.sh` exits 0 (ruff + pytest over
      `.redteam/`).

## Verification
- Tests: `test_sibling_top_level_artifact_proceeds_agent_pair`, `test_sibling_top_level_artifact_proceeds_tdd`, `test_sibling_subdirectory_allowlisted_still_trips_agent_pair`, `test_sibling_subdirectory_allowlisted_still_trips_tdd`, `test_non_allowlisted_sibling_top_level_still_trips_agent_pair`, `test_non_allowlisted_sibling_top_level_still_trips_tdd`, `test_cross_batch_allowlisted_still_trips_agent_pair`, `test_cross_batch_allowlisted_still_trips_tdd`, `test_root_level_out_of_scope_still_trips_agent_pair`, `test_root_level_out_of_scope_still_trips_tdd`
- Verify command: `bash .redteam/scripts/verify.sh` ✅ (558 passed, exit 0)

## Code review summary
- Diff is scoped to `.redteam/workflows/phase_runners/implement.py` (extends `_floor_outside_scope` + docstring) and a new `.redteam/tests/test_sibling_task_floor_exemption.py`; no refactor of the existing `_in_task_dir` / `_scope_root` idiom.
- Sibling exemption is top-level only: the no-slash guard runs BEFORE `fnmatch.fnmatchcase(name, "*_review.md")`, so sibling subdirectories with an allowlisted basename still fail-closed (plan_review PR-002).
- Cross-batch paths, root-level out-of-scope paths, non-allowlisted sibling basenames, and the current-task self-path all continue to trip the floor (asserted with negative tests in both `_run_agent_pair` and `run(mode="tdd")` paths).
- IR-001 (carried subprocess encoding rule) resolved — the new `_git` test helper pins `text=True, encoding="utf-8"`.
- Stdlib only (`fnmatch`) — no new dependency, no project/stack fingerprint added to `.redteam/workflows/`.
- `REVIEW_DECISION: APPROVED` (Codex, round 2).

## Generated by
redteam / batch fix-124-stacked-floor / task task-001-stacked-floor-exemption
