# Goal: make test quality enforceable in agent-pair mode (#160 + #159/#161)

## Intent

Three consumer reports describe one compound failure: in the **default
agent-pair mode** the implementer writes the tests, but nothing tells it what a
real test is, and the reviewer's check for test quality is satisfiable without
executing anything. A consumer repo shipped a component to production that
never mounted — lint, typecheck, cross-model review and all three of its
existing tests passed, because all three only read the source file as text.

Close both halves without weakening the gate.

- **#160 — `test_conventions_file` never reaches agent-pair mode.** The config
  key is referenced by exactly two runners, `write_test.py` and
  `verify_test.py`, and both are TDD-only phases that agent-pair skips. So in
  the mode most consumers run, the project's test conventions are dead config:
  the implementer authors tests with no knowledge of the project's test
  wiring, and the reviewer has no conventions document to judge them against.
  Inject it into the phases that actually write and review tests in agent-pair
  mode (`implement.py`, `review_code.py`), leaving the TDD-phase injection
  as-is.

- **#159 + #161 — one Required Check failing in both directions.** The rule in
  `prompts/codex/code_review.md` is:

  > For any new test added in the diff, briefly justify that the test would have
  > failed against the pre-change code. If you cannot justify that, flag it as
  > `severity:major`.

  1. **#159 — it passes trivially.** A *source-text guard* (read the file under
     test, assert `toContain` on its contents) fails against pre-change code for
     a trivial reason — the string is new — while never importing, mounting or
     executing the module. It is the cheapest test to write and it satisfies the
     rule by construction, so the pipeline drifts toward it.
  2. **#161 — it can never pass.** A *preventive suite* (smoke/characterization,
     added with no product change) is green by construction. Demanding it fail
     against pre-change code demands the repo already be broken, so the reviewer
     returns `CHANGES_REQUESTED` indefinitely and the only exits are deleting
     the suite or overriding the gate.

  Both edit the same paragraph, so they are one revision. The fix is a
  **tightening in both directions**, not a loosening: name the source-text
  escape hatch as not satisfying the rule, and give preventive suites their own
  stricter criterion (an executable demonstration that the suite detects the
  failure it claims to detect — a deliberately broken fixture, in the same file
  and through the same code path, asserted to fail).

  Note #161 also records that encoding the exception in project-owned
  `project-context.md` did **not** work: the reviewer correctly held that a
  prompt-level Required Check outranks a project-level exception. That is why
  this must be fixed in the harness.

## Hard constraints

- **This run modifies the prompt that reviews this run.** `review_code.py`
  hands the reviewer a *path* (`.redteam/prompts/codex/code_review.md`), not
  inlined text, so the reviewer reads the working-tree copy at review time. Any
  task that edits that file changes the criteria applied to every review after
  it, including possibly its own. Treat this as a real hazard: the new
  criterion must be one the task's own tests satisfy, and the prompt must never
  be left in a state that cannot return APPROVED (that failure mode is #103,
  already fixed once — do not reintroduce it).
- **Tighten, never loosen.** The rule's intent is "no test that verifies
  nothing". Neither change may make it easier for a vacuous test to pass; #161's
  carve-out must come with its own stricter obligation, not an exemption.
- **No new config keys, no schema changes.** `test_conventions_file` already
  exists in `config.py`; #160 is a wiring fix, not a config surface change.
- Engine stays project-agnostic (no stack fingerprints in `.redteam/workflows/`
  or non-example tests — keep `test_agents_generic_prompts.py` green),
  stdlib-only, zero runtime deps.
- Behavior for **TDD mode is unchanged and regression-tested**: `write_test.py`
  and `verify_test.py` keep their existing injection.
- Do not touch `.redteam/config.toml`, the installer's ownership split, the
  verification allowlist, or any batch state.

## Operator delegation (autonomy clause)

Plan-level scope questions are delegated to the operator agent: prefer the
**narrowest** change that closes the reported gap, and record each such decision
in `ask_user_response.md` (or the final report) rather than waiting for a human.

Two decisions are **not** delegated and must stop the run:
- weakening the "would have failed before" rule in any direction beyond the
  enumerated tightening above;
- adding a mechanism that lets a project-owned file override a prompt-level
  Required Check (that inversion is what #161 documented as correctly refused).

## Non-goals

- **#162 / #166** — review-verdict non-determinism and standalone-mode
  provenance headers. Same review surface, different concern; #162's
  majority-vote gate is a security boundary and needs its own design decision
  first.
- #133 (plan-aware review), #132, #120, #158.
- No rework of the reviewer adapter, the fallback ladder, or the round-staging.
- No new review phase, no second reviewer pass, no change to the
  `REVIEW_DECISION` vocabulary or the exit-code contract.

## Notes for decomposition

- **Two tasks; the second stacks on the first.**
  1. **Task 1 = #160** — inject `test_conventions_file` into `implement.py` and
     `review_code.py` for agent-pair mode. Mechanical and directly testable.
  2. **Task 2 = #159 + #161** — rewrite the Required Check paragraph in
     `prompts/codex/code_review.md`, plus #159's item 3 (a short "runtime
     coverage" section in `templates/docs/test-conventions.md`, the seed for new
     consumers).

  **Order is deliberate:** the prompt rewrite goes last so it changes the
  criteria for as few reviews as possible. Reversing the order would have task
  1 reviewed under a rule that was rewritten mid-run.

- Task 1 should also fix the now-false first line of this repo's own
  `.redteam/docs/test-conventions.md`, which says the doc is read by "the
  test-author and test-verifier sub-agents" — after #160 the implementer and
  code reviewer read it too. Narrow, factual, project-owned; do not expand it
  into a rewrite of the conventions themselves.

- Each brief MUST tell the planner that `outcome.md` needs a parseable
  `## Verification` section with a fenced ```yaml block containing
  `bash .redteam/scripts/verify.sh` — prose "Verification hooks" does not parse
  (the #138 pitfall).

- Each brief MUST pin Affected files. Task 1: `.redteam/workflows/` +
  `.redteam/tests/` + the one project-owned doc line above. Task 2:
  `.redteam/prompts/codex/code_review.md` + `.redteam/templates/docs/` +
  `.redteam/tests/`. Nothing else.

- **Known stacked-run hazard (#158, still open):** the sibling basename
  allowlist is `{state.json, outcome.md, pr.md, input.md}` and does **not**
  include `pr_url.txt`, so once task 1 opens a PR, task 2's pre-worker floor
  can fail closed on the sibling's `pr_url.txt`. This bit the `benchmark-phase1`
  run. If it fires, that is the known gap and not a task defect — commit or
  stash the file and resume rather than "fixing" it inside a task.

- Prefer testing #160 the way the existing prompt-wiring tests do (assert the
  conventions path appears in the built prompt for agent-pair mode, and that
  TDD-mode injection is unchanged) rather than asserting on whole prompt
  strings, which are brittle.
