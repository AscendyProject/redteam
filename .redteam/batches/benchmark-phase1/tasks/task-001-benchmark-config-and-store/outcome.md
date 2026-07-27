# Outcome — benchmark config loader + JSONL result store (data layer)

## Goal
Land the pure data-layer foundation for the #146 Phase 1 MVP benchmark: a fail-loud
`benchmark.toml` loader and a JSONL append/resume store, exposed by a new stdlib-only
`.redteam/workflows/benchmark.py` module that never runs a model, spawns a subprocess,
or mutates orchestrator state — so task-002/003 can build the loop and report on top of a
side-effect-free, hermetically tested seam.

## Done-when
- [ ] `.redteam/workflows/benchmark.py` exists and imports cleanly (`python -c "import benchmark"` from `.redteam/workflows/` succeeds) with no non-stdlib imports and no filesystem access at import time.
- [ ] The module exposes a `BenchmarkSet` frozen dataclass with fields `repetitions: int`, `budget_usd: float | None`, `configs: dict[str, dict[str, str]]` (insertion order preserved), and `task_ids: tuple[str, ...]` (sorted).
- [ ] The module exposes a loader (e.g. `load_benchmark_set(set_root: Path) -> BenchmarkSet`) that reads `<set_root>/benchmark.toml` and enumerates non-empty `<set_root>/tasks/<id>/input.md` entries.
- [ ] Loader defaults: `repetitions` defaults to `1` when absent; `budget_usd` defaults to `None` (no cap) when absent.
- [ ] Loader fails loud (raises `ValueError` naming the offending key/section) on: unknown top-level key, any `[configs.<name>]` sub-key not in `{planner, implementer, reviewer, rescue}`, `repetitions` that is a `bool` or not an `int` or `< 1`, `budget_usd` that is a `bool` or not a number or `<= 0`, zero `[configs.*]` tables, any `[[benchmark.matrix]]` / matrix-shaped top-level key, missing `benchmark.toml`, and a tasks tree with no non-empty `input.md`.
- [ ] Loader fails loud on bad per-config role value TYPES: every value under `[configs.<name>]` (`planner` / `implementer` / `reviewer` / `rescue`) must be a **non-empty `str`**; a non-string (e.g. `planner = 123`, `reviewer = true`, `rescue = 1.5`, an inline table/array) OR an empty / whitespace-only string raises `ValueError` whose message names the offending `configs.<name>.<role>` path. `bool` is rejected even though it is stringifiable.
- [ ] The module exposes `append_record(jsonl_path: Path, record: dict) -> None` that creates the parent directory and file on first call, opens with `encoding="utf-8"`, and writes exactly one compact JSON object (`json.dumps(..., separators=(",", ":"))`) followed by `\n` per call.
- [ ] The module exposes `load_records(jsonl_path: Path) -> list[dict]` that returns records in append order, silently skips blank lines, and raises a `ValueError` naming the file path and 1-indexed line number on any malformed JSON line.
- [ ] The module exposes `completed_triples(records) -> set[tuple[str, str, int]]` returning `(config, task, repetition)` triples for every record present (records with `outcome="error"` or `outcome="deferred"` still count as completed).
- [ ] The module docstring pins the cross-cutting invariants: stdlib-only imports, no side effects at import time, and never touches `.redteam/config.toml` or any `.redteam/batches/` path.
- [ ] The record schema (TypedDict or frozen dataclass + `to_json` helper) documents the deterministic fields listed in the brief (at minimum `schema_version`, `config`, `task`, `repetition`, `outcome`, `review_rounds`, `retry_count`, `rescue_count`, `scope_creep_count`, `wall_clock_sec`, `claude_cost_usd`, `started_at`, `finished_at`) with `schema_version = 1`.
- [ ] `bash .redteam/scripts/verify.sh` is green (ruff check + ruff format check + full pytest, including `test_agents_generic_prompts.py` and the new tests).
- [ ] `git diff --name-only main...HEAD` lists only files under `.redteam/workflows/benchmark.py` and `.redteam/tests/test_benchmark*.py` (no other file modified).

