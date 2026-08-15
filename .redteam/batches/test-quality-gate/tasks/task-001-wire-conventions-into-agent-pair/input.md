# Task 1 — Inject `test_conventions_file` into agent-pair phases (#160)

## Problem

`test_conventions_file` (already declared in `.redteam/workflows/config.py` and
defaulted to `.redteam/docs/test-conventions.md`) is referenced by exactly two
phase runners today:

- `.redteam/workflows/phase_runners/write_test.py` (line ~91)
- `.redteam/workflows/phase_runners/verify_test.py` (line ~47)

Both are TDD-only phases. In the **default agent-pair mode** neither runs, so
the config key is dead: the implementer authors the tests with no knowledge of
the project's test wiring, and the reviewer has no conventions document to
judge them against. A consumer repo shipped a component that never mounted to
production; three "tests" passed because all three only grepped the source file
as text.

## Change

Inject the conventions path into the two prompts that actually write and
review tests in agent-pair mode:

1. `.redteam/workflows/phase_runners/implement.py` — the agent-pair implement
   prompt, built by `_agent_pair_base_prompt(task_dir, proj)` (~line 614).
   Name the project's test conventions at `proj.test_conventions_file` in the
   same sentence that already tells the implementer it must write the planned
   tests itself. Do not restructure the prompt; do not touch the TDD prompt
   (`_tdd_base_prompt`) — its behaviour is unchanged.
2. `.redteam/workflows/phase_runners/review_code.py` — the agent-pair review
   prompts, `_code_review_prompt(task_dir, base_branch)` (~line 33) and
   `_narrowed_code_review_prompt(...)` (~line 87). Both already list the
   review-criteria file, the security checklist, and the project hard-rules
   file; add `proj.test_conventions_file` to the same enumeration so the
   reviewer judges new tests against the project's conventions. `project_config()`
   is already imported and used in both.

No other runner changes. No new config keys, no schema changes — the field
already exists on `ProjectConfig`.

## Also fix (project-owned doc, one line)

The first sentence of `.redteam/docs/test-conventions.md` currently reads:

> The test-author and test-verifier sub-agents read this ...

After this change the implementer and the code reviewer read it too. Update
that sentence to reflect the new readers factually. This is narrow and
project-owned: **do not rewrite the conventions themselves**, do not add
sections, do not touch the templates copy at `.redteam/templates/docs/test-conventions.md`
(that is task 2's surface, and even there only for a different section).

## Tests

Extend `.redteam/tests/test_mode_aware_prompts.py` (or add a small companion
file if it fits the existing shape better — the existing tests already prove
the fixture pattern with `class _Proj`). Follow the pattern of the existing
prompt-wiring assertions rather than pinning whole prompt strings:

- **Agent-pair implement prompt** contains `proj.test_conventions_file`.
- **Agent-pair `_code_review_prompt`** contains `proj.test_conventions_file`.
- **Agent-pair `_narrowed_code_review_prompt`** contains `proj.test_conventions_file`.
- **TDD implement prompt** does NOT reference `proj.test_conventions_file`
  (its existing test-author/verifier contract is unchanged; TDD injection stays
  in `write_test.py` / `verify_test.py`, not in `implement.py`).
- **TDD-phase regression**: `write_test.py` and `verify_test.py` still inject
  the conventions path in their prompts, unchanged (add a light assertion if
  none exists today).

Prefer substring/`in` assertions against the built prompt strings — do not
snapshot the full prompt.

`.redteam/tests/test_agents_generic_prompts.py` must stay green (no stack
fingerprints in agent bodies).

## Hard constraints inherited from the goal

- **Engine stays project-agnostic.** No project- or stack-specific fingerprints
  in `.redteam/workflows/` or non-example tests.
- **Stdlib only, zero runtime deps.**
- **TDD mode is behaviour-identical** and regression-tested — do not remove or
  alter the `write_test.py` / `verify_test.py` injection.
- **No new config keys, no schema changes** — `test_conventions_file` already
  exists.
- Do not touch `.redteam/config.toml`, the installer's ownership split, the
  verification allowlist, or any batch state.
- Do not touch `.redteam/prompts/codex/code_review.md` or
  `.redteam/templates/docs/*` — those are task 2's surface.

## Operator delegation

Plan-level scope questions are delegated to the operator agent (prefer the
narrowest change; record decisions in `ask_user_response.md` or the final
report). Do **not** silently expand into rewriting the conventions doc or
touching TDD wiring.

## Affected files (pin exactly this scope)

- `.redteam/workflows/phase_runners/implement.py` (agent-pair prompt only)
- `.redteam/workflows/phase_runners/review_code.py` (both agent-pair review
  prompts)
- `.redteam/docs/test-conventions.md` (first-line factual fix only)
- `.redteam/tests/test_mode_aware_prompts.py` (or a small companion test file)

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

## Done-when

- The agent-pair implement prompt names the project's test conventions path.
- Both agent-pair code-review prompts (`_code_review_prompt` and
  `_narrowed_code_review_prompt`) name the project's test conventions path.
- TDD-mode prompts are behaviour-identical (regression-asserted).
- `.redteam/docs/test-conventions.md`'s first sentence factually reflects the
  new readers.
- `bash .redteam/scripts/verify.sh` passes (ruff + pytest over `.redteam/`).
- `test_agents_generic_prompts.py` is green.
