Disagree

- None.

Uncertain

- None material. The TDD runner prompt tests will require mocking because the prompts are built inside `run()` (`write_test.py:86`, `verify_test.py:24`), but existing `_engine.write_test()` / `_engine.verify_test()` loaders and established monkeypatch patterns make the proposed scope feasible.

Agree

- The plan directly covers every requested prompt: `implement.py:614`, `review_code.py:33`, and `review_code.py:87`.
- It preserves `_tdd_base_prompt` at `implement.py:795` and regression-tests the existing conventions injection at `write_test.py:91` and `verify_test.py:47`.
- The documentation change is narrowly limited to the opening sentence of `.redteam/docs/test-conventions.md`.
- Affected files are explicitly pinned, with forbidden task-2 and security-boundary surfaces excluded.
- Verification is parseable by `_base.py:566-596`: the fenced YAML contains `- bash .redteam/scripts/verify.sh`. That command is a pure repository verification step.
- The tests use substring assertions rather than brittle full-prompt snapshots.
- No dependency, schema, allowlist, installer, adapter, or project-specific engine changes are proposed.

No open PR-NNN findings.

REVIEW_DECISION: APPROVED
