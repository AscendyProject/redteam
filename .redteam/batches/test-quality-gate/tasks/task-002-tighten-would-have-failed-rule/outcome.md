# Outcome — Tighten the "would have failed before" Required Check (#159 + #161)

## Goal
Rewrite the Required Check paragraph in `.redteam/prompts/codex/code_review.md`
so it tightens the "would have failed against the pre-change code" rule in
both directions (source-text bypass banned; preventive suites given an
executable-demonstration criterion; a narrowly-scoped no-in-repo-execution-path
exemption) and add a matching "Runtime coverage" section to the templated
`test-conventions.md` for new consumers — with the rewrite in a state that
still allows the reviewer to return `APPROVED` on this very task.

## Audit of `.redteam/prompts/codex/code_review.md` consumers (re-run)
A repo-wide grep for the full path `.redteam/prompts/codex/code_review.md`
(excluding `__pycache__`, per the brief) plus this task's own `input.md` and
`outcome.md` (which reproduce the audit itself and would double-count) returns
exactly the **four** references the brief enumerates, all of which pass or
embed the file's *path* — none open, `read_text()`, parse, import, execute, or
otherwise interpret its contents:

- `.redteam/workflows/phase_runners/review_code.py:42` — `_code_review_prompt`;
  embeds the path in the built reviewer prompt as the criteria to apply.
- `.redteam/workflows/phase_runners/review_code.py:117` —
  `_narrowed_code_review_prompt`; same, for round-over-round narrowed review.
- `.redteam/workflows/orchestrator.py:2133` — the standalone
  `orchestrator review` prompt; embeds the path as criteria.
- `.redteam/workflows/orchestrator.py:452` —
  `_set_next_action_for_manual_phase`'s `prompt_map`; embeds the path in a
  human-readable instruction for the manual fallback.

The other repo-wide hits are batch artifacts (prior tasks' `outcome.md`,
`input.md`, `impl_diff.patch`, review rounds) and this batch's `goal.md` —
all documentation of the same path string, none of which are code that opens
or interprets the file. Clause C (no in-repo execution path) therefore
applies **only** to this one artifact, established per-artifact by naming
its in-repo consumers (the four above) and demonstrating that none of them
parse or interpret its contents — they only embed the path as a string
handed to the reviewer.

