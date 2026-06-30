---
description: Show the redteam pipeline status for a batch — per-task phase, next step, gates, and any deferrals — without running anything. Use to check where tasks stand.
---

Report the redteam pipeline status for a batch, read-only (runs no phases).

Steps:

1. Confirm the harness is vendored — `.redteam/workflows/orchestrator.py` must
   exist. If not, tell the user to run `/redteam:install` first.
2. Determine the batch directory:
   - If the user passed one in `$ARGUMENTS`, use it.
   - Otherwise list the directories under `.redteam/batches/`. If there's exactly
     one, use it; if there are several, ask the user which one.
3. Run:

   ```bash
   python3 .redteam/workflows/orchestrator.py status .redteam/batches/<batch>
   ```

4. Summarize for each task: completed phase count, `next` phase, and any
   `[GATE: …]`, `[CODEX: …]`, or `[DEFERRED]` markers. For a task blocked at a
   human gate, tell the user exactly which sentinel to `touch` and to re-run
   `/redteam:install`'s orchestrator `resume` (or the resume command)
   afterward.

Note: `status` never prints `last_failure_log` (a phase's raw stderr can carry
credentials) — only the failure *reason*. Point the user at the task's
`state.json` for the full log if they need it.
