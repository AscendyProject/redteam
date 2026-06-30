---
description: Run redteam goal mode — decompose a human-authored goal.md into a single-parent task DAG (goal.json + per-task input.md briefs) via the goal-decomposer + cross-provider decomposition review, then run the tasks parent-first with each dependent stacked on its parent's branch. Use for a multi-step goal you want broken into a reviewed, stacked chain of draft PRs.
---

Drive redteam **goal mode**: turn one human-authored `goal.md` into a reviewed,
single-parent task DAG and run it parent-first, each dependent stacked on its
parent's branch (one draft PR per task — the human checkpoint stays per task).

Steps:

1. Confirm the harness is vendored — `.redteam/workflows/orchestrator.py` must
   exist. If not, tell the user to run `/redteam:install` first.
2. Determine the batch directory:
   - If the user passed one in `$ARGUMENTS`, use it.
   - Otherwise list the directories under `.redteam/batches/`. If there's exactly
     one without a `goal.json`, use it; if there are several (or none), ask the
     user which one to use (or to create one).
3. Ensure a `goal.md` exists at the batch root (`<batch>/goal.md`). This is the
   **human's intent** — the one-paragraph-or-so description of the overall goal,
   plus any hard constraints and non-goals. If it's missing, tell the user to
   write it; do not write it for them unless they ask (same rule as `input.md`:
   `goal.md` is the human's intent, the decomposer turns it into the manifest +
   per-task briefs).
4. Run the decomposition (it does NOT dispatch any task — it only generates and
   reviews the plan, fail-closed):

   ```bash
   python3 .redteam/workflows/orchestrator.py decompose .redteam/batches/<batch>
   ```

   On success it writes `<batch>/goal.json` (single-parent DAG + `ceilings.max_tasks`)
   and one `tasks/<id>/input.md` per task, after a cross-provider decomposition
   review gate. It fails closed if: a `goal.json` or any `tasks/<id>/input.md`
   already exists (idempotency — remove generated artifacts to re-run), the
   decomposer signals it cannot decompose (surfaces `decompose_blocked.md`), the
   review is not APPROVED (persists `decompose_review.md`), or the manifest is
   invalid (e.g. a task with ≥2 parents — v1 is a single-parent forest;
   `max_tasks` must equal the task count).
5. If decompose exits non-zero, read the surfaced `decompose_blocked.md` /
   `decompose_review.md` and relay why it stopped; do not run `start`.
6. On success, run the validated stack parent-first:

   ```bash
   python3 .redteam/workflows/orchestrator.py start .redteam/batches/<batch>
   ```

   Each task runs through the normal pipeline and opens its own draft PR; a
   dependent's reviewed range / PR base / changed-paths are pinned to its parent's
   branch. Use `/redteam:status` (or `orchestrator status <batch>`) to track, and
   `orchestrator resume <batch>` to continue.

Do not write the goal brief for the user, and do not merge any PR — each task's
draft PR is the human checkpoint, same as the agent-pair flow.
