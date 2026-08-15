# Task 2 — Tighten the "would have failed before" Required Check (#159 + #161)

## Depends on

Task 1 (`task-001-wire-conventions-into-agent-pair`). This task stacks on
task 1's branch. The order is deliberate: the prompt rewrite goes last so it
changes the criteria for as few reviews as possible.

## Problem

The Required Check paragraph in `.redteam/prompts/codex/code_review.md`
currently reads:

> For any new test added in the diff, briefly justify that the test would have
> failed against the pre-change code. If you cannot justify that, flag it as
> `severity:major`.

This one paragraph fails in **two** directions:

1. **#159 — it passes trivially.** A *source-text guard* (open the file under
   test, assert `toContain` on its contents) fails against pre-change code for
   a trivial reason — the string is new — while never importing, mounting, or
   executing the module. It is the cheapest test to write and it satisfies the
   rule by construction, so the pipeline drifts toward it. A consumer repo
   shipped a component that never mounted; all three of its "tests" only read
   the source as text.
2. **#161 — it can never pass.** A *preventive suite* (smoke /
   characterization, added with no product change) is green by construction.
   Demanding it fail against the pre-change code demands the repo already be
   broken, so the reviewer returns `CHANGES_REQUESTED` indefinitely and the
   only exits are deleting the suite or overriding the gate.

Both edit the same paragraph — this is one revision, a **tightening in both
directions**, not a loosening.

## Change

Rewrite the Required Check paragraph in
`.redteam/prompts/codex/code_review.md` so it says all of the following
(shape / order is a planner decision; the *semantic clauses* are load-bearing
and must appear):

### Clause A — name the source-text escape hatch as not satisfying the rule (#159)

A test that only asserts on the **source text** of the thing under test — e.g.
opening the file and asserting substrings/regex against its contents — does
not satisfy the rule when the thing under test *has an in-repo execution path*
and the test goes around it. The vacuity is in the **bypass**, not in the
assertion. Flag such tests `severity:major`.

### Clause B — preventive suites have a stricter criterion (#161)

A preventive suite (smoke / characterization, added with no product change)
satisfies the rule by an **executable demonstration that the suite detects the
failure it claims to detect**: a deliberately broken fixture, exercised
through the **same code path** the suite claims to protect, in the same
file, asserted to fail. A fixture that fails for some unrelated contrived
reason does not qualify — that is the same vacuousness as Clause A in a new
costume. "Same code path" is load-bearing; the prompt must say what it is
for: the fixture must break **the behaviour the suite claims to protect**.

### Clause C — the no-in-repo-execution-path exemption, narrowly scoped

Where the artifact under test **has no in-repo execution path** — nothing in
the repo parses, interprets, imports, executes, or otherwise acts on its
contents; consumers only pass or embed its *path* — Clause A's "bypass"
condition cannot arise, so pinning specific semantic clauses of the file's
text is the only assertion available and is accepted.

The exemption is scoped by these obligations, all of which must be stated in
the prompt so it cannot be used as #159's hole reopened:

- **Eligibility is per artifact, never by file class.** A glob, a directory,
  or a file extension is not an argument. Eligibility must be *established*
  for the specific artifact — by naming that artifact's in-repo consumers and
  demonstrating none of them parse or interpret its contents. Configuration,
  templates, manifests, and workflow definitions are non-importable yet are
  loaded and acted on by code, so their semantics *are* reachable
  behaviourally; if any in-repo path can exercise the clause, that path must
  be tested and the exemption does not apply. "Not importable" is far too
  weak.
- **Semantic clauses only.** Under the exemption the assertion must pin a
  **semantic clause the change requires**, never incidental wording,
  formatting, ordering, or whitespace.
- **Where an execution path does exist, it must be preferred.** Assertions on
  the assembled/observed behaviour (e.g. a built prompt string a runner
  actually hands to another process) come before file-text pinning wherever
  the claim can be expressed that way.

### Non-goals for the rewrite

- Do **not** introduce a new `REVIEW_DECISION` value or change the exit-code
  contract.
- Do **not** add a second reviewer pass or a new review phase.
- Do **not** add a mechanism by which a project-owned file (e.g. `project-context.md`)
  can override a prompt-level Required Check — #161 documented that inversion
  as correctly refused by the reviewer; adding such a mechanism is one of the
  two undelegated stop conditions.
- Do **not** weaken the rule's core intent ("no test that verifies nothing")
  beyond the enumerated tightening above — the other undelegated stop.

## Also change (templates seed for new consumers, #159 item 3)

Add a short **"Runtime coverage"** section (heading exactly) to
`.redteam/templates/docs/test-conventions.md`. It should tell new consumers,
in a few lines, that tests must exercise the code path they claim to protect
— importing, mounting, or executing the thing under test — and that
source-text guards (opening a file and asserting on its contents) don't count
when an execution path exists. Match the existing template's tone (concise,
declarative, project-agnostic). Do not touch `.redteam/docs/test-conventions.md`
(that is the dogfood copy already updated by task 1).

