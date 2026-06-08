# Cross-stack validation findings (#7.5)

Pre-handoff solo coupling smoke: cloned a real JS repo (Vite/React/TS, eslint +
vitest + playwright) to a throwaway dir, ran `install.py` into it, authored a JS
`config.toml`, and exercised the engine without model calls. Goal: surface
residual Python/ascendy coupling before asking a sibling agent pair to run a
live end-to-end task.

## Clean (no coupling)

- **install.py** vendors cleanly into the JS repo; the consumer's own files are
  untouched (the repo had no `.claude/agents`, but the file-by-file copy path is
  the one regression-tested in `test_install.py`).
- **config loads** with JS values: `source_dirs=["src/","components/","pages/"]`,
  `test_file_glob="*.spec.ts"`, `verify_command="npm run lint && npm run test"`,
  `base_branch="main"`.
- **orchestrator `status`** runs on the JS repo (exit 0) — no Python assumption
  at import/startup.
- **review_code / create_pr** runners carry no hardcoded diff-base or `.py`
  literals — the base-branch + paths are config-driven (closed in extraction #4).

## Finding F-1 (MEDIUM): verification allowlist is Python-only

`phase_runners/_base.py:354-355` hardcodes the verification-command allowlist to
`{"pytest", "ruff", "mypy"}` (bare tools) and the same set for `python -m <mod>`.

The project's configured `verify_command` is exact-argv-trusted, so
`"npm run lint && npm run test"` as a whole is allowed. **But** any *granular*
verification step an LLM-authored `outcome.md` ("Verification hooks") proposes
that is neither the exact configured command nor pytest/ruff/mypy — e.g.
`vitest run`, `eslint .`, `tsc --noEmit` — is **rejected** by
`validate_verification_commands`.

Effect on a non-Python project: it can only use the one monolithic
`verify_command`; the moment the planner emits per-step JS verification, the
phase fails validation. Incomplete + fragile cross-stack support.

This is a **security boundary** (extraction #2-verify, Codex 3 rounds) — the
allowlist exists so an LLM-authored outcome.md cannot smuggle arbitrary
commands. So it must not be loosened casually.

### Proposed fix (deliberate, plan_review warranted)

Make the bare-tool allowlist config-driven:

- Add `ProjectConfig.verification_allowlist: tuple[str, ...] = ("pytest", "ruff", "mypy")`
  (default preserves current Python behavior).
- `validate_verification_commands` reads it (plus the still-trusted exact
  `verify_command`); the `python -m <module>` convenience branch gates its
  module on the same configured set.
- A JS project sets e.g. `verification_allowlist = ["vitest", "eslint", "tsc", "npm", "npx"]`.
- Keep: exact-argv trust for `verify_command`, the shell-metachar reject, the
  path/`./`-executable reject. The trust model is unchanged; only the
  *project-declared* bare-tool set becomes configurable instead of hardcoded.

Tracked as the gating item before the frontend live handoff (a sibling would hit
F-1 immediately otherwise). Sync `AGENTS.md` "Allowed verification command
families" wording with whatever ships.
