# Goal mode (#94) — design accepted after 3-round cross-provider plan_review

Status: **design ACCEPTED at the umbrella level** (Claude implementer ↔ Codex
reviewer, 3 rounds, 2026-06-27). All round-1 blockers and round-2/round-3
findings resolved at design level; two impl-level notes carry into **Slice A's
own `plan_review`** before code. This note records the design and the
convergence so the implementer carries the constraints in.
Date: 2026-06-27. Context: #94 (umbrella), #91 (the `base_branch` pin this design
extends), #92 (hard ceilings, feeds Slice C). Supersedes nothing.

## Problem

A "goal mode" above the per-task pipeline: one end-goal → decompose into tasks →
run each through the existing `plan → implement → review → draft PR` pipeline →
accumulate a **stack of draft PRs**. Must preserve redteam's identity:
**draft PR = the human checkpoint; never auto-merge.**

## Engine facts this design is grounded in (verified, with Codex)

- Batch = dir with `tasks/task-NNN-slug/`. Discovery = **sorted directory
  iteration**, no manifest, **no inter-task deps** (`orchestrator.py:1327-1362`).
- Every task branches from **live config base independently**:
  `process_task` calls `_ensure_task_branch(..., cfg.project.base_branch)` at
  `orchestrator.py:833`, and only THEN pins `state["base_branch"] =
  cfg.project.base_branch` at `orchestrator.py:857-868`. So today the branch
  checkout base and the pin are the same value, but **branch creation does not
  read the pin** — it reads live config. (Codex PR-001.)
- `_ensure_task_branch` (`orchestrator.py:736`) creates the task branch from the
  requested base **only if the branch does not already exist**; otherwise it just
  checks out the existing branch (`orchestrator.py:784-800`) — it does not verify
  the existing branch descends from the requested base. (Codex PR-003.)
- The pin is a **single string** `state["base_branch"]`; `pinned_base_branch()`
  (`_base.py:325-338`) returns one value. Consumers that read it: review range
  (`review_code.py:43`), implement diff/changed-paths (`implement.py:271`), the
  TDD `write_test` path (`write_test.py:102/127/142/171`), the tier-downgrade
  guard (`orchestrator.py:1119`), and the **PR base** (`create_pr.py:167` →
  `gh pr create --base`). All per-task pipeline consumers honor the pin. The
  standalone `review` command and `cmd_review` still use live config — they are
  NOT per-task pipeline consumers, so out of scope here.
- `done` == `next_phase == "done"` (`orchestrator.py:963-966`); afterward
  `process_task` skips branch setup (`orchestrator.py:827-830`). `done` does NOT
  make the branch immutable. (Codex PR-004.)
- No auto-merge anywhere. Tiers set `review`/`gates`/`models` only.

## Core resolution: PR stacking over a SINGLE-PARENT forest, no auto-merge

Chaining is solved by **PR stacking**, not auto-merge:

- A dependent task B pins `state["base_branch"]` to its **parent task A's branch**
  (`redteam/task-001-...`) instead of config base. Then B's reviewed range, its
  PR base, its changed-paths all become `A-branch...HEAD` = **exactly B's delta**.
- The human merges the stack bottom-up. **No auto-merge ever needed.** Trust
  model preserved.

**v1 constraint — each task has AT MOST ONE dependency (single parent).** The
DAG is therefore a **forest of chains**: independent roots run in parallel (as
today); each non-root has exactly one parent and stacks on it. This maps 1:1 to
the single-string `state["base_branch"]` pin. A task declaring **≥2 dependencies
fails the manifest validation closed** — multi-parent merge (an integration
branch that merges several upstream branches, then pins the dependent to it) is
explicitly **out of scope for v1** and recorded as future work. (Codex PR-002.)

This is strictly more than pure-linear (parallel roots/chains) while staying
within the one-base-branch model.

## The three pieces

### Piece 1 — single-parent DAG + task-on-task branching (engine gap) — SLICE A

New, optional **batch manifest** `goal.json` at batch root:

```json
{
  "goal": "one-line goal",
  "ceilings": { "max_tasks": 8 },
  "tasks": {
    "task-001-foo": { "depends_on": [] },
    "task-002-bar": { "depends_on": ["task-001-foo"] }
  }
}
```

**Backward compatible:** absent `goal.json`, `process_batch` behaves exactly as
today (flat, independent, sorted iteration). Goal mode is purely additive.

**(1a) Manifest validation as a hard invariant, before any state seeding or
writable phase** (Codex PR-006). On a batch that HAS `goal.json`, validate the
whole manifest up front; **abort the entire goal batch (fail closed)** — seed no
state, run no task — if any of:
- unknown task ref in any `depends_on` (a ref not present as a task dir / key),
- self-dependency,
- duplicate task IDs,
- a task name that doesn't match the on-disk task dir,
- **≥2 entries in any `depends_on`** (v1 single-parent rule),
- a cycle (toposort over single-parent edges; a back-edge → abort).
Stdlib only (json + a trivial toposort). This runs in `process_batch` before the
task loop.

