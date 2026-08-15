# Outcome — Inject `test_conventions_file` into agent-pair phases (#160)

## Goal
In agent-pair mode, both the implementer (who writes the tests) and the code
reviewer (who judges them) are pointed at the project's `test_conventions_file`
so agent-pair tests are judged against the same conventions TDD-mode tests
already are. TDD mode is behaviour-identical.

## Done-when
- [ ] `_agent_pair_base_prompt(task_dir, proj)` in
      `.redteam/workflows/phase_runners/implement.py` includes the substring
      `proj.test_conventions_file`'s value (i.e. the string returned contains
      the test-conventions path passed in on `proj`), asserted by a new test
      in `.redteam/tests/test_mode_aware_prompts.py`.
- [ ] `_code_review_prompt(task_dir, base_branch)` in
      `.redteam/workflows/phase_runners/review_code.py` includes the
      test-conventions path from `project_config().test_conventions_file`,
      asserted by a new test.
- [ ] `_narrowed_code_review_prompt(task_dir, base_branch, prior_rev, open_items)`
      in the same file includes the test-conventions path from
      `project_config().test_conventions_file`, asserted by a new test.
- [ ] `_tdd_base_prompt(task_dir, proj)` in `implement.py` does NOT contain the
      test-conventions path (regression: TDD injection stays in
      `write_test.py` / `verify_test.py`, not in `implement.py`), asserted by
      a new test.
- [ ] `write_test.py`'s built prompt still contains
      `proj.test_conventions_file` and `verify_test.py`'s built prompt still
      contains `proj.test_conventions_file` (TDD-phase regression), asserted
      by new light tests.
- [ ] The first sentence of `.redteam/docs/test-conventions.md` factually names
      the new readers (i.e. no longer claims only "the test-author and
      test-verifier sub-agents read this"); the rest of the document is
      byte-identical.
- [ ] `.redteam/tests/test_agents_generic_prompts.py` remains green (no
      stack-specific fingerprints leaked into agent bodies).
- [ ] `bash .redteam/scripts/verify.sh` exits 0.

## Out of scope
- No changes to `_tdd_base_prompt` behaviour or wording beyond what the
  regression assertion pins.
- No new config keys and no schema changes on `ProjectConfig`
  (`test_conventions_file` already exists at `config.py:64`).
- No changes to `.redteam/prompts/codex/code_review.md` (task 2's surface).
- No changes to `.redteam/templates/docs/test-conventions.md` (task 2's
  surface).
- No rewrite of the conventions document itself — only the first sentence's
  reader list is corrected.
- No changes to `.redteam/config.toml`, the installer, the verification
  allowlist, batch state, or any adapter.
- No changes to the third `run(...)` fallback prompt in `review_code.py` (the
  non-headless, non-agent-pair branch beginning around line 390) — it is
  outside the two prompts the brief pins.

## Affected files
- `.redteam/workflows/phase_runners/implement.py` — add
  `proj.test_conventions_file` to the sentence in `_agent_pair_base_prompt`
  (~line 614) that already tells the implementer it writes the planned tests
  itself; leave `_tdd_base_prompt` (~line 795) untouched.
- `.redteam/workflows/phase_runners/review_code.py` — add
  `proj.test_conventions_file` to the enumeration in `_code_review_prompt`
  (~line 33) and `_narrowed_code_review_prompt` (~line 87) alongside the
  existing `security_checklist` / `context_file` / `code_review.md` triple;
  `project_config()` is already imported and called in both.
- `.redteam/docs/test-conventions.md` — first sentence only: replace
  "The test-author and test-verifier sub-agents read this" with a factually
  correct list of readers (adds the agent-pair implementer and code reviewer).
- `.redteam/tests/test_mode_aware_prompts.py` — extend the existing
  `_Proj` fixture pattern with a `test_conventions_file` attribute and add
  substring assertions for the four prompt behaviours above, plus the two
  TDD-phase regression assertions for `write_test.py` and `verify_test.py`.
  (No companion file is created; the brief allows either and the existing
  file is the natural home.)
- `.redteam/tests/test_baseline_trust_root_cross_run.py` — add
  `test_conventions_file` to the module-level `_PROJ` SimpleNamespace mock
  (mechanical consequence of `_agent_pair_base_prompt` now requiring it).
- `.redteam/tests/test_implement_untracked_baseline_pin.py` — same: add
  `test_conventions_file` to the module-level `_PROJ` mock.
- `.redteam/tests/test_implementer_commit.py` — same.
- `.redteam/tests/test_sibling_task_floor_exemption.py` — same.
- `.redteam/tests/test_tracked_baseline_attribution.py` — same.
- `.redteam/tests/test_floor_plan_affected_files_exemption.py` — same.

## Verification

### Existing (must continue to pass)

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### To be created (the test-writing phase will define exact test names)
- Tests under `.redteam/tests/test_mode_aware_prompts.py` covering:
  - agent-pair `_agent_pair_base_prompt` names `proj.test_conventions_file`
    (substring assertion against the built prompt string; do NOT snapshot the
    whole prompt);
  - agent-pair `_code_review_prompt` names
    `project_config().test_conventions_file`;
  - agent-pair `_narrowed_code_review_prompt` names
    `project_config().test_conventions_file`;
  - TDD `_tdd_base_prompt` does NOT name `proj.test_conventions_file`
    (regression that TDD's injection stays in `write_test.py` /
    `verify_test.py`).
- TDD-phase regression tests (may live in the same file or a small companion
  test file under `.redteam/tests/` named to match `test_*.py`) covering:
  - `write_test.py`'s built prompt still contains
    `proj.test_conventions_file`;
  - `verify_test.py`'s built prompt still contains
    `proj.test_conventions_file`.
- All new assertions use substring/`in` checks against the built prompt
  strings, never full-prompt snapshots.

## Risks
- The review-code assertions need `project_config().test_conventions_file`
  to resolve during the test. `_code_review_prompt` and
  `_narrowed_code_review_prompt` both call `project_config()` internally
  rather than taking a `proj` fixture, so the test either (a) relies on the
  default `ProjectConfig` value (`.redteam/docs/test-conventions.md`) and
  greps for that literal, or (b) monkeypatches
  `phase_runners.review_code.project_config`. Choice belongs to the
  test-writing phase; either is stdlib-only and consistent with the file's
  existing patterns.
- The exact rewording of the first sentence of
  `.redteam/docs/test-conventions.md` is a copy decision — this outcome only
  requires that the sentence factually name the new readers (implementer +
  code reviewer for agent-pair, alongside the existing TDD readers). Wording
  is left to the implementer.
- No new pip dependency is required; the tests continue to import the
  existing runners via `_engine` as the current file already does. If any
  contributor considers adding one, they must stop — the engine is
  stdlib-only.
