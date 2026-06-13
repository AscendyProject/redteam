# Test conventions — redteam

> The test-author and test-verifier sub-agents read this (path from
> `config.toml [project] test_conventions_file`) so generated tests match how
> redteam's suite is actually wired. This repo dogfoods its own harness, so this
> describes redteam's tests (stdlib + pytest, no DB/async framework). For a
> web/DB-shaped example see `examples/fastapi-like/.redteam/docs/test-conventions.md`.

## Layout
- Tests live in `.redteam/tests/` (`test_dir` in config), flat, one `test_*.py`
  per concern. Functions are `test_*`; no classes needed.
- pytest config is minimal/implicit; the gate is `bash .redteam/scripts/verify.sh`
  (`ruff check` + `ruff format --check` + `pytest -q` over `.redteam/`).

## Importing the engine under test
The engine is **not** an installed package — it lives under `.redteam/workflows/`
and resolves its repo root from its own file location. Tests load modules by path
via `importlib.util`, mirroring the existing helpers:
- `_load_orchestrator_module()` / `_load_base_module()` — `spec_from_file_location`
  + `exec_module` on `orchestrator.py` / `phase_runners/_base.py`.
- For `adapters` imports, prepend `.redteam/workflows` to `sys.path` (see
  `test_reviewer_adapter.py`) then `from adapters import ...`.
Copy the existing helper rather than inventing a new import mechanism.

## How external systems are stubbed
- **subprocess is always mocked — never spawn a real `codex`/`claude`/`git`/`gh`.**
  Patch at the module that calls it: `patch("adapters.codex.subprocess.run", ...)`,
  `patch("adapters.claude.subprocess.run", ...)`, or `monkeypatch.setattr(base.subprocess, "Popen", fake_popen)`. Use a `MagicMock`/`_FakeProc` with
  `returncode`/`stdout`/`stderr` (and `.wait()`/`.kill()` for Popen fakes).
- **repo_root / branch helpers** are monkeypatched to `tmp_path`
  (`monkeypatch.setattr(orch, "repo_root", lambda: tmp_path)`,
  `_ensure_task_branch` → a stub) so no real git state is touched.
- **PHASE_RUNNERS** entries are swapped with `monkeypatch.setitem` to halt a task
  at a chosen phase and capture state.

## Database / async
None. redteam has no datastore and no async framework — do not introduce
`pytest-asyncio`, a DB fixture, or a container. State is plain dicts persisted as
`state.json`; the filesystem is the only external surface, exercised via
`tmp_path`.

## Core fixtures / patterns
- `tmp_path` for task dirs: write `outcome.md` / `state.json` / sentinels, then
  drive `process_task(task_dir)` and assert on the persisted state / return code.
- `monkeypatch` for module-level swaps (subprocess, repo_root, runners).
- `capsys` to assert on stdout/stderr (exit-code commands, WARN fail-safe paths).

## Conventions for new tests
- Each test traces to one behavior; reference the issue/finding in the docstring
  for regression tests (e.g. `#35`, `PR-001`).
- Assert the **fail-closed** outcome explicitly where it applies (deferred /
  non-zero exit / untouched file), not just the happy path.
- If you change test infrastructure (a shared helper, an import shim), refresh
  this file in the same PR.

## Gaps the sub-agent should NOT silently fill
There is no shared `conftest.py`; the per-file `importlib` loader is the
convention. If you need a shared helper, add it deliberately and note it here —
don't invent a global fixture or a package `__init__` to make imports cleaner.