## Self-review hazard (this run edits the rule that reviews this run)

`review_code.py` hands the reviewer a *path* to
`.redteam/prompts/codex/code_review.md`, not inlined text, so the reviewer
reads the working-tree copy at review time. This task's own tests are
reviewed under the rule this task rewrites. Do not leave the prompt in a
state that cannot return `APPROVED` (that failure mode is #103 — do not
reintroduce it).

Prior decompositions of this goal failed the review gate the same way:
**claiming the exemption instead of establishing it**. One asserted it in a
docstring; another asserted it by file class. Both are refused.

This brief establishes eligibility for the one artifact this task edits —
`.redteam/prompts/codex/code_review.md` — by auditing its actual in-repo
consumers.

### Audit of `.redteam/prompts/codex/code_review.md` (repo-wide, full-path)

Two cautions matter:

- **Match on the full path, not the basename.** The bare basename
  `code_review.md` names two unrelated files here: the criteria prompt
  above, and the per-task review artifact at `<task_dir>/code_review.md`. A
  bare `grep code_review.md` returns `implement.py`, `create_pr.py`, and
  `review_code.py`, which reads as three consumers of the prompt — but those
  are all the task artifact, not the criteria file. Grep for the full path.
- **Search the whole repo, not just `phase_runners/`.** An earlier audit
  scoped to that directory and so missed the two `orchestrator.py` sites.

The complete audit of `.redteam/prompts/codex/code_review.md`, repo-wide, is
**four** references — all of which pass or embed the file's *path*, none of
which open, parse, or interpret its contents:

- `review_code.py:42` — `_code_review_prompt`; embeds the path in the built
  reviewer prompt as the criteria to apply.
- `review_code.py:116` — `_narrowed_code_review_prompt`; same, for
  round-over-round narrowed review.
- `orchestrator.py:2133` — the same, for `orchestrator review` (standalone).
- `orchestrator.py:452` — `_set_next_action_for_manual_phase`'s `prompt_map`;
  embeds the path in a human-readable instruction for the manual fallback.

The implementer must **re-run the audit** (grep for the full path, repo-wide,
excluding `__pycache__`), confirm no consumer opens or parses the file, and
record the result in `outcome.md` so the reviewer can see it. If a fifth
consumer exists and *does* parse the file, the exemption does not apply and
the plan must be revised (stop and use `ask_user_response.md`).

Do **not** generalise the exemption to any other prompt, file class, or
directory. Only `.redteam/prompts/codex/code_review.md` is audited here;
whether any other prompt would qualify is unknown, and neither the rewritten
rule nor the tests may imply an answer either way.

## Tests

Route eligibility through the **rewritten rule's own no-execution-path clause**
(Clause C above). The tests are legitimate *because the rule says so*, not
because a docstring asserts it — do not write a docstring claiming these
tests "are not source-text guards"; that is exactly the failure mode the
prior decompositions hit.

Test the **built prompt** wherever the claim can be expressed against
`review_code.py`'s assembled string; fall back to pinning the markdown only
for semantic clauses that exist nowhere but the file itself.

Concretely, add tests (in `.redteam/tests/`, following the file-per-concern
convention already in use) that pin the following semantic clauses. Prefer
substring / `in` assertions over full-string snapshots.

Against the **built agent-pair reviewer prompt** (assembled by
`review_code._code_review_prompt` and `review_code._narrowed_code_review_prompt`):

- The prompt still names `.redteam/prompts/codex/code_review.md` as the
  criteria the reviewer is to apply. (Regression: the reviewer must still be
  pointed at the rule.)
- The prompt still ends its instructions with the enumerated
  `REVIEW_DECISION:` vocabulary the runner already expects (no new values,
  no removals).

Against `.redteam/prompts/codex/code_review.md` (markdown-only assertions,
because these semantic clauses exist nowhere but the file — Clause C
applies):

- The rule paragraph contains a clause naming the **source-text /
  bypass** failure (Clause A). Assert on a small stable semantic phrase, not
  on cosmetic wording — e.g. that both the notion of "source text" and the
  notion of a "bypass" or "execution path" appear together in that
  paragraph.
- The rule paragraph contains a clause carving out **preventive suites** and
  requiring an **executable demonstration** with a **broken fixture** through
  the **same code path** (Clause B). Assert on the semantic phrases
  ("preventive", "same code path", "fixture" or the equivalent load-bearing
  words the planner chooses), not on whole sentences.
