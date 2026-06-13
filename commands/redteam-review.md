---
description: Run the redteam adversarial reviewer (a different model than the one that wrote the code) over the current branch diff, read-only. Use to get a cross-model security/quality review of your local changes without driving the full pipeline.
---

Run a one-shot **cross-model adversarial review** of the current branch's diff.

This is the harness's review gate on its own: the configured reviewer (a
*different* provider than whoever wrote the code — `codex` by default) reads
`git diff <base>...HEAD` read-only and returns a `REVIEW_DECISION`. It does NOT
require a task/batch and does NOT write code.

Steps:

1. Confirm the harness is vendored — `.redteam/workflows/orchestrator.py` must
   exist. If not, tell the user to run `/redteam:redteam-install` first.
2. Run the reviewer:

   ```bash
   python3 .redteam/workflows/orchestrator.py review
   ```

3. The full review (with `IR-NNN` findings) is printed and saved to
   `.redteam/last_review.md`. The exit code is the decision:
   `0` = APPROVED, `1` = changes/rescue/ask requested (issues found),
   `2` = the reviewer itself failed (missing/expired `codex` CLI, timeout, or an
   unparseable result — fail-closed, never treated as an approval).
4. Summarize the findings for the user and, if `CHANGES_REQUESTED`, walk through
   the `IR-NNN` items.

Notes:
- The reviewer provider and its CLI auth come from `.redteam/config.toml`
  `[models].reviewer` (use `/redteam:config` to change it). For a `codex`
  reviewer, the `codex` CLI must be installed and logged in (`codex login`).
- If `reviewer = "human"`, there is no headless reviewer and the command exits
  with guidance instead of running.
