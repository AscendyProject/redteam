---
description: Run redteam goal mode autonomously — decompose a human-authored goal.md into a single-parent task DAG (goal.json + per-task input.md briefs) via the goal-decomposer + cross-provider decomposition review, then DRIVE the batch to completion: run parent-first, diagnose and remediate stuck tasks, and resume until every task's draft PR is open (or a genuinely-human decision is needed). Give it a goal; it runs until done. It never merges — the draft-PR stack stays the human checkpoint.
---

Drive redteam **goal mode** end-to-end: turn one human-authored `goal.md` into a
reviewed, single-parent task DAG, then **act as the operator until the goal is
done** — don't stop at the first `start` invocation. "Done" means every task in
the manifest reached `done` (one draft PR per task, stacked parent-first).
Merging those PRs stays with the human, always.

## Setup

1. Confirm the harness is vendored — `.redteam/workflows/orchestrator.py` must
   exist. If not, tell the user to run `/redteam:install` first.
2. Determine the batch directory:
   - If the user passed one in `$ARGUMENTS`, use it.
   - Otherwise list the directories under `.redteam/batches/`. If there's exactly
     one without a `goal.json`, use it; if there are several (or none), ask the
     user which one to use (or to create one).
3. Ensure a `goal.md` exists at the batch root (`<batch>/goal.md`). This is the
   **human's intent** — the overall goal plus hard constraints and non-goals. If
   it's missing, tell the user to write it; do not write it for them unless they
   ask. Never edit `goal.md` yourself: it is the contract you execute against.
4. If `<batch>/goal.json` already exists, the batch is already decomposed — skip
   to the drive loop. Otherwise run the decomposition (generates + reviews the
   plan; dispatches nothing; fail-closed):

   ```bash
   python3 .redteam/workflows/orchestrator.py decompose .redteam/batches/<batch>
   ```

   If it exits non-zero, read the surfaced `decompose_blocked.md` /
   `decompose_review.md` and relay why it stopped — a rejected decomposition is
   a human decision point, not something to retry around. Stop here.

## Drive loop

Run the stack, then keep operating until the goal is complete:

```bash
python3 .redteam/workflows/orchestrator.py start  .redteam/batches/<batch>   # first pass
python3 .redteam/workflows/orchestrator.py status .redteam/batches/<batch> --json
```

After every pass, read the `--json` status: `goal.complete` is the done
criterion; each task reports `next_phase`, `deferred` entries, and
`last_failure_reason`. Then:

- **`goal.complete: true`** → report the draft-PR stack (each task's `pr_url`,
  parent-first) and stop. Do **not** merge or approve any PR.
- **Tasks incomplete but progressing** (no deferral, no repeated failure) →
  `orchestrator resume` and re-check.
- **A task deferred or erroring** → diagnose before resuming. Read that task's
  `state.json` (`deferred_requirements[].feedback`, `last_failure_log`),
  `progress.md`, and the phase artifacts (`plan_review.md`, `code_review.md`).
  Remediations you may apply yourself:
  - *Transient infra* — expired `codex login`, `gh`/network hiccup: fix it (or
    ask the user to re-login), then resume.
  - *Stale same-named task branch* from an earlier run tripping the branch
    guards: never delete it outright — it may hold unmerged work (the engine
    itself defers rather than deletes for exactly this reason). If its tip is
    already contained in the base branch (`git merge-base --is-ancestor
    <branch> <base>` exits 0), deleting is safe; otherwise **rename it aside**
    (`git branch -m <branch_prefix>/<task-id> <branch_prefix>/<task-id>-stale-1`)
    so the run can recreate the name, list the preserved branch in the final
    report, and resume.
  - *Sticky deferral* — `next_phase: "deferred"` never re-runs on its own. Once
    you've addressed the cause the deferral entry describes, set `next_phase`
    back to the entry's `backtrack_to` (or its `phase`) in `state.json`, leave
    the `deferred_requirements` history intact, and resume.
  - *Defective brief* — if the reviewer's feedback shows the decomposer-written
    `tasks/<id>/input.md` is wrong or ambiguous, fix the brief **within
    goal.md's stated intent and constraints**, then reset the task's
    `next_phase` as above and resume. Record what you changed for the final
    report.
- **A task blocked at an opt-in human gate** (`gate_sentinel` in the status) →
  tell the user which sentinel to `touch`; never touch it yourself unless the
  user already authorized it this session. For `human_gate_pr` specifically,
  offer `orchestrator wait-and-resume` (polls GitHub and advances once the PR
  is merged/closed).

Hard stops — report to the human instead of continuing:

- The same task defers again for the same reason after one remediation
  (two-strike rule — blind re-retrying a fail-closed deferral is prohibited).
- Any fix would loosen a security boundary (verification allowlist, snapshot /
  fail-closed logic, adapter trust model, review gates) — that goes through
  `plan_review`, never an inline patch.
- More than ~10 drive-loop passes without reaching `goal.complete`.

## Final report

Summarize: tasks done vs total, the draft-PR stack (parent-first, with URLs),
any briefs you amended, any deferrals you remediated and how, and anything left
for the human. The draft PRs are the human checkpoint — do not merge them.
