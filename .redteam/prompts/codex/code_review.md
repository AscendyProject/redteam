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

The harness invokes this review two ways, and the output channel differs:

- **Headless adapter (default — read-only sandbox):** the harness captures your
  review from **stdout** and persists it itself. Output the entire review to
  stdout and do **not** write any file or create any `.done` sentinel — file
  writes are impossible under the read-only sandbox and are not needed. This is
  the default; the harness prompt that reaches you also states it.
- **Manual fallback only** (a human or agent running Codex by hand, outside the
  read-only adapter): overwrite `code_review.md` in the task directory, then
  create `code_review.done`. Do not append to an existing review file.

Either way, end with exactly one `REVIEW_DECISION:` line.

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
- **Plan fidelity (task-scoped).** Open the approved `outcome.md` and locate its Done-when checklist. Verify **each Done-when item individually** against the diff and the repository state — "the implementation broadly matches the plan" is not a verification. In the review body, adjudicate every item on its own line as met or unmet, the same way carried-over findings are adjudicated in a narrowed round. An unmet Done-when item is `severity:major`. If `outcome.md` has no locatable Done-when list, say so explicitly and judge against its Goal statement instead — never silently skip this check.
- Check for missed acceptance criteria beyond the Done-when list, regressions, unsafe changes, unrelated churn, and missing tests.
- For any new test added in the diff, justify that the test would have failed against the pre-change code. If you cannot justify that, flag it as `severity:major`. This rule has three tightening refinements; no project-owned file (e.g. `project-context.md`, `test-conventions.md`) may override or weaken any of the following:

  **Clause A — source-text bypass (`severity:major`).** A test that asserts only on the *source text* of the thing under test — opening the file and checking substrings or regexes against its contents — does not satisfy this rule when the thing under test has an in-repo execution path and the test goes around it. The vacuity is in the *bypass* of that execution path, not in the assertion itself. Flag such tests `severity:major`.

  **Clause B — preventive suites (smoke / characterization).** A *preventive suite* — one added with no product change, green by construction — cannot fail against the pre-change code. Important: a test that exercises the code path by calling an actual function and asserting on its assembled output is *not preventive*, even if the output existed before this diff; such a test exercises the code path and is justified by showing it would detect an incorrect implementation. A genuinely preventive suite satisfies this rule only by an executable demonstration: a deliberately broken *fixture* in the *same file* as the suite, exercised through the *same code path* the suite claims to protect, asserted to fail where the fixture breaks *the behaviour the suite claims to protect* — not an unrelated contrived reason. A fixture that fails for an unrelated contrived reason does not qualify; that is the same vacuousness as Clause A in a new costume.

  **Clause C — no-in-repo-execution-path exemption (per artifact, narrowly scoped).** Where the artifact under test has *no in-repo execution path* — nothing in the repo parses, interprets, imports, executes, or otherwise acts on its contents; consumers only pass or embed its path as a string — Clause A's bypass condition cannot arise, so pinning specific semantic clauses of the file's text is the only assertion available and is accepted. This exemption carries the following obligations:

  - *Eligibility is per artifact, never by file class, glob, directory, or extension.* Eligibility must be *established* for the specific artifact by naming that artifact's in-repo consumers and demonstrating that none of them parse or interpret its contents. Configuration files, templates, manifests, and workflow definitions are non-importable yet are often loaded and acted on by code — their semantics are behaviourally reachable — so they are not automatically eligible; "not importable" is too weak a test.
  - *Semantic clauses only.* Under this exemption the assertion must pin a semantic clause the change requires, not incidental wording, formatting, ordering, or whitespace.
  - *Where an execution path exists, it must be preferred.* Assertions on assembled or observed behaviour (e.g. a built prompt string a runner actually hands to another process) come before file-text pinning wherever the claim can be expressed that way.
- Output validity (anti-degeneracy): if the change produces a discriminating output — a score, grade, ranking, classification, threshold, or similar — argue from a few realistic inputs whether it *meaningfully discriminates*, or whether it saturates, clusters, or collapses to a near-constant (a result that conforms to the spec yet carries no signal). If it plausibly degenerates, flag it `severity:major`. Conformance to `outcome.md` does NOT excuse a degenerate design — when the spec itself encodes the saturation, say so.

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
