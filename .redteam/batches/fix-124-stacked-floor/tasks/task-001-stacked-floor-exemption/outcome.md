# Outcome — Exempt sibling-task harness decision-trail artifacts from the out-of-scope tracked floor (#124)

## Goal
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

## Out of scope
- Whether the batch `state.json` (or any batch dir contents) should be tracked
  in a consumer repo at all — candidate B in #124, explicitly out of scope per
  the brief.
- Re-running `goal-mode-e2e/task-002-failure-path` — handled separately after
  this fix merges.
- Widening `_commit_worker_diff`
  (`.redteam/workflows/phase_runners/implement.py:218-296`) or
  `_uncommitted_outside_scope_files`
  (`.redteam/workflows/phase_runners/implement.py:366-399`) to be
  sibling-aware. The concrete #124 failure is the pre-worker tracked floor
  (invoked at `implement.py:441` agent-pair, `implement.py:622` tdd) — those
  two surfaces operate on different snapshots (post-worker untracked delta and
  the Layer-2 untracked gate respectively) and the brief explicitly defers
  symmetric treatment to plan_review only if a failing test proves it. No
  failing test exists for them under #124; they stay untouched.
- Widening the allowlist beyond `{state.json, outcome.md, pr.md, top-level
  *_review.md}`. Specifically NOT exempted: `input.md`, `verification.log`,
  `impl_diff.patch`, `progress.md`, `pr_url.txt`, `ask_user_response.md`,
  archived `*.round<N>.md` files, and `state.json.bak-*` snapshot files.
  These remain floor-tripping if tracked-modified on a sibling. Re-opening
  the allowlist is a separate decision, not done silently here.
- Cross-batch exemption. Only sibling task dirs under the SAME batch's
  `tasks/` root (i.e. `task_dir.parent`) are exempted.
- Subdirectory exemption. A sibling-task subdirectory (any path containing
  `/` after the sibling-id segment) is NEVER exempted, even if its basename
  is on the allowlist.
- Cross-run trust-root floor
  (`_cross_run_trust_root_floor`, `.redteam/workflows/phase_runners/implement.py:155-215`).
  That floor handles the untracked surface and the stored-baseline contents;
  it is unrelated to the #124 tracked-floor failure and unchanged here.
- Any refactor of the floor / `_in_task_dir` / `_scope_root` idiom beyond the
  minimum needed to express the sibling-artifact allowlist.
- Engine project-fingerprinting. The `<batch>/tasks/<task-id>/` layout is a
  generic harness concept (used in `examples/fastapi-like/` and every
  consumer) — no new project/stack-specific assumption is introduced.

## Affected files
- `.redteam/workflows/phase_runners/implement.py` — extend
  `_floor_outside_scope` (and update its docstring) to additionally exempt
  paths that match the sibling-artifact rule described in Done-when item 1:
  same-batch `tasks/<sibling-id>/{state.json|outcome.md|pr.md|*_review.md}`,
  TOP-LEVEL ONLY (the relative-to-sibling-dir path must contain no `/`).
  Reuse the existing `_scope_root` / `_in_task_dir` POSIX-prefix idiom and
  `fnmatch.fnmatchcase` from the stdlib for the `*_review.md` glob; do not
  refactor the floor or change the existing `_in_task_dir` exemption for the
  current task dir.
- `(new) .redteam/tests/test_sibling_task_floor_exemption.py` — regression
  tests for the new sibling-aware exemption: top-level proceeds case
  (each of `state.json`, `outcome.md`, `pr.md`, a `*_review.md` filename),
  subdirectory-allowlisted-basename still-trips case (the explicit PR-002
  negative test, e.g. `tasks/<sibling>/sub/state.json`), non-allowlisted
  top-level still-trips case, cross-batch still-trips case, and a root-level
  still-trips regression. Each behavior is asserted in BOTH `_run_agent_pair`
  and the `run(mode="tdd")` path. The agent-pair implementer writes this
  file together with the implementation change.

## Verification

```yaml
commands:
  - "bash .redteam/scripts/verify.sh"
```

## Verification hooks
### Existing (must continue to pass)
- `bash .redteam/scripts/verify.sh` — full ruff + pytest suite over
  `.redteam/`.
- `.redteam/tests/test_tracked_baseline_attribution.py` — already covers the
  out-of-scope tracked floor's fail-closed behavior on root-level paths
  (`README.md`, `NOTES.md`) and the in-scope pre-edit attribution. Must
  continue to pass byte-for-byte.