**(1b) Dependency-aware layered scheduler.** Run in topo layers: a task is
runnable only when its parent (if any) is `done`. Independent roots run as today.
If a parent is deferred/failed/blocked, its descendants are **skipped as
`blocked_on_dependency`** (not run), and goal mode continues with other chains
(Codex PR-004 freeze interaction — a non-done parent never gets a dependent
pinned to it).

**(1c) Pin BEFORE branch, and branch FROM the pin** (Codex PR-001). Reorder
`process_task` branch setup so the dependent task's base is established before
checkout:
1. Resolve the task's base: parent's branch (`<branch_prefix>/<parent_id>`) for a
   dependent task, else `cfg.project.base_branch`. The scheduler passes this in
   (e.g. via the manifest-derived parent, looked up in `process_batch`), so
   `process_task` does not itself re-read the manifest.
2. Set `state["base_branch"]` = that resolved base **first** (still "pin once":
   only when absent; legacy-unpinned-with-writable-phase still fails closed as
   today).
3. Call `_ensure_task_branch(..., base_branch=pinned)` using the pinned value,
   not `cfg.project.base_branch`.
This keeps a single pin authoritative for checkout AND review-range AND PR base.

**(1d) Ancestry fail-closed on reused branches** (Codex PR-003). Before running a
dependent task whose branch already exists, verify `merge-base --is-ancestor
<pinned-base> <task-branch>`. If the existing task branch does NOT descend from
the pinned parent branch (e.g. a stale `redteam/task-B` from a prior flat run off
`main`), **fail closed** (defer with a clear reason) rather than silently
producing a stacked PR whose head doesn't actually contain the parent. No
auto-deletion of the consumer's branch (data-safety).

**(1e) Freeze detection via recorded parent tip — centralized at the read path**
(Codex PR-004, rounds 1→3). At pin time record `state["base_branch_sha"]` = the
parent branch tip SHA. A moved parent tip after pin must fail closed with
"upstream reworked after dependent pinned — re-plan the stack."

The round-1 draft ran this check only before *writable* worker phases; the
round-2 draft listed four consumer call sites. **Both under-apply for the same
reason — the pin has MORE readers than any hand-written inventory** (Codex
round-3 PR-004): `review_code.py:43`, `create_pr.py:167`, the tier guard
`orchestrator.py:1119`, `implement.py:271`, AND the TDD path
`write_test.py:102/127/142/171`, plus any future reader. A four-call-site plan
silently misses `write_test` and the next consumer added.

**Resolution — centralize the guard at the single read path, not the call
sites.** The freeze check lives INSIDE `pinned_base_branch(state, repo)` itself
(`_base.py:325`): every reader already funnels through that one accessor, so
making the accessor verify `state["base_branch_sha"]` against the live parent tip
means **every** consumer — present and future, TDD and agent-pair — pays the
check automatically and none can forget it. The accessor fails closed (raises) if
the tip moved. `git rev-parse` is cheap and the pin is read a handful of times
per phase, not in a loop. (This threads the `repo` path into `pinned_base_branch`;
callers already have it.) For root tasks the base is config base, no SHA is
recorded, and the guard is a no-op (root behavior byte-for-byte unchanged).

**(1f) Push/pull semantics — skip remote pull for parent task branches** (Codex
PR-008, rounds 1→3). v1 treats **local branch state as authoritative** for
stacking: a dependent branches from the parent's LOCAL branch, and ancestry/SHA
checks are local.

The existing `git pull --ff-only origin <base>` (`orchestrator.py:777`) is
**unconditional** after checkout, so if a remote `redteam/task-001` exists and
has *moved*, the ff-only pull mutates the local parent — contradicting "local
authoritative" and shifting the very SHA that §1e froze. So the pull must be
**guarded**: skip it when the resolved base is a parent task branch, keep it only
for a real remote config base. **Judge the base on an explicit signal, not a
prefix string** (Codex round-3): do NOT test
`base_branch.startswith(f"{branch_prefix}/")` — a project's real remote config
base could legitimately live under the same prefix, and a loose match would
wrongly suppress its pull. The scheduler already knows whether a task is a
dependent (it resolved the parent), so pass an explicit `base_is_parent` flag (or
check membership in the manifest-derived parent-branch set) into the checkout step
and gate the pull on that. Goal mode does not require pushing parent branches
before dependents run; `create_pr` pushes each branch at PR time as today.

