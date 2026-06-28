# Slice A: single-parent DAG manifest + task-on-task branching

Implements **Slice A** of the goal-mode design (#94). The accepted umbrella design
is `docs/decisions/2026-06-27-goal-mode-design.md` — read it; this brief is the
Slice A cut of it. Security-adjacent (extends the #91 `base_branch` pin), so this
task goes through `plan_review` before any code.

## Goal
A batch may carry an optional `goal.json` manifest declaring a **single-parent
forest** of task dependencies; the orchestrator runs dependent tasks **stacked on
their parent's task branch** (so each dependent's reviewed range and PR base are
exactly its delta), with fail-closed guards so a moved/stale parent can never
silently produce a wrong stacked PR. Absent `goal.json`, behavior is byte-for-byte
unchanged. **No auto-merge** — the draft-PR stack stays the human checkpoint.

## What to build
1. **Manifest schema + up-front validation (§1a).** Optional `goal.json` at batch
   root: `{ "goal": str, "ceilings": {...}, "tasks": { "<task-id>": {"depends_on":
   [...]} } }`. When present, validate the WHOLE manifest in `process_batch`
   BEFORE seeding any state or running any task, and **abort the entire goal batch
   (fail closed)** on any of: unknown task ref in `depends_on`, self-dependency,
   duplicate task IDs, a task key with no matching on-disk task dir, **≥2 entries
   in any `depends_on`** (v1 single-parent rule), or a cycle (toposort; back-edge
   → abort). Stdlib only (`json` + trivial toposort). `ceilings` is parsed/stored
   but NOT enforced here (that is Slice C) — accept and ignore beyond schema.
2. **Dependency-aware layered scheduler (§1b).** Run in topo layers: a task is
   runnable only when its parent (if any) is `done`. Independent roots run as
   today. A task whose parent is not `done` (deferred/failed/blocked) is **skipped
   as `blocked_on_dependency`** — not run — and other chains continue.
3. **Pin-before-branch, branch-from-the-pin (§1c).** Reorder `process_task` so the
   resolved base (parent's branch `<branch_prefix>/<parent_id>` for a dependent,
   else `cfg.project.base_branch`) is pinned into `state["base_branch"]` FIRST,
   then `_ensure_task_branch` is called with that pinned base — not live config.
   The scheduler passes the resolved parent in; `process_task` does not re-read the
   manifest. Preserve the existing "pin once / legacy-unpinned-with-writable-phase
   fails closed" invariant.
4. **Ancestry fail-closed on reused branches (§1d).** Before running a dependent
   whose task branch already exists, verify `git merge-base --is-ancestor
   <pinned-base> <task-branch>`; if the branch does NOT descend from the pinned
   parent, fail closed (defer with a clear reason). **Never auto-delete** the
   consumer's branch.
5. **Freeze guard centralized at the read path (§1e).** At pin time record
   `state["base_branch_sha"]` = parent branch tip SHA. Put the freeze check INSIDE
   `pinned_base_branch(state, repo)` itself (thread the `repo` path in; callers
   have it): the accessor re-resolves the live parent tip and **raises (fails
   closed)** if it differs from the recorded SHA. Do NOT enumerate call sites — the
   centralized accessor makes every reader (`review_code`, `create_pr`, tier guard,
   `implement`, the TDD `write_test` path, and any future reader) fail closed
   automatically. Root tasks: base is config base, no SHA recorded, guard is a
   no-op.
6. **Parent-aware pull skip (§1f).** The existing unconditional
   `git pull --ff-only origin <base>` (`orchestrator.py:777`) must be SKIPPED when
   the base is a parent task branch (local-branch-authoritative). Decide this on an
   **explicit `base_is_parent` signal** the scheduler already knows (or membership
   in the manifest-derived parent-branch set) — **NOT** a
   `base_branch.startswith(f"{branch_prefix}/")` string match, which could collide
   with a project's real remote config base.

## Constraints
- **Engine stays project-agnostic; stdlib-only; zero runtime deps.** No
  project/stack fingerprints in `.redteam/workflows/`.
- **Backward compatible:** absent `goal.json`, `process_batch`/`process_task`
  behave exactly as today (flat, sorted, independent; existing tests stay green).
- **Preserve the #91 pin invariants** and the `_writable_phase_started`
  fail-closed legacy guard. Centralizing the freeze check must not weaken pin-once.
- **No auto-merge, no auto-delete of consumer branches, no force-push.**
- Match existing orchestrator/phase-runner style; minimum code.

## Out of scope
- The goal→task **decomposer** and decomposition review (Slice B).
- **Ceilings enforcement** + goal-level done-criterion (Slice C) — parse-and-ignore
  `ceilings` only.
- **Multi-parent / integration-branch** DAGs (≥2 deps fails closed; future work).
- Changing the standalone `review` command / `cmd_review` (not pipeline consumers).
- Auto-retarget of stacked PRs after bottom-up merges (operator-manual in v1).

## Affected files
- `.redteam/workflows/orchestrator.py` — `process_batch` (manifest load +
  validation + layered scheduler), `process_task` (pin-before-branch, parent
  resolution, ancestry check), `_ensure_task_branch` (pull-skip signal), tier
  guard already consumes the pin.
- `.redteam/workflows/phase_runners/_base.py` — `pinned_base_branch` (thread
  `repo`, add the centralized freeze guard); a small `git rev-parse` helper.
- Possibly `.redteam/workflows/config.py` if a manifest/scheduler knob is needed
  (prefer not to add config surface).
- Tests under `.redteam/tests/` — new manifest/scheduler/branching invariant tests.
- Callers of `pinned_base_branch` across `phase_runners/*` get the new `repo` arg.

## Verification
- `bash .redteam/scripts/verify.sh` stays green (ruff check + ruff format --check +
  pytest), including `test_agents_generic_prompts.py` and `test_install.py`.
- New tests assert the real invariants (not just manifest parsing):
  - pinned base (parent branch) is what `_ensure_task_branch` checks out from;
  - a pre-existing wrong-base task branch fails closed (§1d);
  - a moved parent tip fails closed (§1e) — specifically a tip moved AFTER
    `implement`, before `review_code` AND before `create_pr`, fails closed at each;
  - the centralized guard also fails closed on the TDD `write_test` path (assert
    via that path, not just agent-pair phases);
  - a moved remote parent-task branch does NOT mutate the local parent (pull
    skipped — §1f);
  - `create_pr` receives the parent branch as `--base`; review range is
    `parent...HEAD`;
  - manifest validation aborts the whole batch on cycle / unknown ref / self-dep /
    dup / ≥2 deps, seeding NO state;
  - absent `goal.json` → behavior byte-for-byte unchanged (backward-compat test).

## Risks
- Threading `repo` into `pinned_base_branch` touches every caller — must stay a
  pure additive arg without changing existing call semantics for root tasks.
- The `base_is_parent` signal must be plumbed from the scheduler to the
  pull-skip site without `process_task` re-reading the manifest (§1c keeps that
  boundary). If plumbing proves awkward, surface it at the gate rather than
  falling back to a prefix-string match.
- Centralizing a `git rev-parse` inside the pin accessor adds a subprocess call
  per pin read; confirm the read frequency is per-phase (handful), not hot-loop.
- Backward-compat surface is large (every existing batch test) — the absent-
  manifest path must be provably unchanged.
