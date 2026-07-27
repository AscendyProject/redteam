# Goal: #146 Phase 1 — MVP benchmark (named configs → JSONL → diff report)

## Intent

Implement Phase 1 of the accepted #146 design
(`docs/decisions/2026-07-13-benchmark-design.md`), building directly on the
Phase 0 telemetry that already lands in `state["phase_telemetry"]` (#150). The
MVP lets an operator compare a small set of **named `[models]` configurations**
on a **curated task-set**, on **deterministic metrics only**, with **hard cost
controls**, and read the result as a **markdown diff**. NO matrix expansion, NO
Pareto, NO `recommend-models`, NO LLM-judge (all Phase 2).

Deliver two orchestrator subcommands and their data layer:

- **`orchestrator benchmark <set>`** — for each named config in the set, run the
  curated tasks and append one result record per `(config, task, repetition)`
  to a JSONL store. Resumable: a completed triple is skipped on re-run.
- **`orchestrator benchmark-report <set>`** — read the JSONL and print a
  markdown table **diffing the configs** on the deterministic metrics. No single
  hidden score, no Pareto.

## Dataset & config

- `.redteam/benchmarks/<set>/benchmark.toml`:
  - `repetitions` (int, default **1**), `budget_usd` (float, optional hard cap).
  - Named configs as `[configs.<name>]` tables, each an override of the four
    `[models]` roles (`planner`/`implementer`/`reviewer`/`rescue`). An explicit
    **list of named configs — NOT a matrix**. Fail loud on unknown keys / bad
    types (mirror the existing `config.py` loader discipline).
- `.redteam/benchmarks/<set>/tasks/<id>/input.md` — curated task briefs, plus
  optional oracle fields the metrics can read (verification command is the
  project's; keep briefs small and deterministic).
- Store: `.redteam/benchmarks/<set>/results.jsonl` (append-only).

## Metrics — DETERMINISTIC ONLY (read from state.json after each task run)

Per `(config, task, repetition)` record and aggregated per config:
`outcome` (done / deferred / error), review-round count, retry + rescue counts,
scope-creep (floor-trip) count, wall-clock seconds, and **Claude cost** summed
from `state["phase_telemetry"]` cost_usd (Codex-role phases contribute `null` →
reported `n/a`, never fabricated). Aggregate as approval rate, avg review rounds,
retry/rescue rate, and Claude cost per approved task. No LLM-judge score.

## Cost controls (MANDATORY — a benchmark run is real full-pipeline execution)

- **`--dry-run`**: print the planned run count (`configs × tasks × repetitions`
  minus already-completed) and a rough cost estimate (from prior JSONL records
  if any, else "unknown"), then exit 0 **without running anything**.
- **`budget_usd`**: before dispatching each run, if accumulated Claude cost this
  invocation would exceed the cap, **abort before the run** with a clear message
  (fail-closed on cost). Default: no cap.
- **`repetitions`** default 1.

## Hard constraints

- **Testability without real model runs (CRITICAL).** The benchmark loop
  (which configs × tasks × reps to run, budget accounting, JSONL append/resume,
  metric extraction, report) MUST be separable from the actual pipeline
  execution behind a single **injectable** function (e.g. a
  `run_one(set, config_name, task_id) -> result_dict` seam the tests stub). No
  test may spawn a real `claude`/`codex` CLI or run a real pipeline.
- **Must never corrupt the real repo.** A benchmark run must not leave the
  operator's working tree, branches, or `.redteam/config.toml` mutated. Prefer
  running each config against an **isolated copy / temp state** (the design's
  worktree isolation; a sequential temp-config approach is acceptable for the
  MVP as long as the real `config.toml` and repo state are restored/untouched).
  This is a **security/safety boundary** — do not hand-wave it.
- **Never auto-merge, never bypass the human checkpoint.** The benchmark only
  measures; it opens no PRs and merges nothing.
- **Zero runtime dependencies** (stdlib only: `tomllib`, `json`, `statistics`,
  `subprocess`, `pathlib`). No pandas/numpy, no promptfoo/LangSmith/Braintrust.
- **Engine stays project-agnostic** — no stack fingerprints in
  `.redteam/workflows/` or non-example tests; keep `test_agents_generic_prompts.py`
  green. Benchmark sets live under `.redteam/benchmarks/` (project-owned, like
  batches), not in the engine.

## Operator delegation (autonomy clause)

Plan-level scope questions are delegated to the operator agent: prefer the
narrowest change that satisfies the MVP, and record decisions in
`ask_user_response.md` (or the final report). **Widening beyond the MVP
(matrix, Pareto, recommend, judge) or weakening the "never corrupt the repo" /
"never auto-merge" safety boundaries is NOT delegated** — surface those.

## Non-goals (Phase 2+, do NOT build)

- Matrix expansion (`[[benchmark.matrix]]`), Pareto frontier,
  `recommend-models --profile`, LLM-judge scorers, sqlite mirror, any external
  eval-platform export.

## Notes for decomposition

- Natural layering (the decomposer may split into a stacked chain, later tasks
  stacked on earlier):
  1. **Config + JSONL store**: `benchmark.toml` parser (fail-loud) + the result
     record schema + append/resume helpers. Pure, highly testable.
  2. **`benchmark` runner**: the loop over configs × tasks × reps behind the
     injectable `run_one` seam, budget accounting, `--dry-run`, JSONL append,
     repo-safety (isolated/temp state). Metric extraction from a task's
     `state.json` + `phase_telemetry`.
  3. **`benchmark-report`**: read JSONL → aggregate deterministic metrics →
     markdown diff table + `benchmark` / `benchmark-report` wired into
     `orchestrator.main` and the USAGE string.
- Each task's `outcome.md` needs a parseable `## Verification` section whose
  fenced ```yaml `commands:` list is **`bash .redteam/scripts/verify.sh` ONLY**
  (do NOT add a bare `pytest …` line — in this venv-based repo bare pytest is not
  on PATH and the verification exec would fail; verify.sh already runs the suite).
- Pin each task's Affected files strictly inside `.redteam/workflows/` and
  `.redteam/tests/`.
