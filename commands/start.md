---
description: Start the redteam pipeline on a batch — dispatch each task through plan → implement → review and open a draft PR per task. Use to run a batch of seeded tasks (after /redteam:new-task) for the first time.
---

Start the redteam pipeline on a batch: run each seeded task through the pipeline
(plan → implement → adversarial review) and open a draft PR per task. The draft
PR is the human checkpoint — nothing is merged.

Steps:

1. Confirm the harness is vendored — `.redteam/workflows/orchestrator.py` must
   exist. If not, tell the user to run `/redteam:install` first.
2. Determine the batch directory:
   - If the user passed one in `$ARGUMENTS`, use it.
   - Otherwise list the directories under `.redteam/batches/`. If there's exactly
     one, use it; if there are several (or none), ask the user which one.
3. Sanity-check the batch has tasks with a filled-in `input.md` (scaffold with
   `/redteam:new-task`, or for goal mode use `/redteam:goal` instead of this
   command — `goal` decomposes then runs the stack for you).
4. Run:

   ```bash
   python3 .redteam/workflows/orchestrator.py start .redteam/batches/<batch>
   ```

5. Summarize the result per task: which phase each reached, any draft PR URL
   (saved in the task's `pr_url.txt`), and any task left at a `[GATE: …]`,
   `[CODEX: …]`, or `[DEFERRED]` marker. For anything incomplete, point the user
   at `/redteam:status` to inspect and `/redteam:resume` to continue.

Notes:
- `start` is for the first run; once a batch is underway use `/redteam:resume`.
- Do not merge any PR — each task's draft PR is the human checkpoint.