## Out of scope
- Wiring `benchmark` / `benchmark-report` subcommands into `orchestrator.main` or `USAGE` (task-003).
- The benchmark loop, `run_one` seam, budget accounting, `--dry-run`, dispatch/isolation (task-002).
- Aggregation, markdown-diff rendering, cross-config comparison (task-003).
- Any change to `phase_telemetry`, `state.json` schema, `config.py`, adapters, phase runners, or `orchestrator.py`.
- Matrix expansion, Pareto frontier, `recommend-models`, LLM-judge scorers, sqlite mirror, external eval-platform export (Phase 2+).
- Creating anything under `.redteam/benchmarks/` in the repo (fixtures live under `tmp_path` in tests only).
- Fallback resolution of unset roles against `.redteam/config.toml` (loader just carries the override dict through; runtime resolution is task-002's problem).
- Validating that a role override string is a *known* adapter/model id (adapter-registry lookup belongs to task-002 at dispatch time; this loader only enforces the value is a non-empty `str`).
- Cross-run fsync/locking or partial-write recovery on `results.jsonl` (single `write` call is sufficient for the MVP).

## Affected files
- `(new) .redteam/workflows/benchmark.py` — new stdlib-only module: `BenchmarkSet` dataclass, `load_benchmark_set`, JSONL record schema, `append_record`, `load_records`, `completed_triples`.
- `(new) .redteam/tests/test_benchmark_config_and_store.py` — new hermetic test file (may be split into `test_benchmark_config.py` + `test_benchmark_store.py` if the implementer prefers, provided both live under `.redteam/tests/` and match `test_*.py`).

## Verification

### Existing (must continue to pass)

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

### To be created (the test-writing phase will define exact test names)
- tests under `.redteam/tests/` covering loader happy path: a fixture `benchmark.toml` under `tmp_path` with `repetitions = 2`, `budget_usd = 5.0`, and two named `[configs.<name>]` tables (each overriding two of the four roles) parses into the expected `BenchmarkSet` (repetitions/budget/configs mapping preserved in declaration order; `task_ids` sorted from the fixture `tasks/` tree).
- tests under `.redteam/tests/` covering loader fail-loud cases (each raising `ValueError` whose message names the offending key/section): unknown top-level key; unknown per-config role key; `repetitions = True`; `repetitions` non-int (e.g. string); `budget_usd <= 0`; zero `[configs.*]` tables; a matrix-like top-level key such as `[[benchmark.matrix]]`; set root missing `benchmark.toml`; set root whose `tasks/` tree contains no non-empty `input.md`.
- tests under `.redteam/tests/` covering **per-config role value type validation** — each case raises `ValueError` whose message names `configs.<name>.<role>`: `[configs.a] planner = 123` (int rejected); `[configs.a] reviewer = true` (bool rejected); `[configs.a] rescue = 1.5` (float rejected); `[configs.a] implementer = ""` (empty string rejected); `[configs.a] implementer = "   "` (whitespace-only string rejected); at least one non-scalar shape such as `[configs.a] planner = ["x"]` OR an inline-table value (non-string rejected).
- tests under `.redteam/tests/` covering the JSONL store against `tmp_path`: `append_record` creates parent dir + file on first write; two appended records round-trip through `load_records` in append order; blank lines are silently skipped; a malformed JSON line raises `ValueError` naming the file path and 1-indexed line number; `completed_triples` returns the expected set including a triple whose only record has `outcome="error"` (errored triples ARE considered completed for resume).
- tests under `.redteam/tests/` asserting the module has no side effects at import time and no non-stdlib imports (e.g. a `sys.modules` / `AST` check, or a `tmp_path`-scoped cwd import that inspects the module's `__annotations__` / imports list) — kept generic to preserve `test_agents_generic_prompts.py`'s project-agnostic spirit.

## Risks
- The brief permits splitting the test module into two files (`test_benchmark_config.py` + `test_benchmark_store.py`). Either shape is inside the pinned tree; the implementer chooses.
- The record shape may be modeled as a `TypedDict` OR a frozen dataclass + `to_json` helper — the brief accepts either; the implementer picks the one that reads cleanest against `config.py`'s existing dataclass idiom.
- `budget_usd` accepts both `int` and `float` per the brief; the loader must reject `bool` (since `bool` is a subclass of `int` in Python) — worth an explicit test to avoid the silent-True-as-1 trap that bit `config.py`'s ceilings. The same `bool`-is-`int` subclass trap applies to `repetitions` and to any per-config role value if the implementer type-checks with `isinstance(..., int)` / `isinstance(..., str)` naively.
- The invariant "no non-stdlib imports" is asserted by static inspection; if the implementer adds a runtime import guard instead, the exact assertion shape is their call — the intent is that CI fails loud if a `pip` dep sneaks in.
- The brief does not fix the loader's public function name (`load_benchmark_set` is a suggestion). The test-writing phase settles the exact symbol; downstream tasks (002/003) may need to align.
- "Non-empty string" for role override values is treated as `isinstance(v, str) and not isinstance(v, bool) and v.strip() != ""`. If the operator wants to allow explicit empty-string-as-"use base config" sentinels later, that expands the loader contract and belongs to a future task.
