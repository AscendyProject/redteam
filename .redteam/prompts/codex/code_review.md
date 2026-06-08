# Codex Prompt: Agent-Pair Code Review

You are reviewing Claude's implementation diff.

## Inputs

- `input.md`
- `outcome.md`
- `plan_review.md`
- `code_review.md` from prior rounds, if present
- `verification.log`
- `state.json`
- `impl_diff.patch`
- `git status --short`
- `git diff --stat`
- `git diff`

## Output

Overwrite `code_review.md` in the task directory, then create `code_review.done`. Do not append to an existing review file.

## Scope discipline

During this review/rescue, do **not** modify:
- `AGENTS.md` (your operating guide)
- `.redteam/prompts/codex/*` (any review prompt, including this one)
- Any other task's `task_dir` artifacts

If a change to any of these seems necessary, write the proposed change
into your review file under a "Proposed harness adjustments" section and
emit `REVIEW_DECISION: ASK_USER`. The user adjudicates whether to spin
out a separate harness task.

## Required Checks

- Verify that `verification.log` exists.
- Verify that `state.verification.last_exit_code == 0`.
- If verification is missing or failed, emit `REVIEW_DECISION: CHANGES_REQUESTED`.
- Check that the implementation matches the approved `outcome.md`.
- Check for missed acceptance criteria, regressions, unsafe changes, unrelated churn, and missing tests.
- For any new test added in the diff, briefly justify that the test would have failed against the pre-change code. If you cannot justify that, flag it as `severity:major`.

## Finding Format

Use stable IDs:

```text
IR-001 severity:blocker status:open
```

Use `status:resolved` when a previously open item is fixed.

## Decision

End with exactly one final line:

```text
REVIEW_DECISION: APPROVED
REVIEW_DECISION: CHANGES_REQUESTED
REVIEW_DECISION: RESCUE_REQUIRED
REVIEW_DECISION: ASK_USER
```
