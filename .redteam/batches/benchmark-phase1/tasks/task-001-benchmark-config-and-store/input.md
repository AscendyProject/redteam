# task-001 — benchmark config loader + JSONL result store (data layer)

## Why this task exists

This is the foundation slice of the #146 Phase 1 MVP benchmark. The parent goal
(`benchmark-phase1/goal.md`) implements a new subcommand pair — `orchestrator
benchmark <set>` and `orchestrator benchmark-report <set>` — that lets an
operator compare a small **explicit list of named `[models]` configurations**
on a curated task-set, on **deterministic metrics only**, with hard cost
controls, and read the result as a markdown diff.

The whole implementation splits cleanly along the three layers named in the
goal. This task ships **layer 1**: the pure, side-effect-light data plumbing
(TOML config parser + JSONL result-record schema + append/resume helpers).
Nothing here should invoke a model, spawn a subprocess, or wire a subcommand
into `orchestrator.main` — those belong to task-002 and task-003 respectively.

Building this cleanly is what makes the downstream tasks testable without real
model runs (a critical hard constraint of the goal).

## What to build

Add a new engine module — suggested name
`.redteam/workflows/benchmark.py` — that exposes at minimum:

1. **`benchmark.toml` loader** (fail-loud, mirroring the discipline of
   `workflows/config.py`):
   - Schema:
     - `repetitions` (int, default **1**, must be `>= 1`; reject `bool`).
     - `budget_usd` (float or int, optional; must be `> 0` when set; a `None`
       / absent value means "no cap"; reject `bool`).
     - `[configs.<name>]` tables — **one per named config**. Each table
       overrides the four `[models]` roles: `planner`, `implementer`,
       `reviewer`, `rescue`. At least one `[configs.<name>]` table must be
       present. A single named config with only some roles set IS allowed (the
       unset roles fall back to the base `.redteam/config.toml` at runtime —
       do not resolve that here; just carry the override dict through).
   - **Explicit list of configs, NOT a matrix.** Any hint of matrix expansion
     (e.g. an `[[benchmark.matrix]]` array) must be rejected as unknown.
   - Fail loud on unknown top-level keys, unknown per-config keys (only the
     four role names allowed inside `[configs.<name>]`), and bad types. Model
     the loader after `config.py`'s `_reject_unknown_keys` / dataclass pattern
     (frozen dataclasses, `tomllib`, no third-party deps).
   - Return a small frozen dataclass (e.g. `BenchmarkSet`) exposing
     `repetitions`, `budget_usd`, and an ordered mapping of
     `name -> {role: model_id}` overrides. Preserve config-declaration order
     (dict insertion order from `tomllib` is fine).
   - The set root is `.redteam/benchmarks/<set>/`. The loader takes the set
     root `Path` and reads `benchmark.toml` from it; it also enumerates
     `tasks/<id>/input.md` under that root and returns the sorted list of task
     ids that have a non-empty `input.md`. An empty task-set must fail loud.

2. **JSONL result-record schema + append/resume helpers** for
   `.redteam/benchmarks/<set>/results.jsonl`:
   - Define the record shape as a `TypedDict` (or a small frozen dataclass +
     `to_json` helper) with the deterministic fields the parent goal lists —
     at minimum:
     - `schema_version` (int; start at `1`),
     - `config` (str — name from `[configs.<name>]`),
     - `task` (str — task id),
     - `repetition` (int — 1-indexed),
     - `outcome` (`"done" | "deferred" | "error"`),
     - `review_rounds` (int),
     - `retry_count` (int),
     - `rescue_count` (int),
     - `scope_creep_count` (int — floor-trip count),
     - `wall_clock_sec` (float),
     - `claude_cost_usd` (float or `None` when only Codex-role phases ran —
       never fabricate a value),
     - `started_at` / `finished_at` (ISO-8601 UTC strings — use
       `datetime.now(timezone.utc).isoformat()`).
   - `append_record(jsonl_path: Path, record: dict) -> None`: append **one
     JSON object per line** (`json.dumps(..., separators=(",", ":"))` +
     trailing `\n`); create parent directory + file on first write; open with
     `encoding="utf-8"`; ensure the write is a single atomic-enough `write`
     call to a text-mode file (a single `f.write(line)` under `open(..., "a")`
     is sufficient — do not add fsync/locking gymnastics for the MVP).
   - `load_records(jsonl_path: Path) -> list[dict]`: parse the file line by
     line; **skip blank lines silently**; on a malformed line raise a clear
     `ValueError` naming the file + line number (fail-loud on corruption, do
     not "recover" silently — the JSONL is the source of truth).
   - `completed_triples(records) -> set[tuple[str, str, int]]`: return the
     set of `(config, task, repetition)` triples with **any** record present
     (an error/deferred record still counts as completed for resume purposes
     — the runner does not silently re-run to try to get a better result;
     that is a Phase 2 concern and out of scope).