## Done-when
- [ ] `.redteam/prompts/codex/code_review.md` contains a single rewritten
      Required-Check paragraph (replacing the current one-sentence check at
      line 52, staying inside the `## Required Checks` section that spans
      lines 45–53) covering all three clauses with load-bearing phrasing,
      verified by markdown assertions in the new test file. Every semantic
      assertion below is scoped to the **rewritten Required Check
      paragraph** (isolated at test time by slicing the markdown between the
      `## Required Checks` and `## Finding Format` headers, then focusing
      on the paragraph that replaces the current "would have failed"
      bullet). Phrases appearing anywhere else in the file do NOT satisfy
      any of these items:
  - [ ] **Clause A** — the paragraph names both "source text" and the
        notion of a "bypass" or "execution path" together, and flags such
        tests `severity:major`.
  - [ ] **Clause B** — the paragraph names "preventive" (smoke /
        characterization) suites and requires an executable demonstration
        by a deliberately broken "fixture" that (i) lives in the **same
        file** as the suite, (ii) is exercised through the **same code
        path** the suite claims to protect, (iii) is **asserted to fail**,
        (iv) breaks the **behaviour the suite claims to protect** (not an
        unrelated contrived reason). All four qualifiers must be present
        in the paragraph — a fixture that fails for an unrelated contrived
        reason must be explicitly called out as not qualifying (same
        vacuousness as Clause A in a new costume).
  - [ ] **Clause C** — the paragraph names the "no in-repo execution
        path" exemption AND its "per artifact" eligibility obligation
        (never by file class / glob / directory / extension). The
        paragraph explicitly requires the reviewer to *establish*
        eligibility by naming that artifact's in-repo consumers and
        demonstrating none of them parse or interpret its contents. The
        paragraph explicitly warns that non-importable **configuration**,
        **templates**, **manifests**, and **workflow** definitions may
        still have behaviourally reachable semantics (loaded and acted on
        by code) and so are NOT automatically eligible — "not importable"
        is too weak. The paragraph also states that under the exemption
        only "semantic" clauses may be pinned (not incidental wording,
        formatting, ordering, or whitespace), and that where an execution
        path exists it must be preferred (built / assembled / observed
        behaviour comes before file-text pinning).
  - [ ] The paragraph forbids project-owned files overriding the Required
        Check (regression against #161's incorrect inversion).
- [ ] `.redteam/prompts/codex/code_review.md` still enumerates all four
      `REVIEW_DECISION:` values (`APPROVED`, `CHANGES_REQUESTED`,
      `RESCUE_REQUIRED`, `ASK_USER`) at the tail, asserted by a new test
      (regression against #103 — the file must remain in a state that can
      return `APPROVED`).
- [ ] `.redteam/templates/docs/test-conventions.md` gains a new section with
      heading exactly `## Runtime coverage` (concise, declarative,
      project-agnostic) that (a) requires tests to exercise the code path
      they claim to protect via importing / mounting / executing the thing
      under test and (b) calls out source-text guards as insufficient when
      an execution path exists — asserted by a new test that scopes its
      substring checks to the body of that section (sliced between the
      `## Runtime coverage` heading and the next `## ` heading, or EOF).
- [ ] The built agent-pair reviewer prompt returned by
      `phase_runners.review_code._code_review_prompt` still contains the
      substring `.redteam/prompts/codex/code_review.md` (regression: the
      reviewer is still pointed at the rule), asserted by a new test.
- [ ] The built agent-pair reviewer prompt returned by
      `phase_runners.review_code._narrowed_code_review_prompt` still contains
      the substring `.redteam/prompts/codex/code_review.md` and still
      enumerates the `REVIEW_DECISION:` vocabulary the runner expects
      (`APPROVED`, `CHANGES_REQUESTED`, `RESCUE_REQUIRED`, `ASK_USER`; no new
      values, no removals), asserted by a new test.
- [ ] All new test docstrings state which Done-when item and which semantic
      clause (A / B / C / regression) they pin.
- [ ] `.redteam/tests/test_agents_generic_prompts.py` remains green
      (no stack-specific fingerprints leaked into agent bodies).
- [ ] `bash .redteam/scripts/verify.sh` exits 0.

## Out of scope
- No new `REVIEW_DECISION` value, and no change to the four-value enumeration
  or the exit-code contract.
- No second reviewer pass, and no new review phase.
- No mechanism by which a project-owned file (`project-context.md`,
  `test-conventions.md`, etc.) can override a prompt-level Required Check —
  #161 explicitly refused that inversion; if a plan seems to require it,
  stop and use `ask_user_response.md`.
- No changes to `.redteam/docs/test-conventions.md` (that is the dogfood
  copy already updated by task 1).
- No changes to `.redteam/workflows/*` — Clause C rests on the built prompt
  keeping its current shape; if a runner edit seems necessary, stop and use
  `ask_user_response.md`.
- No changes to `.redteam/config.toml`, `.redteam/scripts/install.py`, the
  verification allowlist, batch state, adapters, or any agent skeleton.
- No generalisation of the Clause C exemption to any other prompt, file
  class, or directory. Only `.redteam/prompts/codex/code_review.md` is
  audited here.
- No change to the standalone `orchestrator review` prompt at
  `orchestrator.py:2133` or the manual-fallback `prompt_map` at
  `orchestrator.py:452` — those already embed the path unchanged, and the
  hard rules forbid touching workflow code from this task.
- No test that only asserts on the source text of code that *does* have an
  in-repo execution path (i.e. no self-defeating source-text guards on the
  rewritten prompt's rule *about* source-text guards).
- No fix for the sibling-`pr_url.txt` floor issue (#158); stashing is the
  operator recovery path per the brief.

## Affected files
- `.redteam/prompts/codex/code_review.md` — rewrite the single Required
  Check bullet at line 52 into a paragraph implementing Clauses A, B, and C
  with load-bearing phrasing; leave `REVIEW_DECISION:` enumeration at lines
  70–73 unchanged; leave the file's other sections (Inputs, Output, Scope
  discipline, other Required Checks, Finding Format, Decision) unchanged.
- `.redteam/templates/docs/test-conventions.md` — append a new
  `## Runtime coverage` section (heading exactly) at the end of the file,
  matching the existing template's concise, declarative,
  project-agnostic tone.
- `(new) .redteam/tests/test_would_have_failed_rule.py` — a single new test
  file (the test-writing phase — the implementer, since this task runs in
  agent-pair — creates it) housing (a) the built-prompt regressions against
  `phase_runners.review_code._code_review_prompt` and
  `_narrowed_code_review_prompt`, (b) the markdown semantic-clause
  assertions scoped to the rewritten Required Check paragraph inside
  `.redteam/prompts/codex/code_review.md`, and (c) the template assertion
  scoped to the new `## Runtime coverage` section body inside
  `.redteam/templates/docs/test-conventions.md`.

## Verification

### Existing (must continue to pass)

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### To be created (the test-writing phase will define exact test names)
- A new test file under `.redteam/tests/` (matching `test_*.py`) covering:
  - **Built-prompt regressions** (substring / `in` assertions against the
    strings returned by `phase_runners.review_code._code_review_prompt` and
    `_narrowed_code_review_prompt`; follow the `_Proj` fixture pattern
    already used in `test_mode_aware_prompts.py` and monkeypatch
    `project_config` where the runner calls it internally):
    - the prompt still names `.redteam/prompts/codex/code_review.md` as the
      criteria the reviewer applies;
    - the prompt still names all four `REVIEW_DECISION:` values (`APPROVED`,
      `CHANGES_REQUESTED`, `RESCUE_REQUIRED`, `ASK_USER`).
  - **Markdown semantic clauses** — read
    `.redteam/prompts/codex/code_review.md` once at test time, then
    **isolate the rewritten Required Check paragraph** (slice the text
    between the `## Required Checks` heading at line 45 and the next
    `## ` heading — `## Finding Format` — then further narrow to the
    paragraph that replaces the current "would have failed" bullet). All
    semantic assertions below are made **against that isolated slice**,
    never against the whole file (an unrelated appearance of the phrase
    elsewhere must not satisfy the gate):
    - Clause A — both "source text" and "bypass" / "execution path"
      co-occur in the isolated paragraph, together with
      `severity:major`.
    - Clause B — the isolated paragraph co-locates the words "preventive"
      (or an equivalent naming smoke/characterization suites), "fixture",
      "same code path", "same file", an assertion that the fixture is
      asserted to fail, and an explicit disqualification of a fixture that
      fails for an unrelated / contrived reason.
    - Clause C — the isolated paragraph co-locates "per artifact" (or an
      equivalent per-artifact eligibility phrase), "no in-repo execution
      path", the obligation to *establish* eligibility by naming the
      artifact's in-repo consumers and showing none parse or interpret its
      contents, and the warning that configuration / templates /
      manifests / workflow definitions may still have behaviourally
      reachable semantics (i.e. not-importable is too weak). The paragraph
      must also say that where an execution path exists it must be
      preferred, and that under the exemption only semantic clauses may
      be pinned (not incidental wording, formatting, ordering, or
      whitespace).
    - The isolated paragraph forbids a project-owned file overriding this
      Required Check (regression against #161).
  - **Tail regression** on the whole file: the four `REVIEW_DECISION:`
    values are still enumerated at the tail (existence-in-file is
    sufficient here because the concern is #103 — the file must remain
    in a state that can return `APPROVED`).
  - **Template regression** on `.redteam/templates/docs/test-conventions.md`:
    the file contains a `## Runtime coverage` heading and the assertions
    that "execution / mounting / importing are required" and "source-text
    guards are insufficient when an execution path exists" are checked
    **against the isolated body of that section** (sliced between the
    `## Runtime coverage` heading and the next `## ` heading, or EOF),
    not against the whole template.
- Every new test docstring names the Done-when item and semantic clause
  (A / B / C / regression) it pins so the reviewer can trace the coverage.
- Assertions use substring / `in` checks against the isolated slices,
  not full-string snapshots.

## Risks
- The rewritten Required Check paragraph is being reviewed *by the very
  rule it rewrites* (`review_code.py` hands the reviewer a path to the
  working-tree copy). If the rewrite is too strict — e.g. Clause B is
  worded so that the built-prompt regressions themselves count as
  "preventive suites" that owe an executable-demonstration broken fixture —
  the reviewer will refuse to `APPROVED`. The wording must therefore make
  clear that (a) the built-prompt regressions exercise the runner code path
  they claim to protect (i.e. they are not preventive), and (b) the
  markdown / template assertions ride Clause C's per-artifact exemption
  (established here by the audit). If the implementer cannot land wording
  that satisfies both directions, stop and use `ask_user_response.md`
  rather than loosening the rule.
- The paragraph-isolation slice depends on the current markdown structure
  (`## Required Checks` → `## Finding Format`). If the implementer
  restructures headings while rewriting, the tests must be updated to slice
  on the *new* structure and still enforce that the semantic clauses
  co-occur inside the *same* rewritten paragraph, not scattered across the
  file. Restructuring headings is a copy decision that stays in scope only
  if the isolation contract holds; otherwise stop and use
  `ask_user_response.md`.
- The brief permits "one or more" new test files. This outcome pins a
  single new file for scope-honesty; if the implementer finds that split
  into two or three files would materially improve reviewer traceability,
  that is a scope decision for the operator via `ask_user_response.md`.
- "Load-bearing phrasing" for Clauses A / B / C is a copy decision — the
  outcome pins the semantic phrases the tests must match (per the brief's
  guidance: "source text" + "bypass"/"execution path", "preventive" +
  "fixture" + "same code path" + "same file" + assertion-of-failure +
  disqualification of unrelated / contrived fixtures, "per artifact" + "no
  in-repo execution path" + audit-of-consumers + configuration / templates
  / manifests / workflows warning) but not the sentence structure. Final
  wording is the implementer's, constrained by the tests.
- If the re-run audit turns up a *fifth* consumer that opens or parses
  `.redteam/prompts/codex/code_review.md`, Clause C's exemption does not
  apply to this task's own tests; stop and use `ask_user_response.md`
  rather than proceeding with the current plan.
