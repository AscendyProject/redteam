## What
Land the pure data-layer foundation for the #146 Phase 1 MVP benchmark: a fail-loud
`benchmark.toml` loader and a JSONL append/resume store, exposed by a new stdlib-only
`.redteam/workflows/benchmark.py` module that never runs a model, spawns a subprocess,
or mutates orchestrator state — so task-002/003 can build the loop and report on top of a
side-effect-free, hermetically tested seam.

## Why
This is the foundation slice of #146 Phase 1 MVP benchmark. The parent goal
(`benchmark-phase1/goal.md`) adds an `orchestrator benchmark <set>` / `benchmark-report <set>`
subcommand pair that compares a small explicit list of named `[configs]` on a curated
task-set, on deterministic metrics only, with hard cost controls. Splitting the work
along three layers lets task-002 (loop/dispatch) and task-003 (subcommand wiring +
aggregation) build against a pure, hermetically testable data seam without any real
model invocation.

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

## Verification
- Tests: test_load_happy_path_full, test_load_defaults_repetitions_budget, test_load_task_ids_sorted, test_load_single_config_partial_roles, test_load_budget_usd_int_accepted, test_load_empty_input_md_ignored, test_load_fails_missing_toml, test_load_fails_unknown_top_level_key, test_load_fails_matrix_key, test_load_fails_unknown_per_config_role, test_load_fails_repetitions_bool, test_load_fails_repetitions_string, test_load_fails_repetitions_zero, test_load_fails_budget_usd_zero, test_load_fails_budget_usd_negative, test_load_fails_budget_usd_bool, test_load_fails_zero_configs, test_load_fails_empty_tasks_tree, test_load_fails_no_tasks_dir, test_load_fails_role_value_int, test_load_fails_role_value_bool, test_load_fails_role_value_float, test_load_fails_role_value_empty_string, test_load_fails_role_value_whitespace_string, test_load_fails_role_value_array, test_append_record_creates_parent_and_file, test_append_record_writes_compact_json_line, test_load_records_roundtrip_two_records, test_load_records_skips_blank_lines, test_load_records_malformed_line_raises_with_path_and_lineno, test_completed_triples_done_records, test_completed_triples_error_counts_as_completed, test_completed_triples_deferred_counts_as_completed, test_completed_triples_empty, test_completed_triples_multiple_repetitions, test_benchmark_module_stdlib_only_imports, test_benchmark_module_no_side_effects_at_import_time
- Verify command: `bash .redteam/scripts/verify.sh` ✅

## Code review summary
- Diff summary: `git diff main...HEAD` changes only the two pinned files — `.redteam/workflows/benchmark.py` (new module) and `.redteam/tests/test_benchmark_config_and_store.py` (new hermetic tests).
- Loader fail-loud coverage confirmed: unknown top-level keys, matrix-shaped `benchmark`, bad `repetitions`, bad `budget_usd`, zero configs, unknown roles, bad role value types (int/bool/float/empty/whitespace/array), missing `benchmark.toml`, and empty tasks tree all raise `ValueError` naming the offending key/section (`benchmark.py:69-148`).
- JSONL store contract met: parent creation and compact append with UTF-8, `load_records` skips blank lines and names path/line on malformed JSON, `completed_triples` counts every present record regardless of outcome (`benchmark.py:183-222`).
- Record schema is a `TypedDict` with the required deterministic fields and `schema_version = 1` (`benchmark.py:156-175`).
- New tests are discriminating: against pre-change code every test fails at import (module absent). Anti-degeneracy check is N/A — this change produces no score/ranking/classification.
- Reviewer relied on the recorded verification (`verification.log` reports `791 passed`, `state.json` shows `verification.last_exit_code: 0`) rather than re-running in a read-only sandbox. **REVIEW_DECISION: APPROVED.**

## Generated by
redteam / batch benchmark-phase1 / task task-001-benchmark-config-and-store
