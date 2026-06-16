---
description: Scaffold a new redteam task — create the next task-NNN directory and seed an input.md brief from the template. Use to add a task to a batch without hand-reproducing the brief structure the planner expects.
---

Scaffold a new task in a redteam batch: create the next `task-NNN-<slug>`
directory and seed an `input.md` from the template (the brief the outcome-planner
reads), so the input contract can't be subtly malformed.

Steps:

1. Confirm the harness is vendored — `.redteam/workflows/orchestrator.py` must
   exist. If not, tell the user to run `/redteam:redteam-install` first.
2. Determine the batch directory:
   - If the user passed one in `$ARGUMENTS`, use it.
   - Otherwise list the directories under `.redteam/batches/`. If there's exactly
     one, use it; if there are several (or none), ask the user which one (or to
     create one).
3. Get a short slug (and optional title) for the task from `$ARGUMENTS` or by
   asking the user.
4. Run:

   ```bash
   python3 .redteam/workflows/orchestrator.py new .redteam/batches/<batch> <slug> --title "<title>"
   ```

   It picks the next `task-NNN`, creates the directory, and writes a template
   `input.md`. It never overwrites an existing task directory.
5. Tell the user the created `input.md` path and that they should fill in the
   brief (Goal / What to build / Constraints / Out of scope / Affected files /
   Verification / Risks), then run the orchestrator `start` on the batch.

Do not write the task brief for the user unless they ask — `input.md` is the
human's intent; the planner turns it into `outcome.md`.