Slice A is security-adjacent (touches the #91 pin + branching) → **its own
plan_review before code.** Its tests must assert the real invariants, not just
manifest parsing (Codex PR-005):
- pinned base (parent branch) is what `_ensure_task_branch` checks out from;
- a pre-existing wrong-base task branch fails closed (1d);
- a moved parent tip fails closed (1e) — specifically a tip that moves AFTER
  implement, before `review_code` AND before `create_pr`, fails closed at each
  consumer (the guard lives at the read path, so this holds for every phase);
- because the guard lives in `pinned_base_branch`, the TDD `write_test` consumer
  (`write_test.py:102/127/142/171`) and any future reader fail closed on a moved
  tip too — assert via a `write_test`-path test, not just agent-pair phases;
- a remote parent-task branch that has moved does NOT mutate the local parent
  (the ff-only pull is skipped for parent branches — 1f);
- `create_pr` receives the parent branch as `--base` (assert the prompt/base);
- review range resolves to `parent...HEAD`;
- manifest validation aborts the whole batch on cycle / unknown ref / self-dep /
  dup / ≥2 deps, seeding NO state;
- absent `goal.json` → behavior byte-for-byte unchanged (backward-compat test).

### Piece 2 — goal→task decomposer — SLICE B

A planning agent: `goal.md` → an ordered set of clean-boundary `input.md` briefs
+ the `goal.json` (single-parent) manifest that Slice A consumes. Plus a
**decomposition review**: the manifest itself goes through a cross-provider
adversarial review before any task runs (mirrors `plan_review` one level up). A
bad decomposition is the highest-risk failure mode. Prompt-heavy; depends on A.
The decomposer must emit a single-parent manifest (it cannot express multi-parent
in v1; if a natural decomposition needs it, it serializes into a chain or stops
and asks).

### Piece 3 — goal-level done criterion + ceilings — SLICE C (ships WITH B)

- Hard ceilings (`max_tasks`; token/wall-clock optional) enforced by the
  scheduler so an autonomous loop can't expand unboundedly. Ties into #92.
- Goal-complete = all manifest tasks `done` (draft-PR stack complete) — NOT
  merged (merge stays human).
- Ceilings are load-bearing only once B exists (A hand-feeds a fixed manifest),
  so C ships before/with B, never after.

## Open decisions — settled (post-review)

1. **Auto-merge** → **none. PR stacking; human merges the stack.**
2. **DAG adversarially reviewed before tasks run?** → **Yes** (Slice B).
3. **Failure handling** → **skip-and-continue across chains; block (don't run)
   descendants of a non-done parent; no auto re-plan in v1** (human fixes/merges,
   then `resume`). Surface the partial stack.
4. **Where the goal lives** → `goal.md` (human goal) + `goal.json` (manifest:
   single-parent DAG + ceilings) at batch root; per-task `state["base_branch"]`
   (+ `base_branch_sha`) carries the stacking pin.
5. **DAG shape v1** → **single-parent forest**; ≥2 deps fails closed; multi-parent
   integration branch is future work.

## Slicing (don't land in one PR)

- **Slice A** (engine gap): manifest schema + up-front validation/abort +
  layered scheduler + pin-before-branch + ancestry & freeze fail-closed (guard
  centralized in `pinned_base_branch`) + parent-aware pull skip + stacked PR base.
  Testable with hand-written manifests AND invariant assertions. **`plan_review`
  first.**
- **Slice C** (ceilings + goal done-criterion).
- **Slice B** (decomposer + decomposition review). Must not ship without C.

Order: **A → C → B**.

## plan_review convergence (Claude ↔ Codex, 2026-06-27)

Three rounds, all CHANGES_REQUESTED until the design stabilized; recorded here as
the adversarial trail.

**Round 1** — 2 blockers + 4 major:
- **PR-001 (blocker)** → §1c: pin first, branch from the pin.
- **PR-002 (blocker)** → single-parent forest; ≥2 deps fails closed; multi-parent
  = future work.
- **PR-003 (major)** → §1d: `merge-base --is-ancestor` fail-closed on reused
  branches; no auto-delete.
- **PR-004 (major)** → §1e: record parent tip SHA; fail closed if moved; §1b:
  descendants of a non-done parent are never run.
- **PR-005 (major)** → Slice A invariant test list (not just manifest parsing).
- **PR-006 (major)** → §1a: whole-manifest validation as a hard invariant,
  aborting the entire goal batch before any seeding.
- **PR-007 (minor)** → operator docs: stacked-PR maintenance is manual in v1; no
  GitHub auto-retarget reliance.
- **PR-008 (minor)** → §1f: local branch state authoritative.

**Round 2** — resolved PR-001/003/006/007; PR-004/008/005/002 partial:
- **PR-004** → generalized the freeze guard beyond writable phases.
- **PR-008** → corrected "harmless pull" → the unconditional ff-only pull can
  mutate a moved local parent; must be guarded.
- **PR-005** → added the moved-tip-between-implement-and-review/PR test.
- **PR-002** → Codex couldn't fetch #94 (api.github.com network error) to confirm
  multi-parent need; operator confirms v1 single-parent scope. No design change.

**Round 3** — resolved PR-008/005; PR-004 closed:
- **PR-004** → an enumerated call-site list under-applies (misses `write_test`,
  `implement`, future readers). **Centralize the guard inside
  `pinned_base_branch`** — Codex confirmed the centralized-accessor shape over the
  four-call-site plan.
- **PR-008** → judge the base on an explicit `base_is_parent` signal / manifest
  parent-set membership, NOT a `branch_prefix` string match.
- **PR-005** → also assert the TDD `write_test` path fails closed (follows for
  free from the centralized guard).

The two impl-level notes — centralize-the-guard, explicit-parent-signal — are
re-verified against real code in **Slice A's own `plan_review`**.