3. **Pin down cross-cutting invariants** in module docstrings:
   - Zero non-stdlib imports (`tomllib`, `json`, `dataclasses`, `pathlib`,
     `datetime` — stdlib only).
   - No hidden filesystem side effects at import time.
   - The module must not touch `.redteam/config.toml` or any batch directory.

## Tests to add

Add `.redteam/tests/test_benchmark_config_and_store.py` (or split into
`test_benchmark_config.py` + `test_benchmark_store.py` if clearer) covering:

- **Loader happy path** — a fixture `benchmark.toml` with `repetitions = 2`,
  `budget_usd = 5.0`, and two named configs (each overriding two of the four
  roles) parses into the expected `BenchmarkSet`.
- **Loader fail-loud cases**:
  - unknown top-level key rejected;
  - unknown per-config role key rejected;
  - `repetitions` not an int (e.g. bool `True`, or string) rejected;
  - `budget_usd` `<= 0` rejected;
  - zero `[configs.*]` tables rejected;
  - an `[[benchmark.matrix]]` array (or any matrix-like top-level key)
    rejected — matrix expansion is a Phase 2 non-goal;
  - a set root missing `benchmark.toml` rejected;
  - a set root with `tasks/` empty (no non-empty `input.md`) rejected.
- **JSONL store** (all against `tmp_path`):
  - `append_record` creates the file + parent dir on first write;
  - two appended records round-trip via `load_records` in append order;
  - a malformed line raises `ValueError` naming the file + line number;
  - `completed_triples` returns the expected set (including a record with
    `outcome="error"` — errored triples ARE considered completed for resume).

Keep the tests hermetic: use `tmp_path`, no network, no subprocess, no import
of adapters/orchestrator. This suite must not fingerprint the redteam project
stack (`test_agents_generic_prompts.py` guards agent bodies; this test file
is a different guard but keep to the same project-agnostic spirit — assert
on the loader/store contract, not on redteam-specific model ids).

## Affected files (must stay strictly within these trees)

Both `create` and `modify` are pinned inside these two trees per the parent
goal's constraint:

- `.redteam/workflows/benchmark.py` — **new** module (loader + store).
- `.redteam/tests/test_benchmark_config_and_store.py` — **new** test file
  (split into two files if you prefer, as long as both stay under
  `.redteam/tests/`).

Do **not** touch `orchestrator.py`, any `phase_runners/*`, any adapter, or
`config.py` in this task — those are downstream. Do not create anything
under `.redteam/benchmarks/` in the repo (the fixture set lives in the tests
via `tmp_path`).

## Constraints inherited from the parent

- **Zero runtime dependencies.** Stdlib only. No pandas, no numpy, no
  promptfoo/LangSmith/Braintrust, no toml third-party lib (`tomllib` is
  stdlib in Python 3.11+, which the engine already requires).
- **Engine stays project-agnostic.** No stack fingerprints in
  `.redteam/workflows/` or non-example tests; keep
  `test_agents_generic_prompts.py` green.
- **Fail-loud loader discipline.** Mirror `config.py`'s posture — unknown
  keys, bad types, and empty inputs error out with a message that names the
  offending key/section. Do **not** silently coerce or default around a typo.
- **No side effects at import time.** The module must be safe to import from
  tests without touching disk.
- **Safety boundary — do NOT touch the operator's config or repo.** No writes
  to `.redteam/config.toml`, no branch mutation, no calls into adapters.
  This task is pure data.

## Non-goals for this task (Phase 2+ or later slices)

- Any subcommand wiring into `orchestrator.main` / `USAGE` (that is
  task-003).
- The benchmark loop / dispatch / `run_one` seam / budget accounting /
  `--dry-run` (task-002).
- Aggregation / markdown-diff rendering (task-003).
- Matrix expansion, Pareto frontier, `recommend-models`, LLM-judge scorers,
  sqlite mirror, external eval-platform export — all Phase 2, do NOT build.
- Historical-batch conversion (`.redteam/batches/*` → benchmark set).
- Any change to `phase_telemetry` shape or the state.json schema — this task
  only *reads* the shape task-002 will feed it via records.

## Verification

The task's `outcome.md` must carry a parseable `## Verification` section whose
fenced ```yaml `commands:` list is exactly:

```yaml
commands:
  - bash .redteam/scripts/verify.sh
```

**Do NOT add a bare `pytest …` line** — bare `pytest` is not on PATH in this
venv-based repo and the verification exec would fail (see harness gotcha noted
in the parent goal). `verify.sh` already runs `ruff` + `pytest` over
`.redteam/`, which covers the new tests.

## Done when

- `benchmark.py` exists under `.redteam/workflows/` with the loader + JSONL
  helpers described above.
- `.redteam/tests/test_benchmark_config_and_store.py` exists and covers the
  happy paths + every fail-loud case listed above.
- `bash .redteam/scripts/verify.sh` is green (ruff + full pytest, including
  the new tests and `test_agents_generic_prompts.py`).
- No file outside the two affected trees has been modified.
