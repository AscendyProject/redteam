---
description: Resume the redteam pipeline on an in-progress batch — pick up tasks left at a gate, failure, or deferral and run their remaining phases. Use after clearing a gate or fixing what blocked a task.
---

Resume an in-progress redteam batch: continue each task from where it stopped
(a human gate, a failed phase, or a deferral) through its remaining phases.

Steps:

1. Confirm the harness is vendored — `.redteam/workflows/orchestrator.py` must
   exist. If not, tell the user to run `/redteam:install` first.
2. Determine the batch directory:
   - If the user passed one in `$ARGUMENTS`, use it.
   - Otherwise list the directories under `.redteam/batches/`. If there's exactly
     one, use it; if there are several, ask the user which one.
3. Check status first so you know what's blocking each task:

   ```bash
   python3 .redteam/workflows/orchestrator.py status .redteam/batches/<batch>
   ```

   - For a task stopped at a `[GATE: …]`, clear it as instructed (e.g. `touch`
     the named sentinel) before resuming.
   - For a task marked `[DEFERRED]`, note that `next_phase: "deferred"` is sticky
     — `resume` skips it until you reset `next_phase` in the task's `state.json`
     to the phase you want to re-run.
4. Run:

   ```bash
   python3 .redteam/workflows/orchestrator.py resume .redteam/batches/<batch>
   ```

5. Summarize what advanced, any new draft PR URLs, and anything still blocked.
   Re-run `resume` after clearing the next gate.

Notes:
- Use `/redteam:start` for the first run of a batch; `resume` is for continuing.
- Do not merge any PR — each task's draft PR is the human checkpoint.