- The rule paragraph contains the **no-in-repo-execution-path exemption**
  and its **per-artifact eligibility** obligation (Clause C). Assert on the
  load-bearing phrases (e.g. "per artifact", "no in-repo execution path" or
  the equivalent semantic language).
- The rule paragraph forbids project-owned files overriding the check
  (regression against #161's incorrect inversion).
- The four `REVIEW_DECISION:` values are still enumerated at the tail
  (regression against #103 — the file must remain in a state that can return
  `APPROVED`).

Against `.redteam/templates/docs/test-conventions.md`:

- The template now contains a **"Runtime coverage"** section that names
  execution / mounting / importing as required and calls out source-text
  guards as insufficient when an execution path exists.

Every test docstring should state which Done-when item and which semantic
clause it pins, so the reviewer can trace the coverage.

`.redteam/tests/test_agents_generic_prompts.py` must stay green (no stack
fingerprints in agent bodies).

## Hard constraints inherited from the goal

- **Tighten, never loosen.** Clause C is scoping, not an exemption from the
  rule's intent, and it carries the obligations spelled out above. Any change
  that makes it easier for a vacuous test to pass is out of scope and a stop
  condition.
- **No project-owned override of a prompt-level Required Check.** Do not
  reintroduce that inversion; if a plan would require it, stop and use
  `ask_user_response.md`.
- **Engine stays project-agnostic.** No project- or stack-specific
  fingerprints in `.redteam/workflows/` or non-example tests.
- **Stdlib only, zero runtime deps.**
- Do not touch `.redteam/config.toml`, the installer's ownership split, the
  verification allowlist, or any batch state.
- Do not touch `.redteam/workflows/*` (that was task 1's surface) except as
  transitively required to preserve the built-prompt tests; if a workflow
  edit seems necessary, stop and use `ask_user_response.md`.
- Do not touch `.redteam/docs/test-conventions.md` (task 1's project-owned
  fix). Only the templates copy grows a new section here.

## Operator delegation

Plan-level scope questions are delegated to the operator agent — prefer the
narrowest change; record decisions in `ask_user_response.md` or the final
report. The two undelegated stops are:

- weakening the "would have failed before" rule in any direction beyond the
  enumerated tightening above;
- adding a mechanism that lets a project-owned file override a prompt-level
  Required Check.

## Affected files (pin exactly this scope)

- `.redteam/prompts/codex/code_review.md` — rewritten Required Check
  paragraph (Clauses A, B, C).
- `.redteam/templates/docs/test-conventions.md` — new "Runtime coverage"
  section for new consumers.
- `.redteam/tests/` — one or more new/extended test files as described above.

Nothing else.

## `outcome.md` shape (planner: read carefully)

`outcome.md` MUST include a parseable `## Verification` section with a fenced
` ```yaml ` block invoking the project verify command, e.g.:

```yaml
verify:
  - bash .redteam/scripts/verify.sh
```

Prose "Verification hooks" does **not** parse (this is the #138 pitfall — the
planner previously emitted a prose section that the parser ignored). Use the
YAML-in-fence form.

## Known stacked-run hazard (out of scope; do not "fix" here)

Because this task stacks on task 1, task 1's `pr_url.txt` may already exist
in its sibling task dir when task 2 dispatches. The pre-worker sibling floor's
basename allowlist is `{state.json, outcome.md, pr.md, input.md}` and does
**not** yet include `pr_url.txt` (open issue #158), so it can fail closed on
that one artifact. If it fires, the operator recovers by **stashing** that
one generated file and resuming — do **not** commit it and do **not** patch
around it inside this task. `pr_url.txt` is `create_pr` output, not product
work; committing it would carry unrelated batch state into the stacked
branch and its PR, which the goal-level Hard constraints forbid. If stashing
does not clear it, stop and treat #158 separately.

## Done-when

- The Required Check paragraph in `.redteam/prompts/codex/code_review.md`
  contains Clauses A, B, and C with the load-bearing phrasing above, and the
  file still enumerates all four `REVIEW_DECISION:` values (i.e. can still
  return `APPROVED`).
- `.redteam/templates/docs/test-conventions.md` contains a "Runtime
  coverage" section for new consumers.
- Built-prompt tests pin the reviewer-facing surface (criteria file still
  named, decision vocabulary unchanged); markdown tests pin the semantic
  clauses that exist only in the file.
- The audit of `.redteam/prompts/codex/code_review.md`'s consumers is
  re-run, confirmed to be the four references named above, and recorded in
  `outcome.md`.
- `bash .redteam/scripts/verify.sh` passes (ruff + pytest over `.redteam/`).
- `test_agents_generic_prompts.py` is green.
- The reviewer of *this* task, reading the rewritten rule, can return
  `APPROVED` — the criterion the tests satisfy (Clause C, per-artifact
  eligibility established by the audit) is one the rewritten rule accepts by
  its own wording.
