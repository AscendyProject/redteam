# Task 2 — Plan-declared Affected files exemption for the pre-worker floor (#137)

## Context

Stacks on **task 1** (#136 fixes) — task 1 will have already landed the
batch-root decompose allowlist, added `input.md` to the sibling allowlist,
and reconciled `_cross_run_trust_root_floor._is_allowed` with the #124
sibling allowlist. This task builds directly on that work in the same file.

Issue **#137**: on a review backtrack, the pre-worker out-of-scope tracked
floor (`_floor_outside_scope` in
`.redteam/workflows/phase_runners/implement.py`, #91 Part A) self-locks on
paths the worker itself created in round 1 when those paths were explicitly
**declared** by the review-approved `outcome.md` — e.g. a plan that legally
places a doc file at `docs/reviewer/round-staging.md`. Those paths are the
task's scope by definition; the floor is refusing the task's own approved
scope. Everything the plan did **not** declare stays guarded exactly as
today.

## What to change

All engine changes belong in
`.redteam/workflows/phase_runners/implement.py`; all tests belong under
`.redteam/tests/`. Do **not** touch `docs/`, adapters, prompts, or any
file outside `.redteam/workflows/` and `.redteam/tests/`.

Extend `_floor_outside_scope` (only; **not**
`_cross_run_trust_root_floor` — see "Scope" below) so that a tracked path
is exempt when the current task's **review-approved** `outcome.md` lists
it under Affected files. Concretely:

1. Read the current task's `outcome.md` (`task_dir / "outcome.md"`, the
   same file the planner writes and the reviewer approves).
2. Parse its **Affected files** section into a set of repository-relative
   POSIX paths.
3. If parsing succeeds, add "path is in that set" as an additional
   allowed-predicate branch in `_floor_outside_scope`, alongside the
   existing task-dir, scope-roots, sibling top-level allowlist, and
   batch-root allowlist branches. A path present in that set is **exempt
   from the pre-worker tracked floor**.
4. If `outcome.md` is absent, unreadable, or the Affected files section
   cannot be located/parsed, the exemption set is empty and the floor
   behaves exactly as it does after task 1 (fail-closed default).

### Parsing rules

- Look for a Markdown section titled `Affected files` (case-insensitive
  heading match on `#`/`##`/`###` levels). Any conventional heading level
  the planner emits must work; take the first such section.
- Each list item under that section is one path. Support the standard
  bullet forms the planner uses (`- ...`, `* ...`).
- **Tolerate the `(new) ` prefix convention**: a list item `- (new) foo/bar.py`
  contributes the path `foo/bar.py`. Match the prefix case-insensitively
  and strip a single leading occurrence only; do not silently strip
  anything else.
- Normalize each path with `.replace("\\", "/")` and strip surrounding
  whitespace and backticks; discard empty entries.
- Stop at the next heading of the same or higher level — do not bleed
  into the next section.
- Reject entries that are absolute paths, contain `..` segments, or
  escape the repo root; the exemption set never authorizes paths outside
  the repo. (Fail-closed on malformed entries: skip them, do not treat
  the whole file as parseable-empty.)

Keep everything stdlib-only. A small helper such as
`_plan_affected_files(task_dir: Path) -> frozenset[str]` inside
`implement.py` is fine; keep it colocated with the floors, next to
`_scope_root`.

## Scope

- `_floor_outside_scope` **only**. Do **not** extend the exemption to
  `_cross_run_trust_root_floor`: the plan can declare files inside
  the scope-roots or task dir freely, and outside those two the
  cross-run trust root is meant to be strict — a plan that legally
  declares a doc path merely means the current task's live floor
  tolerates it once, not that the set-once baseline should carry it
  across runs. If a follow-up decides otherwise it belongs on its own
  issue.
- The exemption applies to the **current task**'s `outcome.md`,
  resolved relative to `task_dir`. Do not read sibling `outcome.md`
  files for this exemption (their Affected files belong to other tasks;
  the #124 sibling allowlist already covers the harness-artifact
  case).
- The exemption must have no effect on any other check (the sweep's
  operator-WIP exclusion, `_uncommitted_outside_scope_files`, etc.).

## Files affected

- `.redteam/workflows/phase_runners/implement.py`
- `.redteam/tests/test_floor_plan_affected_files_exemption.py` (new)

The Affected files section in `outcome.md` must list **exactly** those two
paths (prefix new files with `(new) `), and no others.

## Tests to add (new file, task-scoped)

Put the new tests in
`.redteam/tests/test_floor_plan_affected_files_exemption.py`. Cover, at
minimum:

- A tracked path outside `source_dirs`/`test_dir` (e.g. `docs/reviewer/round-staging.md`)
  is **not** in the offending set when the current task's `outcome.md`
  lists it under Affected files.
- Same path **is** in the offending set when `outcome.md` does not list
  it (default fail-closed).
- The `(new) ` prefix is stripped case-insensitively (`- (new) docs/x.md`
  and `- (New) docs/x.md` both exempt `docs/x.md`), and `- foo (new).md`
  is treated as the path `foo (new).md` — the prefix strip is
  positional, not substring.
- Absent `outcome.md`, or `outcome.md` with no `Affected files` heading,
  yields an empty exemption set (behavior byte-identical to task 1's
  baseline).
- Malformed entries (absolute path, `..` segment) are skipped silently;
  well-formed entries in the same list are still honored.
- The exemption **does not** cover
  `_cross_run_trust_root_floor` (a stored baseline entry outside scope
  and not otherwise allowlisted still trips Check-2 even when
  `outcome.md` names it — security boundary regression).
- Default behavior for non-goal-mode, non-stacked, in-scope-only tasks
  is byte-identical: an in-scope path (under `source_dirs`) neither
  changes the offending set nor requires `outcome.md` to be present.
- Every existing regression in
  `test_sibling_task_floor_exemption.py`,
  `test_baseline_trust_root_cross_run.py`, and the new tests from task 1
  still passes (do **not** modify those files in this task; run them as
  part of `verify.sh`).

## Constraints and non-goals

- Fail-closed. Only paths that the current task's `outcome.md`
  **explicitly** lists under Affected files are exempted; everything
  else stays refused.
- No broad prefix exemptions, no globs — Affected-files entries are
  matched by **exact POSIX equality** against `current_tracked`. If the
  planner writes a directory (trailing `/`), do not expand it; treat it
  as a literal path and let the fail-closed default handle it.
- Engine stays project-agnostic, stdlib-only, zero runtime deps.
- No changes to reviewer/worker adapters or prompts. No changes to the
  planner's output format (this task consumes it; #138 fixes the
  planner side).
- No redesign of the set-once baseline mechanism or the commit sweep.
- `outcome.md` **must** include a parseable `## Verification` section
  containing a fenced ```yaml``` block invoking
  `bash .redteam/scripts/verify.sh` — the planner's `## Verification hooks`
  prose form does **not** parse (known #138 pitfall). Example:

  ```
  ## Verification

  ```yaml
  hooks:
    - bash .redteam/scripts/verify.sh
  ```
  ```

## Operator delegation

Narrow plan-level scope questions during this run are delegated to the
operator agent (prefer the narrowest exemption that unblocks the goal).
Weakening the floor's security boundary beyond "current task's approved
`outcome.md` Affected files" is **not** delegated — that stops the run.
