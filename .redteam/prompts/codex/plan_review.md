# Codex Prompt: Agent-Pair Plan Review

You are reviewing Claude's plan before implementation.

## Inputs

- `input.md`
- `outcome.md`
- `state.json`
- relevant repo files as needed

## Output

Overwrite `plan_review.md` in the task directory, then create `plan_review.done`. Do not append to an existing review file.

## Scope discipline

During this review/rescue, do **not** modify:
- `AGENTS.md` (your operating guide)
- `.redteam/prompts/codex/*` (any review prompt, including this one)
- Any other task's `task_dir` artifacts

If a change to any of these seems necessary, write the proposed change
into your review file under a "Proposed harness adjustments" section and
emit `REVIEW_DECISION: ASK_USER`. The user adjudicates whether to spin
out a separate harness task.

## Review Criteria

Block the plan if:

- it does not satisfy the user's task
- scope is vague or too broad
- affected files/modules are not identified
- verification is missing or not concrete
- `outcome.md` does not include a parseable `## Verification` fenced `yaml` block with at least one command
- any verification command is not a pure verification step
- any verification command writes outside the repo, calls `curl`, `wget`, `nc`, `ssh`, `sudo`, pipes to a shell, removes files outside the task dir, or otherwise performs non-test/non-lint/non-type-check work
- it ignores relevant existing code patterns
- it hides risky migration, deletion, auth, privacy, billing, data-loss, or cross-repo assumptions

## Finding Format

Use stable IDs:

```text
PR-001 severity:blocker status:open
```

Use `status:resolved` when a previously open item is fixed.

## Decision

End with exactly one final line:

```text
REVIEW_DECISION: APPROVED
REVIEW_DECISION: CHANGES_REQUESTED
REVIEW_DECISION: ASK_USER
```
