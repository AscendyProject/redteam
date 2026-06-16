# __TITLE__

> Raw task brief for the redteam pipeline. The outcome-planner reads THIS and
> produces a verifiable `outcome.md`. Fill each section in plain language — you are
> describing intent, not writing the plan. Delete this quote block when done.

## Goal
One or two sentences: what should be true when this task is finished, and why.

## What to build
The concrete behavior/change you want. Be specific about the observable result.

## Constraints
Anything the implementer must respect — existing patterns, APIs to reuse, perf or
security requirements, "don't touch X". Omit if none.

## Out of scope
What this task explicitly does NOT cover (forestalls scope creep). List at least one.

## Affected files
The files/areas you expect to change, if you know them. The planner confirms these
and turns them into a budget the implementer can't exceed — so be honest; if you're
unsure, say so here rather than guessing.

## Verification
How "done" is checked: which existing tests/commands must still pass, and what new
behavior should be covered by a test. Describe scope, not exact test names.

## Risks
Anything you're unsure about or had to assume — the human resolves these at the gate.