- `.redteam/tests/test_implementer_commit.py`,
  `.redteam/tests/test_implement_untracked_baseline_pin.py`,
  `.redteam/tests/test_baseline_trust_root_cross_run.py`,
  `.redteam/tests/test_preimplement_snapshot_invariant.py` — cover adjacent
  floors / baselines that share the `_in_task_dir` idiom; must continue to
  pass unchanged (the surgical-changes guarantee for the flat
  single-task_dir path).
- `.redteam/tests/test_agents_generic_prompts.py` — engine-agnosticism
  guard; must remain green (no project/stack-specific fingerprint
  introduced in `.redteam/workflows/`).

### To be created (the agent-pair implement phase will define exact test names)
Tests under `.redteam/tests/` (matching the project's `test_*.py` pattern),
authored alongside the implementation change in the agent-pair mode
`implement` phase, that exercise BOTH `impl._run_agent_pair` and
`impl.run` with `state["mode"]` set so the tdd branch at
`implement.py:586` is taken, and cover:
- **Proceeds-on-sibling-top-level-artifact** (one assertion per basename):
  a stacked layout where the current task lives at
  `<batch>/tasks/<this-id>/` and a sibling task dir
  `<batch>/tasks/<sibling-id>/` contains a tracked-modified path equal to
  `<batch>/tasks/<sibling-id>/state.json` (and separately:
  `<batch>/tasks/<sibling-id>/outcome.md`,
  `<batch>/tasks/<sibling-id>/pr.md`,
  `<batch>/tasks/<sibling-id>/code_review.md` as a `*_review.md` example).
  The floor MUST NOT fire and the worker adapter MUST be invoked.
- **Still-trips-on-sibling-subdirectory-with-allowlisted-basename**
  (the explicit PR-002 negative test): the sibling task dir contains a
  tracked-modified path like `<batch>/tasks/<sibling-id>/sub/state.json`
  or `<batch>/tasks/<sibling-id>/archive/code_review.md`. The floor MUST
  fire (`status == "error"`, "commit or stash" in `feedback`, the
  offending path in `feedback`) and the worker adapter MUST NOT be
  invoked.
- **Still-trips-on-non-allowlisted-top-level-sibling-path**: the sibling
  task dir contains a tracked-modified path like
  `<batch>/tasks/<sibling-id>/scratch.py`,
  `<batch>/tasks/<sibling-id>/input.md`, or
  `<batch>/tasks/<sibling-id>/verification.log` (basenames the brief and
  plan_review chose NOT to include). The floor MUST fire and the worker
  adapter MUST NOT be invoked.
- **Still-trips-on-cross-batch-allowlisted-path**: a tracked-modified path
  under a task dir in a DIFFERENT batch
  (e.g. `<other-batch>/tasks/<id>/state.json`). The floor MUST fire and
  the worker adapter MUST NOT be invoked.
- **Still-trips-on-root-level-out-of-scope-path** (regression guard for
  the existing behavior on a stacked layout): the only tracked-modified
  out-of-scope path is `README.md` at the repo root. The floor MUST fire.
- **Mode-neutrality**: each behavioral case above is asserted in BOTH the
  agent-pair and tdd implement paths, mirroring the dual-helper
  `_wire_agent_pair` / `_wire_tdd` pattern at
  `.redteam/tests/test_tracked_baseline_attribution.py:84-122`.

## Risks
- **`task_id` identity check derivation.** Identifying "sibling" requires
  knowing the current task's directory segment vs the other task dir's
  segment under `task_dir.parent`. The plan derives the current segment
  from `task_dir.name` (on-disk), matching how the existing `_in_task_dir`
  derives `task_rel` from `task_dir` (not from `state["task_id"]`). If
  plan_review prefers `state["task_id"]` instead, that decision is
  re-recorded here, not silently changed at implement time.
- **`*_review.md` glob semantics.** The plan uses
  `fnmatch.fnmatchcase(name, "*_review.md")` against the
  relative-to-sibling-dir path AFTER asserting it contains no `/`. That
  rejects `code_reviewXmd` and accepts `code_review.md`, `test_review.md`,
  `plan_review.md`. If plan_review prefers a stricter regex (e.g. anchored
  `[A-Za-z0-9_]+_review\.md`), the implementer adopts that without
  widening the allowlist surface.
- **Test placement (new file vs extending an existing one).** Plan creates
  a new `test_sibling_task_floor_exemption.py` rather than extending
  `test_tracked_baseline_attribution.py`. Either choice is small; the
  new-file choice keeps the #124 concern diff-isolated for the reviewer.
- **No new dependency / no project fingerprint** is introduced; the change
  stays inside the existing POSIX-prefix idiom and uses stdlib
  (`fnmatch`) only. Flagged here as an explicit non-risk in case
  plan_review proposes a regex library or a heuristic that fingerprints
  the harness layout in a project-specific way — that would violate the
  engine's zero-dependency / project-agnostic rule.
