# Test conventions — <Project name>

> **Template.** The test-author and test-verifier sub-agents read this (path from
> `config.toml [project] test_conventions_file`) so generated tests match how
> your suite is actually wired — fixtures, stubs, DB, env. Fill it from your real
> `conftest`/setup. Keep it accurate: if a sub-agent changes the test
> infrastructure, refresh this file in the same PR. See
> `examples/fastapi-like/.redteam/docs/test-conventions.md` for a real example.

## Layout
- Where tests live (`test_dir` in config) and the file/dir naming split
  (unit/integration/api, per-package conftest or single).
- Where pytest config lives (`pyproject.toml` `[tool.pytest.ini_options]`, or
  `pytest.ini`).

## How external systems are stubbed
List the swap-outs a new test should NOT re-mock because setup already covers
them — module-level `sys.modules` fakes, autouse fixtures, network/IO patches.
State *where* they happen (import-time vs fixture) so ordering is clear.

## Database
- Test DB engine (in-memory? container? transaction-rollback isolation?).
- Any type/dialect aliasing that changes what queries are safe in tested paths.
- Schema create/drop lifecycle.

## Core fixtures
Enumerate the fixtures a test author should reach for first, with one line each
on what they provide (authed client, seeded user, storage row, mocked clients).

## Async / concurrency
The established pattern for testing async code (route-level, `asyncio.run`,
`pytest-asyncio`), or state plainly that it is unestablished.

## Environment variables set at test load
The env the suite sets before import, so tests don't override them blindly.

## Gaps the sub-agent should NOT silently fill
Name the missing fixtures / un-stubbed systems so a sub-agent patches callsites
directly instead of inventing fragile global setup. If a needed fixture is
missing, the convention is to add it centrally and update this file in the same
PR rather than inlining ad-hoc setup.
