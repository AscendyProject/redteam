# Codex Prompt: Agent-Pair Rescue

You are taking over implementation because Claude could not resolve blocker review feedback.

## Inputs

- `input.md`
- `outcome.md`
- `plan_review.md`
- `code_review.md`
- `state.json`
- `verification.log`
- `impl_diff.patch`
- relevant repo files

## Output

Edit the code directly, write `rescue_report.md`, then create `rescue.done`.

## Scope discipline

During this review/rescue, do **not** modify:
- `AGENTS.md` (your operating guide)
- `.redteam/prompts/codex/*` (any review prompt, including this one)
- Any other task's `task_dir` artifacts

If a change to any of these seems necessary, write the proposed change
into your review file under a "Proposed harness adjustments" section and
emit `REVIEW_DECISION: ASK_USER`. The user adjudicates whether to spin
out a separate harness task.

## Rules

- Stay within the approved plan unless the user explicitly expanded scope.
- Fix only the blocker issues that triggered rescue.
- Run the verification commands from `outcome.md`.
- Update `impl_diff.patch` with the final diff.

## Rescue Report Sections

```md
# Rescue Report

## Why Rescue Was Triggered

## What Claude Missed

## Changes Codex Made

## Verification

## Follow-up Advice For Claude
```
